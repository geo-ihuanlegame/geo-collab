# 前端稳定 + 提速小包（方案 A）设计

- 日期：2026-06-10
- 状态：已与用户确认设计，待实施
- 背景：前端长期缺乏分包与统一错误处理，用户痛点为「首屏慢」「白屏后整页废掉」「点了没反应」「代码看不懂改不动」，且四个主要页面（内容/智能体/AI 生文/任务账号）均受影响——因此选择横切面修复而非深挖单页。

## 目标

不引入新依赖、不动业务逻辑，用两个小 PR 解决「慢」「白屏」「静默失败」，并为后续清理建立统一模式。

## 现状诊断（2026-06-10 实测）

- `App.tsx` 静态 import 全部 11 个 Workspace，无任何 `React.lazy` / 动态 `import()`；`vite.config` 无 `manualChunks`。Tiptap、Lucide 与全部业务代码打进单一 chunk，登录页也要下载全量 JS。
- 每个 tab 有 `ErrorBoundary`，但 App.tsx 11 处都传了自定义 `fallback`（一行"出错请刷新重试"文字），覆盖了 `ErrorBoundary.tsx` 自带的「错误信息 + 重试按钮」兜底界面——出错后整个 tab 报废，只能刷新整页（丢失其它 tab 状态）。
- 异步请求错误处理：21 个文件散落 90 处手写 catch，写法各异；漏掉 catch 的 Promise 失败完全静默（表现为"按钮点了没反应"）。
- 轮询：4 处手写 `setInterval`（TasksWorkspace ×2、PipelineEditor、RunDetailModal），启停/清理逻辑各写各的。
- 最大文件：`styles.css` 2783 行、`ContentWorkspace.tsx` 1306 行（33 处 hook 调用）。本次不拆（见「不做什么」）。

## PR1 — 提速 + 防白屏（改动集中在 3 个文件）

### ① 按 tab 懒加载（解决「慢」）

`App.tsx` 里 11 个 Workspace 从静态 import 改为 `React.lazy(() => import(...))`（组件均为具名导出，需 `.then((m) => ({ default: m.XxxWorkspace }))`），每个 tab 外层套 `<Suspense fallback={加载占位}>`，ErrorBoundary 结构不变。Vite 对每个动态 import 自动拆 chunk：

- 登录页不再下载任何业务代码；
- 打开任一 tab 不附带下载其它页面代码；
- 改某页面只使该页 chunk 失效，其余走浏览器缓存。

诚实预期：内容管理是默认 tab 且含最重的 Tiptap，默认页首开提升有限；提升最大的是登录页与切换其它 tab 的体验。

### ② 出错后 tab 内一键恢复（解决「白屏后整页废」）

删掉 App.tsx 11 处自定义 `fallback`，回落到 ErrorBoundary 自带兜底界面（错误信息 + 「重试」按钮，点击原地重挂载子树，不刷新整页）。给 ErrorBoundary 新增可选 `title` prop 用于显示"内容管理出错"等上下文。

### ③ 全局异步错误 toast（解决「点了没反应」）

新增一个挂在 `ToastProvider` 内部的小组件（如 `GlobalErrorListener`），监听 `window` 的 `unhandledrejection` 与 `error` 事件，弹红色 error toast 显示错误信息。已被局部 catch 处理的错误不受影响（不会触发这两个事件）。

## PR2 — 统一数据加载 hook（解决「看不懂」的第一步)

新增 `web/src/hooks/useApiData.ts`：

- `useApiData(fetcher)` — 统一管理 loading / error / data 与卸载后竞态丢弃，返回 `{ data, loading, error, refresh }`；
- `usePolling(fn, intervalMs, enabled)` — 统一轮询定时器启停与清理。

示范改造范围（仅此一处，不搞一次性大迁移）：

1. `TasksWorkspace.tsx`（13 处 catch、2 个定时器，现状最乱）。只读列表（accounts/articles/groups）迁 `useApiData`，两个 `setInterval` 迁 `usePolling`；tasks/records/logs 因被 SSE、表单多处写入，保持手动管理。

其余页面后续改到哪换到哪。

> 2026-06-10 计划阶段修订：原拟一并改造 `PipelineEditor.tsx` 的运行状态轮询，实读代码后发现其已有防脏写守卫（`pollRef` 比对）且是命令式触发（点「运行」才开始、带 run_id），硬套声明式 hook 风险大于收益，故 descope。

## 不做什么（明确出界）

- 不拆 `ContentWorkspace.tsx`、不拆 `styles.css`（后续单独立项）；
- 不引入 TanStack Query 等任何新依赖；
- 不改后端接口与业务行为；
- 不动 `vite.config` 的 `manualChunks`（lazy 分包已够，避免过度配置）。

## 验证方式

前端无单测框架，CI 门禁为 `pnpm --filter @geo/web typecheck` + `build`。每个 PR：

1. typecheck + build 通过；
2. 对比 `vite build` 输出的 chunk 体积（分包前后数字写进 PR 描述）；
3. dev server 手动冒烟：逐 tab 打开；DevTools 断网验证失败请求弹 toast；人为抛渲染错误验证「重试」按钮原地恢复。

## 风险

- React.lazy 改造若漏掉某个具名导出映射，build 时即报错（typecheck/build 门禁可拦住）。
- `unhandledrejection` 可能暴露既有的静默失败（弹出以前看不见的报错）——这是预期行为，属于把暗病变明病；若某处误报噪音大，再局部补 catch。
- PR2 改造 TasksWorkspace 行为回归风险靠手动冒烟覆盖（无单测兜底），改动严格限定为等价替换。
