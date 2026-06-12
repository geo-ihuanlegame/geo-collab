# 暗色玻璃科技风 UI 升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Geo 协作平台前端从「暖色编辑/纸感」全面重构为「暗色玻璃科技风 + 亮纸编辑器」，通过就地重写 `web/src/styles.css` 的 token 层与组件层实现，类名与 DOM 基本不动。

**Architecture:** 现有 `styles.css`（~2863 行）已是 CSS 变量驱动，全站颜色读自 `:root` 的 `--accent` / `--fg` / `--paper` / `--cream` / `--hair` 等变量。重写 token 层即可让暗色基底级联到所有页面；随后逐节修正不走变量的硬编码颜色与组件细节；最后给编辑器/阅读区加 `.paper-scope` 局部翻亮。纯 CSS + 极少量纯展示性 JSX className，不动业务逻辑/数据流/API。

**Tech Stack:** React 19 + Vite + TypeScript（strict）+ 单文件 `web/src/styles.css`，无 Tailwind、无组件库。验证用 `pnpm --filter @geo/web typecheck` / `build`，视觉用可视化伴侣（`.superpowers/brainstorm` server）或 Playwright 截 Vite dev server（5173）。

**设计依据：** `docs/superpowers/specs/2026-06-09-ui-dark-glass-redesign-design.md`

---

## 验证说明（贯穿全程）

CSS 重构没有单元测试可写，每个任务的验证 = **自动门禁 + 视觉核对**：

- **自动门禁（硬性，每个任务必过）：** `pnpm --filter @geo/web build`（含 `tsc -b`）必须成功。它能捕获 CSS 语法错误导致的构建失败和任何 TS 回归。
- **视觉核对（检查点）：** 启动 `pnpm --filter @geo/web dev`（端口 5173），用 Playwright（webapp-testing 技能）截图对应页面，或把改动后的类渲染进可视化伴侣预览。截图与本计划描述/草图一致即通过。

> 后端数据非必需也能看壳层与登录页；需要数据的页面若本地无后端，以"无数据空态"截图核对配色与组件即可。

---

## 文件结构

- **修改（主）：** `web/src/styles.css` —— 所有视觉改动集中于此。按现有 `/* ═══ 分区 ═══ */` 注释组织，重写顺序：tokens → base → shell → components → 各 feature。
- **修改（极少量，仅展示性包裹）：**
  - `web/src/features/content/ContentWorkspace.tsx` —— 给 Tiptap 编辑器/正文容器加 `paper-scope` className（Task 11）。
  - 如有其它长文阅读容器，同法加 `paper-scope`。
- **不改：** 任何 `.ts` 业务逻辑、`api/`、后端、路由。

---

## Task 1: 重写设计 Token 层（`:root`）

这是最高杠杆的一步：替换 `:root` 取值即可让全站翻成暗色。同时新增玻璃/霓虹/亮纸 token。

**Files:**
- Modify: `web/src/styles.css:1-32`（现有 `:root { ... }` 块）

- [ ] **Step 1: 用下面内容替换 `web/src/styles.css` 第 1–32 行的整个 `:root` 块**

