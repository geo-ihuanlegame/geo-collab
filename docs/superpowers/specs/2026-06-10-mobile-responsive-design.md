# 移动端适配设计文档

- **日期**：2026-06-10
- **目标**：在已完成的「暗色玻璃科技风」UI 之上，为整个平台做移动端（手机）适配，全功能可用。
- **状态**：已通过头脑风暴确认，待转实现计划。
- **前置**：UI 重构见 `2026-06-09-ui-dark-glass-redesign-design.md`。

## 1. 背景与现状

前端 React 19 + Vite + TS，单文件 `web/src/styles.css`。`index.html` 已有 viewport meta。现有 3 个遗留 `@media`（1100/980/640px）来自旧的浅色设计，与重构后的固定高度 + `overflow-y:auto` 滚动模型部分冲突（如 980px 的 `.shell{height:auto;overflow:visible}`），本次统一删除重写。

桌面布局：`.shell` 为 `240px + 1fr` 网格；多页为双栏（`520–680px + 1fr`）：内容管理（列表+编辑器）、账号/媒体矩阵、分发引擎、AI 方案、智能体节点编排（`.peLayout`）；另有宽表格（审计/用户/智能体日志）。导航为左侧竖向边栏，含「内容管理 / 提示词管理」可展开子页（受控 `reviewTab` / `scope`，状态在 `App.tsx`）。

## 2. 已确认的方向决策

| 决策点 | 结论 |
|---|---|
| 导航模式 | B 底部标签栏 + 「更多」弹层（移动端） |
| 底栏 5 项 | 内容管理 / AI 生文 / 智能体管理 / 分发引擎 / 更多 |
| 适配范围 | 全功能平台适配（所有页面移动可用，复杂编辑器做到"可用+可滚动"） |
| 实现方式 | 方案 1：响应式 + 轻量移动外壳（媒体查询 + useIsMobile hook，复用现有组件，桌面零回归） |
| 内容页移动布局 | master-detail（列表 ↔ 编辑器切换） |
| 宽表格移动处理 | 横向滚动（overflow-x:auto） |

## 3. 断点策略

- 主断点 ≤768px = 移动模式：底部标签栏 + 单列堆叠 + 隐藏侧边栏。
- >768px：维持现有桌面侧边栏布局。
- 中间窄桌面（约 768–1024px）仅做小幅 padding/标题字号收缩。
- 所有移动样式严格包在 @media (max-width:768px) 内，桌面规则不受影响。

## 4. 移动外壳（useIsMobile 驱动）

### 4.1 useIsMobile hook
新增 web/src/hooks/useIsMobile.ts：基于 window.matchMedia('(max-width: 768px)')，监听变化返回 boolean。供 App.tsx 切换移动/桌面外壳。

### 4.2 App Bar（顶部）
移动模式下隐藏侧边栏，顶部渲染一条轻量 App Bar：左侧品牌 AI 渐变图标 + 当前页标题（由 activeNav/子页推导），吸顶。

### 4.3 底部标签栏
固定底部，5 项：内容 / 生文 / 智能体 / 分发 / 更多。
- 每项图标（复用 navItems 的 lucide 图标）+ 短标签；激活态霓虹高亮。
- 高度含 padding-bottom: env(safe-area-inset-bottom) 适配刘海屏。
- 点击非「更多」项 → handleNavClick(key)；点「更多」→ 打开「更多」弹层。
- 激活态：activeNav 属于底栏 5 项之一则高亮对应项；否则高亮「更多」。

### 4.4 「更多」底部弹层
点「更多」从底部滑出 sheet（带遮罩），列出其余入口：提示词管理、图片库、媒体矩阵、系统状态，以及（admin）用户管理、审计日志；底部显示用户名 + 退出登录。点任一项导航并关闭弹层。

## 5. 子页在移动端的处理

