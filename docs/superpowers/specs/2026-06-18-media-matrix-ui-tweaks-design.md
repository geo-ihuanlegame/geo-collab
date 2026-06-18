# 媒体矩阵账号页 UI 微调（6 项）设计稿

- 日期：2026-06-18
- 分支 / worktree：`worktree-feat+frontend`（基于 main `3b29308`）
- 范围：**纯前端**（React + CSS）。本次不动后端。
- 主题：现有 **深色玻璃风**（`html{color-scheme:dark}`，`--bg:#0a0c14`，`--paper:rgba(255,255,255,.045)` 玻璃面，`--fg:#eceefb`）。所有改动复用 `:root` 既有 CSS 变量，不硬编码颜色。
- 设计参考：`demo.pen` 两张深色图（`账号页(深色·改后)` + `添加账号弹窗(深色·改后)`）。

## 背景

「媒体矩阵」账号页（前端 `web/src/features/accounts/`）做 6 处显示微调。涉及文件仅 4 个：

- [AccountsWorkspace.tsx](../../../web/src/features/accounts/AccountsWorkspace.tsx) — 页面、筛选栏（「中间行」）、待处理/列表拆分
- [AccountRow.tsx](../../../web/src/features/accounts/AccountRow.tsx) — 表头 + 行 + 状态药丸
- [AddAuthorizationDialog.tsx](../../../web/src/features/accounts/AddAuthorizationDialog.tsx) — 添加账号弹窗、AppSecret 输入
- [styles.css](../../../web/src/styles.css) — 相关样式

## 不在本次范围（归其他 worktree / 后续）

- **「已停用」状态的后端接口与数据建模**：当前后端 `Account.status` 仅 `valid / expired / unknown`（`server/app/modules/accounts/models.py:35` 的 CHECK 约束）。本次只做前端三态显示能力，待后端按 DB 建模新增 `disabled` 后自动点亮。由其他 worktree 负责。
- **失效/停用账号的归类与「状态」筛选的数据流**：失效号进「待处理」已实现且保持不动；中间行「状态」筛选当前对（仅含 valid 的）列表无实质过滤效果——此数据流由其他 worktree 处理，本次不改其逻辑，只做 UI（加标签、加 chip）。

## 状态模型（前端约定）

集中一个 `status → {label, tone}` 映射（在 `AccountRow.tsx` 内或抽到小工具），三态：

| status 值 | 文案 | 色调（CSS class） | 现状 |
|---|---|---|---|
| `valid` | 启用中 | `.statusPill.active`（`--green`） | 已有 |
| `disabled` | 已停用 | `.statusPill.disabled`（中性灰 `--fg-2` + `--glass`）**新增** | 后端暂不产出，前向兼容 |
| 其它（`expired`/`unknown`/…） | 已失效 | `.statusPill.inactive`（`--red`） | 已有 |

`Account.status` 类型为 `string`（`web/src/types.ts:251`），无需改类型。

## 逐项设计

### #1 中间行右移 + 左侧「平台」「状态」标签列
- 位置：`AccountsWorkspace.tsx` 筛选栏 `mediaMatrixFilterBar`（平台 chips 行 `:191`、状态 chips/搜索行 `:207`）。
- 做法：两行各加一个**固定宽度标签列**（如 48–52px，文案「平台」/「状态」，`--fg-2`），chips 整体右移与之对齐。新增 CSS `.mediaMatrixFilterRowLabel`；平台行改为 `label + chips` 横向结构，状态行左侧 `label + chips`、右侧搜索框不变。
- 验收：两行左侧出现「平台」「状态」注释，chips 右移对齐；窄屏不串行。

### #2 表头字号放大到 14px
- 位置：`styles.css` `.accountRowHeader { font-size: 11px }`（`:3780`）。
- 做法：`font-size: 11px → 14px`。`text-transform: uppercase` 对中文无效，保留。待处理区与列表区共用同一表头组件，一处即生效。
- 验收：状态/账号/平台/备注/操作 表头明显变大、与行高协调。

### #3 新增第三态「已停用」（不重命名「已失效」）
- 状态药丸：`AccountRow.tsx:17,22-25` 当前二元 `isActive ? "启用中" : "已失效"` → 改为上表三态映射。
- CSS：新增 `.statusPill.disabled`（`background: var(--glass)`，`color: var(--fg-2)`）。
- 状态筛选 chip：`AccountsWorkspace.tsx:207-224` 状态行增加「已停用」chip（`filterStatus === "disabled"`），`已失效` 保留；`filteredAccounts`（`:107-114`）增加 `disabled` 分支。
- 归类逻辑（待处理 vs 列表 `:96-104`）**保持不动**。
- 验收：药丸能渲染三态；筛选行出现「已失效」「已停用」两个 chip；后端无 `disabled` 数据时「已停用」筛选结果为空（预期）。

### #4 平台筛选 单选 → 多选
- 位置：`AccountsWorkspace.tsx` `filterPlatform: string`（`:46`）、过滤（`:107-114`）、chips（`:191-205`）。
- 做法：状态改 `selectedPlatforms: string[]`（或 `Set<string>`）。
  - 「全部」：清空集合；集合空时「全部」高亮、各平台不高亮。
  - 平台 chip：点击切换在集合中的存在；在集合内即高亮。
  - 过滤：`selectedPlatforms.length === 0 || selectedPlatforms.includes(a.platform_code)`。
- 高亮样式：现 `.mediaMatrixFilterChip.active` 用 `#2D2A24`（暖灰），深色底对比偏闷 → **改用主题紫 accent**（`background: var(--accent-soft)`，`border-color: var(--accent)`，文字提亮），与左侧导航选中态一致。
- 验收：可同时选多个平台、列表为并集；高亮清晰；再点「全部」复位、各平台恢复常态。

### #5 添加账号弹窗加高加宽
- 位置：`styles.css` `.addAuthDialog`（`:4221-4233`），`max-height: min(700px, 100vh-48px)`、`width: min(520px,100%)`。
- 做法：`max-height → min(860px, calc(100vh - 48px))`，`width → min(560px, 100%)`。`.addAuthBody` 仍 `overflow-y:auto` 作兜底。
- 注意：该 class 同被 `EditAccountDialog` 复用——加高是上限放宽，对内容更少的编辑弹窗无副作用。
- 验收：选「公众号」后 AppID/AppSecret/名称/联系方式/备注/分发 在常规视口一页全展开、无内部滚动条。（极矮屏仍受 `100vh-48` 上限，`overflow` 兜底。）

### #6 AppSecret 明文显示
- 位置：`AddAuthorizationDialog.tsx:367`，AppSecret `<input type="password">`。
- 做法：`type="password" → type="text"`。仅影响添加时输入框；后端永不回传 secret 原文，编辑弹窗无此字段，无额外泄露面。
- 验收：输入 AppSecret 时明文可见、非圆点。

## 风险 / 注意

- **多 worktree 并行同改账号页**：其他 worktree 也在动账号相关代码，`AccountsWorkspace.tsx` / `styles.css` 可能产生合并冲突——以最小 diff、不重排既有结构为原则降低冲突面。
- **前向兼容的死态**：`disabled` 在后端落地前，UI 上「已停用」筛选/药丁不会出现真实数据，属预期，不是 bug。

## 验证

- 前端门禁：`pnpm --filter @geo/web typecheck` + `pnpm --filter @geo/web build`（前端无单测框架，二者即 CI 门禁）。
- 人工核对（Vite 5173）：六项逐条对照 `demo.pen` 两张深色图。