```css
:root {
  /* ── 霓虹强调（靛蓝 → 紫）── */
  --acc-1: #6d6bf6;
  --acc-2: #a855f7;
  --grad: linear-gradient(120deg, var(--acc-1) 0%, var(--acc-2) 100%);
  --cyan: #38bdf8;

  --accent:      #6d6bf6;
  --accent-soft: rgba(109,107,246,0.16);
  --accent-deep: #b9b6ff;

  /* ── 状态色（暗底霓虹版）── */
  --green:      #34d399;
  --green-soft: rgba(52,211,153,0.14);
  --red:        #f87171;
  --red-soft:   rgba(248,113,113,0.14);
  --yellow:     #fbbf24;
  --yellow-soft:rgba(251,191,36,0.14);

  /* ── 文字 ── */
  --fg:   #eceefb;
  --fg-2: #99a0bd;
  --fg-3: #5f667e;

  /* ── 表面 / 玻璃 / 描边 ── */
  --bg:      #0a0c14;
  --bg-2:    #0e1120;
  --glass:        rgba(255,255,255,0.045);
  --glass-strong: rgba(255,255,255,0.07);

  --paper:   rgba(255,255,255,0.045);  /* 暗色下"卡片面"= 玻璃 */
  --cream:   #0e1120;                  /* 工作区底 */
  --cream-2: rgba(255,255,255,0.07);
  --sidebar: rgba(10,12,22,0.55);
  --hair:    rgba(255,255,255,0.08);
  --hair-2:  rgba(255,255,255,0.12);

  /* ── 亮纸 scope（仅在 .paper-scope 内通过覆盖生效，见 Task 11）── */
  --paper-bg:    #fbf9f4;
  --paper-fg:    #1a1a1a;
  --paper-fg-2:  #5c5751;
  --paper-fg-3:  #9a958d;
  --paper-hair:  #e8e5de;

  --serif: "Fraunces", Georgia, "Noto Serif SC", "Source Han Serif SC", serif;
  --sans:  -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
  --mono:  "JetBrains Mono", "SF Mono", "Cascadia Code", Consolas, monospace;

  --r-sm: 6px;
  --r:    10px;
  --r-lg: 14px;

  --easing: cubic-bezier(0.22, 1, 0.36, 1);
}
```

- [ ] **Step 2: 给 `body` 加暗色氛围背景**

找到 `body { ... }` 规则（约第 38–46 行），把 `background: var(--cream);` 这一行替换为：

```css
  background:
    radial-gradient(900px 460px at 12% -8%, rgba(109,107,246,0.20), transparent 60%),
    radial-gradient(760px 420px at 100% 0%, rgba(168,85,247,0.16), transparent 55%),
    linear-gradient(180deg, var(--bg), var(--bg-2));
  background-attachment: fixed;
```

- [ ] **Step 3: 运行构建确认无语法错误**

Run: `pnpm --filter @geo/web build`
Expected: 构建成功（`tsc -b` 与 `vite build` 均无报错）。

- [ ] **Step 4: 视觉核对**

启动 `pnpm --filter @geo/web dev`，打开 http://127.0.0.1:5173 。预期：全站底色变深蓝黑、文字变浅、侧边栏/卡片呈半透明玻璃感（细节会在后续任务打磨，此步只确认"整体翻暗且无大面积白底/不可读文字"）。用 Playwright 截图存档或可视化伴侣核对。

- [ ] **Step 5: Commit**

```bash
git add web/src/styles.css
git commit -m "feat(ui): 重写设计 token 层为暗色玻璃霓虹基底"
```

---

## Task 2: 壳层 —— 侧边栏

**Files:**
- Modify: `web/src/styles.css:69-147`（SIDEBAR 分区）

- [ ] **Step 1: 替换侧边栏相关规则**

把 `.sidebar`、`.brandMark`、`.navItem` 及其状态规则替换/补充为：