内容管理(未审核库/已审核库)、提示词管理(AI生文提示词/AI格式提示词) 的子页在桌面通过侧边栏展开；移动端底栏只暴露顶级，故：
- 在这两页顶部注入移动专用分段切换条（segmented control），切换子页。
- 复用已有受控状态：contentReviewTab / promptsScope 及其 setter（已在 App.tsx），通过 props 传入；分段条仅在 isMobile 时渲染。
- 桌面端这两页不出现该分段条（仍由侧边栏子页驱动），行为不变。

## 6. 布局堆叠（@media max-width:768px）

- .shell：移除侧边栏列 → 单列；.sidebar { display:none }；工作内容区底部留出底栏高度 + 安全区的 padding-bottom，避免被底栏遮挡。
- 内容管理（master-detail）：.contentGrid 单列；移动端默认显示列表（.listPane），隐藏 .editorPane；当有文章被加载（draft.id != null 或显式 mobileView==='editor'）时编辑器全屏接管并显示「← 返回列表」回到列表。复用现有 loadArticle/resetDraft 状态，新增最小移动视图状态。
- 其它双栏（账号/媒体 .mediaGrid、分发 .taskGrid、AI 方案、节点编排 .peLayout）→ 单列堆叠。
- 智能体节点编排：.peLayout 堆叠 —— 节点列表 .peNodeList 变顶部横向滚动条，配置面板 .pePanel 在下，整体可纵向滚动。
- 宽表格（审计/用户/智能体日志）：表格外层容器加 overflow-x:auto，横向滚动，不换行；表头粘性保持。
- 富文本工具栏：overflow-x:auto + flex-wrap:nowrap，横向滚动避免溢出换行。
- 顶栏 .topbar：标题 + 操作纵向堆叠或换行；极窄屏操作按钮可仅显图标。
- 弹窗（.modal/.schemePanel/.modalCard/.modalBackdrop）：移动端近全宽（width: calc(100vw - 24px)），max-height 适配，底部留安全区。

## 7. 触控与字号

- 底栏项、关键按钮最小可点区域 ≥44px。
- 移动端基础字号略增（正文 ~15px）。
- 输入框/文本域移动端 font-size: 16px，避免 iOS 聚焦自动缩放。

## 8. 范围与非目标

范围内：styles.css 移动媒体查询；新增 useIsMobile hook、底部标签栏组件、「更多」弹层组件；App.tsx 注入移动外壳；内容/提示词页注入移动分段条；内容页 master-detail 的最小移动状态。纯展示性/布局性改动，不改业务逻辑、数据流、API。

非目标（YAGNI）：
- 不做 PWA / 离线 / 安装。
- 不为移动端单独写一套页面组件（方案 2 已否决）。
- 不追求富文本/节点编排在手机上的"原生手感"，只保证可用+可滚动。
- 不改后端、不改路由结构。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 富文本/节点编排小屏拥挤 | 堆叠 + 横向/纵向可滚动；工具栏横滚；接受"可用"标准 |
| 底栏遮挡内容 | 工作内容区统一 padding-bottom = 底栏高 + safe-area |
| 旧媒体查询冲突 | 删除原 1100/980/640px 三处，统一重写 |
| 桌面回归 | 移动样式全部包在 @media (max-width:768px) + isMobile 分支；桌面规则不动 |
| iOS 聚焦缩放 | 输入框移动端 16px |
| 刘海/手势区 | env(safe-area-inset-*) |

## 10. 验证

- pnpm --filter @geo/web typecheck 与 build 通过。
- dev server 用窄窗 / 浏览器响应式模式 / 可视化伴侣按 390px 宽逐页核对：底栏 + 激活态、「更多」弹层、App Bar、内容 master-detail（列表↔编辑器↔返回）、内容/提示词移动分段子页、宽表格横滚、节点编排堆叠、弹窗全宽、富文本工具栏横滚。
- 桌面（>768px）回归核对：侧边栏、嵌套子页、双栏、滚动模型一切如常。