```css
.sidebar {
  display: flex;
  flex-direction: column;
  background: var(--sidebar);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-right: 1px solid var(--hair);
  padding: 28px 18px 24px;
  overflow-y: auto;
}

.brandMark {
  width: 30px;
  height: 30px;
  border-radius: 9px;
  background: var(--grad);
  box-shadow: 0 6px 18px rgba(124,107,246,0.5);
  flex-shrink: 0;
}

.navItem {
  display: flex;
  align-items: center;
  gap: 11px;
  width: 100%;
  padding: 10px 12px;
  border-radius: var(--r);
  color: var(--fg-2);
  font-size: 14px;
  font-weight: 500;
  text-align: left;
  border: 1px solid transparent;
  transition: background .18s var(--easing), color .18s, border-color .18s;
}

.navItem:hover { background: var(--glass); color: var(--fg); }

.navItem.active {
  color: #fff;
  background: linear-gradient(100deg, rgba(109,107,246,0.30), rgba(168,85,247,0.16));
  border-color: rgba(140,120,255,0.35);
  box-shadow: 0 4px 16px rgba(109,107,246,0.25), inset 0 1px 0 rgba(255,255,255,0.08);
  font-weight: 600;
  position: relative;
}

.navItem.active::before {
  content: "";
  position: absolute;
  left: -18px;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 18px;
  border-radius: 2px;
  background: var(--grad);
  box-shadow: 0 0 10px var(--acc-1);
}

.navDot {
  width: 7px; height: 7px; border-radius: 50%;
  background: transparent; margin-left: auto; flex-shrink: 0;
  transition: all .18s;
}

.navItem.active .navDot {
  background: var(--grad);
  box-shadow: 0 0 8px var(--acc-1);
}
```

> 注意：`.brandMark` 由 4px 竖条改成 30px 圆角方块。若希望保留竖条造型，跳过 `.brandMark` 改动即可——两者都可，本计划默认升级为发光方块以增强品牌感。

- [ ] **Step 2: 构建**

Run: `pnpm --filter @geo/web build`
Expected: 成功。

- [ ] **Step 3: 视觉核对**

dev server 截图侧边栏：磨砂玻璃底、激活项渐变高亮 + 左侧发光条 + 右侧光点、品牌发光方块。

- [ ] **Step 4: Commit**

```bash
git add web/src/styles.css
git commit -m "feat(ui): 侧边栏改玻璃磨砂 + 霓虹渐变激活态"
```

---

## Task 3: 壳层 —— 顶栏与工作区

**Files:**
- Modify: `web/src/styles.css:149-234`（WORKSPACE / TOPBAR 分区）

- [ ] **Step 1: 调整工作区底与标题**

`.workspace` 的 `background: var(--cream);` 保持（现在已是暗色）。把 `.eyebrow::before` 与 `h1` 标题适配暗底——找到 `h1 { ... }`，把 `color: var(--fg);` 保留即可（变量已暗适配）。新增标题渐变描边可选；为保持可读性，本任务**仅确认**标题在暗底清晰，无需改 `h1`。

给 `.topbar` 下方增加一条霓虹细线（可选装饰），在 `.topbar { ... }` 规则末尾追加：

```css
.topbar { border-bottom: 1px solid var(--hair); padding-bottom: 20px; }
```

- [ ] **Step 2: 构建**

Run: `pnpm --filter @geo/web build`
Expected: 成功。

- [ ] **Step 3: 视觉核对 + Commit**

dev server 确认顶栏标题清晰、分隔线柔和。
```bash
git add web/src/styles.css
git commit -m "feat(ui): 顶栏分隔线与工作区暗底适配"
```

---

## Task 4: 组件 —— 按钮

`.primaryButton` 现在用 `var(--fg)`（浅色）做底会变成"浅底深字"，需改为渐变。同时修掉硬编码 `#000` / `#edddd8`。

**Files:**
- Modify: `web/src/styles.css:236-292`（BUTTONS 分区）

- [ ] **Step 1: 替换按钮规则**

```css
.primaryButton {
  background: var(--grad);
  color: #fff;
  border-color: transparent;
  box-shadow: 0 6px 20px rgba(124,107,246,0.40);
}
.primaryButton:hover:not(:disabled) {
  box-shadow: 0 8px 26px rgba(124,107,246,0.55);
  filter: brightness(1.06);
}

.secondaryButton {
  background: var(--glass);
  color: var(--fg);
  border-color: var(--hair-2);
}
.secondaryButton:hover:not(:disabled) {
  border-color: rgba(140,120,255,0.45);
  background: var(--glass-strong);
}

.dangerButton {
  background: var(--red-soft);
  color: var(--red);
  border-color: rgba(248,113,113,0.30);
}
.dangerButton:hover:not(:disabled) { background: rgba(248,113,113,0.22); }

.fileButton {
  position: relative;
  background: var(--glass);
  color: var(--fg-2);
  border-color: var(--hair-2);
  cursor: pointer;
}
.fileButton:hover { border-color: rgba(140,120,255,0.45); background: var(--glass-strong); }

.iconButton {
  display: inline-grid;
  place-items: center;
  width: 30px; height: 30px;
  border: 1px solid var(--hair);
  border-radius: var(--r-sm);
  color: var(--fg-2);
  background: var(--glass);
  transition: all .14s;
}
.iconButton:hover:not(:disabled) { color: var(--fg); border-color: var(--hair-2); background: var(--glass-strong); }
```

> `.fileButton input` / `.toolbarFile input` 的隐藏规则（约 277–279 行）保持不动。

- [ ] **Step 2: 构建 → 视觉核对 → Commit**

Run: `pnpm --filter @geo/web build`（Expected: 成功）。截图确认主按钮渐变发光、次/危险/图标按钮玻璃化。
```bash
git add web/src/styles.css
git commit -m "feat(ui): 按钮改渐变发光 + 玻璃次级态"
```

---

## Task 5: 组件 —— 面板、徽章、输入、状态

**Files:**
- Modify: `web/src/styles.css:294-371`（STATUS / PANEL / BADGES / INPUTS 分区）

- [ ] **Step 1: 面板玻璃化**

```css
.panel {
  background: var(--glass);
  border: 1px solid var(--hair);
  border-radius: var(--r-lg);
  padding: 24px 26px;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
}
```

- [ ] **Step 2: 徽章霓虹胶囊**

把 `.badge` 基础规则替换为：

```css
.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 9px;
  border-radius: 99px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.3px;
  white-space: nowrap;
  background: var(--cream-2);
  color: var(--fg-2);
  border: 1px solid var(--hair);
}
```

各状态色规则（`.badge.valid` 等，332–349 行）**无需改动**——它们已读 `--green` / `--red` / `--yellow` / `--accent` 变量，Task 1 已把这些变量改成暗底霓虹版。仅需给彩色徽章补描边，在 332 行前插入：

```css
.badge.valid, .badge.succeeded { border-color: rgba(52,211,153,0.30); }
.badge.failed, .badge.expired  { border-color: rgba(248,113,113,0.30); }
.badge.running                  { border-color: rgba(109,107,246,0.30); }
.badge.waiting_manual_publish, .badge.partial_failed { border-color: rgba(251,191,36,0.30); }
```

- [ ] **Step 3: 输入聚焦霓虹光晕**

`input:focus` 规则（368–371 行）已读 `--accent` / `--accent-soft`，Task 1 后自动成霓虹。补全输入底色——在 INPUTS 分区的 `input[...]{}` 选择器组里追加：

```css
input[type="text"], input[type="url"], input:not([type]), select, textarea {
  background: var(--glass);
  border: 1px solid var(--hair-2);
  border-radius: var(--r-sm);
  color: var(--fg);
}
input::placeholder, textarea::placeholder { color: var(--fg-3); }
```

> 若某些页面对 input 已有局部 background/border 规则，本全局规则可能被覆盖，属正常；后续 feature 任务逐页核对。

- [ ] **Step 4: 滚动条与选区暗色化**

找到顶部（约 53–58 行）滚动条规则，把 `border: 3px solid var(--cream);` 改为 `border: 3px solid transparent; background-clip: padding-box;`，并确认 `::selection` 用 `var(--accent-soft)` / `var(--accent-deep)`（已暗适配，无需改）。

- [ ] **Step 5: 构建 → 视觉核对 → Commit**

Run: `pnpm --filter @geo/web build`（Expected: 成功）。
```bash
git add web/src/styles.css
git commit -m "feat(ui): 面板/徽章/输入/滚动条暗色玻璃霓虹化"
```

---

## Task 6: 内容管理页

**Files:**
- Modify: `web/src/styles.css:373-965`（CONTENT WORKSPACE 分区）

- [ ] **Step 1: 卡片面玻璃化**

把该分区内 `.listPane`、`.editorPane` 的 `background: var(--paper);`（约 386、400 行）保持（`--paper` 现已是玻璃）；给二者补 `backdrop-filter: blur(10px);`。把 `.searchRow` 的 `background: var(--cream);`（约 417 行）改为 `background: var(--glass);`。

- [ ] **Step 2: 修硬编码蓝**

第 960 行 `background: #3b82f6;` 改为 `background: var(--grad);`。

- [ ] **Step 3: 逐项核对该分区其它硬编码色**

在 `web/src/styles.css` 的 373–965 行范围内搜索 `#` 与 `rgb`，凡是亮底假设的浅色（白底、浅灰边）改为对应变量（`var(--glass)` / `var(--hair)` / `var(--fg-*)`）。逐条替换，保持语义。

- [ ] **Step 4: 构建 → 视觉核对 → Commit**

Run: `pnpm --filter @geo/web build`（Expected: 成功）。dev server 打开「内容管理」，确认列表/卡片玻璃化、无残留白底。
```bash
git add web/src/styles.css
git commit -m "feat(ui): 内容管理页玻璃化与配色适配"
```

---

## Task 7: AI 生文页

**Files:**
- Modify: `web/src/styles.css:1567-2295`（AI GENERATION 分区）+ `2296-2470`（REVIEW + AUTO-DISTRIBUTE 分区）

- [ ] **Step 1: 修该范围内硬编码色**

在 1567–2470 行搜索 `#` / `rgb(`，按下表替换（行号以当前文件为准，搜索值定位）：
- `2198`: `.schemeToggle .knob ... background: #fff;` → 保留 `#fff`（开关滑块在彩色轨道上用白，OK）。
- `2445`: `.reviewStripApprove:hover ... background: #000;` → 改 `background: var(--acc-2);`。
- 其余浅底假设色（白卡、浅灰边）→ 改 `var(--glass)` / `var(--hair)` / `var(--fg-*)`。

- [ ] **Step 2: 面板/卡片玻璃化**

该分区内凡 `background: var(--paper)` / `var(--cream)` 的容器，确认呈玻璃感；给主要卡片容器补 `backdrop-filter: blur(10px);`（仅限非长列表行容器）。

- [ ] **Step 3: 构建 → 视觉核对 → Commit**

Run: `pnpm --filter @geo/web build`（Expected: 成功）。打开「AI 生文」，确认问题池/方案池/运行卡片暗色玻璃化、状态徽章霓虹。
```bash
git add web/src/styles.css
git commit -m "feat(ui): AI 生文页玻璃化与配色适配"
```

---

## Task 8: 任务 / 分发引擎页

**Files:**
- Modify: `web/src/styles.css:1161-1382`（TASKS WORKSPACE / UNIFIED LIST 分区）

- [ ] **Step 1: 适配配色**

该分区内浅底假设色 → 变量。第 1660 行附近 `color: #fff;` 视上下文保留（彩底上白字）。列表行 `.taskGrid .listPane` 等保持玻璃面。长列表行容器**不要**加 `backdrop-filter`（性能）。

- [ ] **Step 2: 构建 → 视觉核对 → Commit**

Run: `pnpm --filter @geo/web build`（Expected: 成功）。打开「分发引擎」核对。
```bash
git add web/src/styles.css
git commit -m "feat(ui): 任务/分发引擎页配色适配"
```

---

## Task 9: 图片库 / 媒体矩阵页

**Files:**
- Modify: `web/src/styles.css:966-1136`（MEDIA WORKSPACE 分区）+ 图片库相关（约 2028-2113，lightbox/dropdown）

- [ ] **Step 1: 适配配色，保留 lightbox 深色**

- `2028`、`2113`: 灯箱上的 `rgba(0,0,0,.45)` + `#fff` 字 → 保留（灯箱本就深色覆盖层）。
- `2047`: `.imageLibraryDropdown button.danger { color: #e53e3e; }` → 改 `color: var(--red);`。
- `2048`: `:hover { background: #fff5f5; }` → 改 `background: var(--red-soft);`。
- 其它浅底卡片 → `var(--glass)` / `var(--hair)`。

- [ ] **Step 2: 构建 → 视觉核对 → Commit**

Run: `pnpm --filter @geo/web build`（Expected: 成功）。打开「图片库」核对网格卡片与灯箱。
```bash
git add web/src/styles.css
git commit -m "feat(ui): 图片库/媒体矩阵页配色适配"
```

---

## Task 10: 系统状态 / 审计 / 用户管理页

**Files:**
- Modify: `web/src/styles.css:1137-1160`（SYSTEM STATUS 分区）+ `1389-1425`（SIDEBAR USER 分区）

- [ ] **Step 1: 适配配色**

该范围内浅底假设色 → 变量。侧边栏用户卡（1389–1425）改玻璃面：`background: var(--glass); border: 1px solid var(--hair);`。

- [ ] **Step 2: 构建 → 视觉核对 → Commit**

Run: `pnpm --filter @geo/web build`（Expected: 成功）。打开「系统状态」与（管理员）审计/用户管理核对。
```bash
git add web/src/styles.css
git commit -m "feat(ui): 系统/审计/用户页配色适配"
```

---

## Task 11: 亮纸编辑器 scope

给 Tiptap 编辑器与正文阅读容器加 `.paper-scope`，在该 scope 内把内容变量覆盖回亮色纸面。

**Files:**
- Modify: `web/src/features/content/ContentWorkspace.tsx`（编辑器容器加 className）
- Modify: `web/src/styles.css`（新增 PAPER SCOPE 分区，置于 CONTENT WORKSPACE 分区之后）

- [ ] **Step 1: 在编辑器容器加 className**

打开 `web/src/features/content/ContentWorkspace.tsx`，找到渲染 Tiptap 编辑器正文的容器（`.editorPane` 或其内部 `EditorContent` 包裹元素）。给该包裹元素的 `className` 追加 `paper-scope`。例如把 `className="editorPane"` 改为 `className="editorPane paper-scope"`。

> 若 `.editorPane` 同时包含工具栏与正文且你只想让正文区变亮纸，则只给正文/`EditorContent` 的直接包裹 div 加 `paper-scope`。优先选「正文容器」粒度。

- [ ] **Step 2: 新增 PAPER SCOPE 样式**

在 `web/src/styles.css` 的 CONTENT WORKSPACE 分区末尾（第 965 行附近）追加：

```css
/* ═══════════════════════════════ PAPER SCOPE（亮纸编辑器）═══════════════════════════════ */
.paper-scope {
  /* 在 scope 内把内容相关变量翻成亮色纸面 */
  --paper: var(--paper-bg);
  --cream: var(--paper-bg);
  --cream-2: #efece3;
  --fg: var(--paper-fg);
  --fg-2: var(--paper-fg-2);
  --fg-3: var(--paper-fg-3);
  --hair: var(--paper-hair);
  --hair-2: #d6d2c8;

  background: var(--paper-bg);
  color: var(--paper-fg);
  border-radius: var(--r-lg);
}

/* 正文排版：衬线标题延续品牌，墨色正文 */
.paper-scope .ProseMirror { color: var(--paper-fg); }
.paper-scope .ProseMirror h1,
.paper-scope .ProseMirror h2,
.paper-scope .ProseMirror h3 { font-family: var(--serif); color: var(--paper-fg); }
.paper-scope ::selection { background: rgba(109,107,246,0.18); color: #4549C5; }
```

> 严格以 `.paper-scope` 前缀限定，防止样式泄漏到暗壳。`.ProseMirror` 是 Tiptap 正文根类；若实际类名不同，用 dev server 检查 DOM 后替换。

- [ ] **Step 3: 构建**

Run: `pnpm --filter @geo/web build`
Expected: 成功（含 `tsc -b`，确认 TSX 改动无类型错误）。

- [ ] **Step 4: 视觉核对**

dev server 打开「内容管理」并进入一篇文章编辑。预期：左侧列表/外壳仍暗色，编辑器正文区为米白纸面 + 墨色字 + 衬线标题，形成明暗对比；scope 外无亮色泄漏。

- [ ] **Step 5: Commit**

```bash
git add web/src/styles.css web/src/features/content/ContentWorkspace.tsx
git commit -m "feat(ui): 亮纸编辑器 scope（暗壳内嵌护眼纸面）"
```

---

## Task 12: 登录页与全局收尾

登录页（AUTH PAGES 分区）已有自带深色（`#0f172a` / `#2563eb`），需统一到新霓虹体系。

**Files:**
- Modify: `web/src/styles.css:1426-1566`（AUTH PAGES 分区）

- [ ] **Step 1: 统一登录页配色**

替换该分区内硬编码色：
- `1432`: `background: #0f172a;` → `background: linear-gradient(180deg, var(--bg), var(--bg-2));`
- `1454`/`1518`/`1519`: `background: #2563eb;` → `background: var(--grad);`
- `1497`: `border-color: #2563eb;` → `border-color: var(--acc-1);`
- `1525`: `background: #1d4ed8;` → `filter: brightness(1.06);`（删除该硬编码底色，改用亮度悬浮）
- 卡片容器改玻璃面 `var(--glass)` + `1px solid var(--hair)` + `backdrop-filter: blur(14px)`。

- [ ] **Step 2: 全局硬编码色终检**

在整份 `web/src/styles.css` 搜索剩余的亮底假设硬编码色（`#fff` 白底、浅灰 `#e...`/`#f...` 边/底），逐条核对：彩底上的白字保留，白底容器改 `var(--glass)`。记录任何故意保留项。

- [ ] **Step 3: 构建 + 类型 + lint**

Run:
```bash
pnpm --filter @geo/web build
pnpm --filter @geo/web lint
```
Expected: build 成功；lint 不新增 error（非阻塞，但不退步）。

- [ ] **Step 4: 全站视觉巡检**

dev server 逐页截图（登录、内容、AI 生文、智能体管理、分发引擎、图片库、系统状态、编辑器亮纸），对照 spec 第 5/6 节确认一致、无白底残留、文字对比度可读。

- [ ] **Step 5: Commit**

```bash
git add web/src/styles.css
git commit -m "feat(ui): 登录页统一霓虹体系 + 全局硬编码色收尾"
```

---

## 自查（计划层面）

- **Spec 覆盖：** token（Task 1）、壳层侧边栏/顶栏（Task 2-3）、组件按钮/徽章/面板/输入（Task 4-5）、10 页逐个（Task 6-10, 12）、亮纸 scope（Task 11）、风险中的性能（长列表不加 blur，Task 8）与防泄漏（Task 11 前缀限定）、验证（每任务 build + 视觉，Task 12 lint）。spec 各节均有对应任务。
- **占位符：** 无 TBD/TODO；每个 CSS 步骤给出实际代码或精确"搜索值→替换值"。少数"逐条搜索硬编码色"步骤受单文件 2863 行所限无法穷举，已给出明确规则 + 已知行号锚点。
- **类型一致：** 变量名（`--acc-1/--acc-2/--grad/--glass/--paper-*`）在 Task 1 定义，后续任务一致引用。`paper-scope` className 在 Task 11 定义并使用。
```
