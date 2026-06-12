# 移动端适配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为平台做手机端适配：≤768px 时用「底部标签栏 + 更多弹层」替代侧边栏，双栏布局堆叠、宽表格横滚、内容页 master-detail，桌面零回归。

**Architecture:** 方案 1（响应式 + 轻量移动外壳）。新增 `useIsMobile` hook 驱动 `App.tsx` 切换外壳（移动端渲染 App Bar + `MobileNav` 底栏/更多弹层，CSS 隐藏侧边栏）；布局收缩全部包在 `@media (max-width:768px)`；内容/提示词页注入移动分段子页；内容页加最小 `mobileView` 状态做 master-detail。纯展示/布局改动，不动业务逻辑/API。

**Tech Stack:** React 19 + Vite + TS（strict）+ 单文件 `web/src/styles.css` + lucide-react 图标。验证用 `pnpm --filter @geo/web typecheck` / `build`，视觉用 dev server 按 390px 宽核对。

**设计依据：** `docs/superpowers/specs/2026-06-10-mobile-responsive-design.md`

---

## 验证说明（贯穿全程）

- **自动门禁（每任务必过）：** `pnpm --filter @geo/web build`（含 `tsc -b`）成功。
- **视觉核对（检查点）：** `pnpm --filter @geo/web dev`，浏览器开发者工具切到手机尺寸（如 iPhone 390×844）逐页核对；同时在 >768px 宽确认桌面无回归。
- Windows 主机，`pnpm` 可用；若缺用 `npx pnpm`。

## 文件结构

- **新增** `web/src/hooks/useIsMobile.ts` —— 响应式断点 hook。
- **新增** `web/src/components/MobileNav.tsx` —— 底部标签栏 + 「更多」弹层。
- **修改** `web/src/App.tsx` —— 注入移动外壳（App Bar + MobileNav），传 `isMobile` 及子页 props。
- **修改** `web/src/features/content/ContentWorkspace.tsx` —— 移动分段子页 + master-detail。
- **修改** `web/src/features/prompt-templates/PromptsWorkspace.tsx` —— 移动分段子页。
- **修改** `web/src/features/auth/UsersWorkspace.tsx` —— 表格加横滚容器。
- **修改** `web/src/styles.css` —— 移动外壳样式 + `@media (max-width:768px)` 布局收缩；删除旧 3 个媒体查询。

---

## Task 1: `useIsMobile` hook

**Files:** Create `web/src/hooks/useIsMobile.ts`

- [ ] **Step 1: 创建 hook**

```ts
import { useEffect, useState } from "react";

const QUERY = "(max-width: 768px)";

/** 视口宽度 ≤768px 时返回 true（移动模式）。响应窗口缩放/旋转。 */
export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState<boolean>(
    () => typeof window !== "undefined" && window.matchMedia(QUERY).matches,
  );
  useEffect(() => {
    const mq = window.matchMedia(QUERY);
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", onChange);
    setIsMobile(mq.matches);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return isMobile;
}
```

- [ ] **Step 2: 构建**

Run: `pnpm --filter @geo/web build` → Expected: 成功。

- [ ] **Step 3: Commit**

```bash
git add web/src/hooks/useIsMobile.ts
git commit -m "feat(mobile): useIsMobile 响应式断点 hook" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: MobileNav 组件（底栏 + 更多弹层）

**Files:** Create `web/src/components/MobileNav.tsx`

- [ ] **Step 1: 创建组件**

```tsx
import { useState } from "react";
import {
  Bot, FileText, Images, LogOut, MessagesSquare, MonitorCog,
  MoreHorizontal, RadioTower, ScrollText, Send, Sparkles, Users, X,
} from "lucide-react";
import type { NavKey } from "../types";

type Item = { key: NavKey; label: string; icon: typeof Bot };

const BOTTOM: Item[] = [
  { key: "content", label: "内容", icon: FileText },
  { key: "ai", label: "生文", icon: Sparkles },
  { key: "agents", label: "智能体", icon: Bot },
  { key: "tasks", label: "分发", icon: Send },
];

const MORE_BASE: Item[] = [
  { key: "prompts", label: "提示词管理", icon: MessagesSquare },
  { key: "image-library", label: "图片库", icon: Images },
  { key: "media", label: "媒体矩阵", icon: RadioTower },
  { key: "system", label: "系统状态", icon: MonitorCog },
];

const MORE_ADMIN: Item[] = [
  { key: "admin", label: "用户管理", icon: Users },
  { key: "audit-logs", label: "审计日志", icon: ScrollText },
];

export function MobileNav({
  activeNav,
  onNavigate,
  isAdmin,
  username,
  onLogout,
}: {
  activeNav: NavKey;
  onNavigate: (key: NavKey) => void;
  isAdmin: boolean;
  username: string;
  onLogout: () => void;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  const moreItems = isAdmin ? [...MORE_BASE, ...MORE_ADMIN] : MORE_BASE;
  const bottomActive = BOTTOM.some((b) => b.key === activeNav);

  function go(key: NavKey) {
    setMoreOpen(false);
    onNavigate(key);
  }

  return (
    <>
      {moreOpen && (
        <div className="mobileMoreOverlay" onClick={() => setMoreOpen(false)}>
          <div className="mobileMoreSheet" onClick={(e) => e.stopPropagation()}>
            <div className="mobileMoreHead">
              <span>更多</span>
              <button type="button" className="iconButton" onClick={() => setMoreOpen(false)}>
                <X size={16} />
              </button>
            </div>
            <div className="mobileMoreGrid">
              {moreItems.map((it) => {
                const Icon = it.icon;
                return (
                  <button
                    key={it.key}
                    type="button"
                    className={`mobileMoreItem${activeNav === it.key ? " active" : ""}`}
                    onClick={() => go(it.key)}
                  >
                    <Icon size={20} />
                    <span>{it.label}</span>
                  </button>
                );
              })}
            </div>
            <div className="mobileMoreUser">
              <span className="mobileMoreName">{username}</span>
              <button type="button" className="secondaryButton" onClick={onLogout}>
                <LogOut size={15} /> 退出
              </button>
            </div>
          </div>
        </div>
      )}

      <nav className="mobileBottomBar">
        {BOTTOM.map((it) => {
          const Icon = it.icon;
          return (
            <button
              key={it.key}
              type="button"
              className={`mobileTab${activeNav === it.key ? " active" : ""}`}
              onClick={() => go(it.key)}
            >
              <Icon size={20} />
              <span>{it.label}</span>
            </button>
          );
        })}
        <button
          type="button"
          className={`mobileTab${!bottomActive || moreOpen ? " active" : ""}`}
          onClick={() => setMoreOpen((v) => !v)}
        >
          <MoreHorizontal size={20} />
          <span>更多</span>
        </button>
      </nav>
    </>
  );
}
```

- [ ] **Step 2: 构建** → `pnpm --filter @geo/web build`（组件未被引用，仅验证类型）。Expected: 成功。

- [ ] **Step 3: Commit**

```bash
git add web/src/components/MobileNav.tsx
git commit -m "feat(mobile): MobileNav 底部标签栏 + 更多弹层组件" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: App.tsx 注入移动外壳

**Files:** Modify `web/src/App.tsx`

- [ ] **Step 1: 加 imports**

在顶部 import 区加：
```tsx
import { MobileNav } from "./components/MobileNav";
import { useIsMobile } from "./hooks/useIsMobile";
```

- [ ] **Step 2: 取 isMobile**

在 `AppShell` 组件体内、`const { user, loading, logout } = useAuth();` 之后加一行：
```tsx
  const isMobile = useIsMobile();
```

- [ ] **Step 3: 给 shell 加移动类 + App Bar**

找到 `return (\n    <ToastProvider>\n      <main className="shell">`，改成（给 `.shell` 加条件类，并在 `<aside className="sidebar">` 前插入移动 App Bar）：
```tsx
    <ToastProvider>
      <main className={`shell${isMobile ? " shellMobile" : ""}`}>
        {isMobile && (
          <header className="mobileAppBar">
            <div className="brandMark">AI</div>
            <span className="mobileAppName">AI插件自动化平台</span>
          </header>
        )}
        <aside className="sidebar">
```

- [ ] **Step 4: 在 workspace 后渲染 MobileNav**

找到 workspace `</section>` 与 `</main>` 之间（即 `        </section>\n      </main>`），改为：
```tsx
        </section>
        {isMobile && (
          <MobileNav
            activeNav={activeNav}
            onNavigate={handleNavClick}
            isAdmin={user.role === "admin"}
            username={user.username}
            onLogout={logout}
          />
        )}
      </main>
```

- [ ] **Step 5: 传移动子页 props（内容/提示词已有受控 state）**

把 `<ContentWorkspace ... reviewTab={contentReviewTab} />` 改为额外传 `isMobile` 与 setter：
```tsx
                <ContentWorkspace
                  dirtyCheckRef={contentDirtyRef}
                  isActive={activeNav === "content"}
                  reviewTab={contentReviewTab}
                  isMobile={isMobile}
                  onReviewTabChange={setContentReviewTab}
                />
```
把 `<PromptsWorkspace scope={promptsScope} />` 改为：
```tsx
                <PromptsWorkspace
                  scope={promptsScope}
                  isMobile={isMobile}
                  onScopeChange={setPromptsScope}
                />
```

- [ ] **Step 6: 构建** → `pnpm --filter @geo/web build`。Expected: 成功（ContentWorkspace/PromptsWorkspace 新 props 在 Task 5/6 加，若此步类型报错，先加可选 props 占位见 Task 5 Step 1 / Task 6 Step 1，或将本步与 Task 5/6 合并提交）。

> 实施顺序提示：先做 Task 5、Task 6 给两个组件加上可选 props，再回到本 Step 6 构建，避免类型缺失。Subagent 执行时按 Task 3→5→6 连续做、最后统一构建提交也可。

- [ ] **Step 7: Commit**

```bash
git add web/src/App.tsx
git commit -m "feat(mobile): App 注入移动 App Bar + 底部导航外壳" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 移动外壳样式（App Bar / 底栏 / 更多弹层 / 隐藏侧边栏）

**Files:** Modify `web/src/styles.css`（在文件末尾新增「MOBILE SHELL」分区）

- [ ] **Step 1: 末尾追加移动外壳样式**

```css
/* ═══════════════════════════════ MOBILE SHELL ═══════════════════════════════ */
.mobileAppBar { display: none; }
.mobileBottomBar { display: none; }

@media (max-width: 768px) {
  .shellMobile {
    grid-template-columns: 1fr;
    grid-template-rows: auto 1fr;
  }
  .shellMobile .sidebar { display: none; }

  .mobileAppBar {
    display: flex;
    align-items: center;
    gap: 10px;
    height: 52px;
    padding: 0 16px;
    background: rgba(10,12,22,0.85);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--hair);
    position: sticky;
    top: 0;
    z-index: 20;
  }
  .mobileAppName { font-family: var(--display); font-size: 15px; font-weight: 700; letter-spacing: -0.2px; }

  /* 工作区底部留出底栏高度 + 安全区 */
  .workspaceInner { padding: 20px 16px calc(72px + env(safe-area-inset-bottom)); }

  .mobileBottomBar {
    display: flex;
    position: fixed;
    left: 0; right: 0; bottom: 0;
    z-index: 40;
    height: calc(60px + env(safe-area-inset-bottom));
    padding-bottom: env(safe-area-inset-bottom);
    background: rgba(10,12,22,0.92);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-top: 1px solid var(--hair);
  }
  .mobileTab {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 3px;
    font-size: 10px;
    color: var(--fg-3);
    min-height: 44px;
  }
  .mobileTab.active { color: var(--accent-deep); }
  .mobileTab.active svg { color: var(--acc-2); }

  .mobileMoreOverlay {
    position: fixed; inset: 0; z-index: 41;
    background: rgba(4,6,12,0.62);
    backdrop-filter: blur(3px); -webkit-backdrop-filter: blur(3px);
    display: flex; align-items: flex-end;
  }
  .mobileMoreSheet {
    width: 100%;
    background: var(--surface-2);
    border-top: 1px solid var(--hair);
    border-radius: var(--r-lg) var(--r-lg) 0 0;
    padding: 16px 16px calc(20px + env(safe-area-inset-bottom));
    box-shadow: 0 -16px 48px rgba(0,0,0,0.5);
  }
  .mobileMoreHead { display: flex; align-items: center; justify-content: space-between; font-size: 14px; font-weight: 600; margin-bottom: 14px; }
  .mobileMoreGrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .mobileMoreItem {
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    padding: 16px 8px;
    background: var(--glass);
    border: 1px solid var(--hair);
    border-radius: var(--r);
    color: var(--fg-2);
    font-size: 12px;
  }
  .mobileMoreItem.active { color: #fff; background: linear-gradient(135deg, rgba(109,107,246,0.3), rgba(168,85,247,0.16)); border-color: rgba(140,120,255,0.35); }
  .mobileMoreUser { display: flex; align-items: center; justify-content: space-between; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--hair); }
  .mobileMoreName { font-size: 13px; color: var(--fg-2); }
}
```

- [ ] **Step 2: 构建** → `pnpm --filter @geo/web build`。Expected: 成功。

- [ ] **Step 3: 视觉核对**

dev server 手机尺寸：底栏 5 项 + 激活态、点「更多」弹出底部 sheet（网格入口 + 用户/退出）、App Bar 吸顶、内容不被底栏遮挡。桌面尺寸确认底栏/AppBar 不显示、侧边栏正常。

- [ ] **Step 4: Commit**

```bash
git add web/src/styles.css
git commit -m "feat(mobile): 移动外壳样式（App Bar/底栏/更多弹层/隐藏侧边栏）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 提示词页移动分段子页

**Files:** Modify `web/src/features/prompt-templates/PromptsWorkspace.tsx`

- [ ] **Step 1: 扩展 props**

把 `export function PromptsWorkspace({ scope: propScope }: { scope?: PromptScope } = {}) {` 改为：
```tsx
export function PromptsWorkspace(
  { scope: propScope, isMobile, onScopeChange }:
  { scope?: PromptScope; isMobile?: boolean; onScopeChange?: (s: PromptScope) => void } = {},
) {
```

- [ ] **Step 2: 在 header 后注入移动分段条**

找到 PromptsWorkspace 的 `</header>`（topbar 结束处），紧随其后插入：
```tsx
      {isMobile && (
        <div className="mobileSegTabs">
          <button
            type="button"
            className={`mobileSegTab${scope === "generation" ? " active" : ""}`}
            onClick={() => { setScope("generation"); onScopeChange?.("generation"); }}
          >
            AI生文提示词
          </button>
          <button
            type="button"
            className={`mobileSegTab${scope === "ai_format" ? " active" : ""}`}
            onClick={() => { setScope("ai_format"); onScopeChange?.("ai_format"); }}
          >
            AI格式提示词
          </button>
        </div>
      )}
```

- [ ] **Step 3: 构建** → `pnpm --filter @geo/web build`。Expected: 成功。

- [ ] **Step 4: Commit**

```bash
git add web/src/features/prompt-templates/PromptsWorkspace.tsx
git commit -m "feat(mobile): 提示词页移动分段子页（AI生文/AI格式）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 内容页移动分段子页 + master-detail

**Files:** Modify `web/src/features/content/ContentWorkspace.tsx`

- [ ] **Step 1: 扩展 props**

把 `export function ContentWorkspace({ dirtyCheckRef, isActive, reviewTab: reviewTabProp }: Props = {}) {` 与其 `interface Props` 改为：
```tsx
interface Props {
  dirtyCheckRef?: MutableRefObject<() => boolean>;
  isActive?: boolean;
  reviewTab?: ReviewStatus;
  isMobile?: boolean;
  onReviewTabChange?: (t: ReviewStatus) => void;
}

export function ContentWorkspace(
  { dirtyCheckRef, isActive, reviewTab: reviewTabProp, isMobile, onReviewTabChange }: Props = {},
) {
```

- [ ] **Step 2: 加移动视图状态**

在 `const [reviewTab, setReviewTab] = useState<ReviewStatus>("pending");` 附近加：
```tsx
  const [mobileView, setMobileView] = useState<"list" | "editor">("list");
```

- [ ] **Step 3: 选中文章/新建时切到编辑器视图**

在 `loadArticle` 函数体内（加载文章成功后，函数体最后）加一行 `setMobileView("editor");`。在 `resetDraft` 函数体内（新建草稿）也加 `setMobileView("editor");`。在 `loadGroup` 同理加 `setMobileView("editor");`。
（这些函数都在本文件，找到其定义在末尾加该行即可；只改这一行，不动其它逻辑。）

- [ ] **Step 4: 在 header 后注入移动分段子页**

找到内容页 `</header>`（紧接 `<section className="contentGrid">` 之前），插入：
```tsx
      {isMobile && mobileView === "list" && (
        <div className="mobileSegTabs">
          <button
            type="button"
            className={`mobileSegTab${reviewTab === "pending" ? " active" : ""}`}
            onClick={() => { setReviewTab("pending"); setArticlePage(0); setSelectedArticleIds([]); onReviewTabChange?.("pending"); }}
          >
            未审核库
          </button>
          <button
            type="button"
            className={`mobileSegTab${reviewTab === "approved" ? " active" : ""}`}
            onClick={() => { setReviewTab("approved"); setArticlePage(0); setSelectedArticleIds([]); onReviewTabChange?.("approved"); }}
          >
            已审核库
          </button>
        </div>
      )}
```

- [ ] **Step 5: 给 contentGrid 加移动视图类 + 返回按钮**

把 `<section className="contentGrid">` 改为：
```tsx
      <section className={`contentGrid${isMobile ? ` mobile-${mobileView}` : ""}`}>
```
在 `<section className="editorPane">` 之后、其内容最前面，插入移动返回按钮：
```tsx
          {isMobile && (
            <button type="button" className="mobileBackBtn" onClick={() => setMobileView("list")}>
              ← 返回列表
            </button>
          )}
```

- [ ] **Step 6: 构建** → `pnpm --filter @geo/web build`。Expected: 成功（含 tsc）。

- [ ] **Step 7: Commit**

```bash
git add web/src/features/content/ContentWorkspace.tsx
git commit -m "feat(mobile): 内容页移动分段子页 + master-detail（列表↔编辑器）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 布局收缩样式 + 分段条 + master-detail CSS

**Files:** Modify `web/src/styles.css`（在 MOBILE SHELL 的 `@media (max-width:768px)` 块内补充；分段条规则放块外通用或块内均可）

- [ ] **Step 1: 加分段条样式（移动块外，通用）**

在 MOBILE SHELL 分区起始处（`@media` 之前）加：
```css
.mobileSegTabs {
  display: flex;
  gap: 6px;
  margin-bottom: 16px;
  background: var(--glass);
  border: 1px solid var(--hair);
  border-radius: var(--r);
  padding: 4px;
}
.mobileSegTab {
  flex: 1;
  padding: 9px 10px;
  border-radius: var(--r-sm);
  font-size: 13px;
  font-weight: 600;
  color: var(--fg-3);
}
.mobileSegTab.active {
  color: #fff;
  background: linear-gradient(100deg, rgba(109,107,246,0.32), rgba(168,85,247,0.18));
}
.mobileBackBtn {
  align-self: flex-start;
  margin-bottom: 10px;
  padding: 7px 12px;
  font-size: 13px;
  color: var(--fg-2);
  background: var(--glass);
  border: 1px solid var(--hair-2);
  border-radius: var(--r-sm);
}
```

- [ ] **Step 2: 在 `@media (max-width:768px)` 块内补充布局收缩**

```css
@media (max-width: 768px) {
  /* 双栏 → 单列 */
  .contentGrid, .taskGrid, .mediaGrid { grid-template-columns: 1fr; }
  .peLayout { flex-direction: column; }
  .peNodeList { flex-direction: row; overflow-x: auto; flex-wrap: nowrap; padding-bottom: 6px; }
  .peNodeList > * { flex: 0 0 auto; }

  /* 内容 master-detail：按 mobileView 切换显示 */
  .contentGrid.mobile-list .editorPane { display: none; }
  .contentGrid.mobile-editor .listPane { display: none; }
  .contentGrid.mobile-editor { grid-template-rows: minmax(0, 1fr); }

  /* 顶栏堆叠 */
  .topbar { flex-direction: column; align-items: stretch; gap: 12px; }
  .topActions { flex-wrap: wrap; }
  h1 { font-size: 28px; }

  /* 富文本工具栏横滚（内容编辑器工具栏类名为 .toolbar）*/
  .toolbar { flex-wrap: nowrap; overflow-x: auto; }

  /* 宽表格横滚（用户管理表格无 wrapper，加在容器上）*/
  .usersTableWrap { overflow-x: auto; }
  .agentLogsScroll { overflow: auto; }

  /* 弹窗近全宽 */
  .modal, .groupPickerModal, .schemePanel, .modalCard {
    width: calc(100vw - 24px) !important;
    max-width: calc(100vw - 24px) !important;
  }

  /* 输入框 16px 防 iOS 聚焦缩放 */
  input, select, textarea, .input { font-size: 16px; }
  body { font-size: 15px; }
}
```

> 说明：类名均已核对存在 —— 富文本工具栏 `.toolbar`（components/editor/EditorToolbar.tsx 根节点）、节点编排 `.peLayout` / `.peNodeList`（PipelineEditor.tsx + styles.css:3136）。

- [ ] **Step 3: 用户管理表格加 wrapper**

`web/src/features/auth/UsersWorkspace.tsx`：把 `<div className="panel" style={{ padding: 0, overflow: "hidden" }}>` 内的 `<table ...>` 外面包一层 `<div className="usersTableWrap">`：
找到 `<table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>` 改为
```tsx
        <div className="usersTableWrap">
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
```
并在对应 `</table>` 后补 `</div>`。（审计日志表格已自带 `overflowX:auto` 容器，无需改。）

- [ ] **Step 4: 构建** → `pnpm --filter @geo/web build`。Expected: 成功。

- [ ] **Step 5: 视觉核对**

手机尺寸逐页：内容（列表→点文章→编辑器→返回列表）、内容/提示词分段子页切换、分发/账号双栏堆叠、节点编排堆叠 + 节点条横滚、审计/用户表格横滚、弹窗全宽、富文本工具栏横滚。桌面尺寸全回归核对。

- [ ] **Step 6: Commit**

```bash
git add web/src/styles.css web/src/features/auth/UsersWorkspace.tsx
git commit -m "feat(mobile): 布局收缩（双栏堆叠/表格横滚/master-detail/弹窗全宽）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 清理旧媒体查询 + 终检

**Files:** Modify `web/src/styles.css`

- [ ] **Step 1: 删除 3 个旧媒体查询**

定位并删除以下三段（与新断点冲突/重复）：
1. `@media (max-width: 1100px) { .workspaceInner { padding: 32px 28px 60px; } h1 { font-size: 34px; } }`
2. `@media (max-width: 980px) { .shell { grid-template-columns: 1fr; height: auto; overflow: visible; } .sidebar { ... } .contentGrid, .mediaGrid, .taskGrid { grid-template-columns: 1fr; } .formRow.split { grid-template-columns: 1fr; } .topbar { flex-direction: column; align-items: flex-start; } }`
3. `@media (max-width: 640px) { .lightboxInner { flex-direction: column; align-items: center; } .lightboxImg { max-width: 90vw; max-height: 60vh; } .lightboxInfo { width: 100%; } }`

把第 3 段的 lightbox 规则**迁移**进新的 `@media (max-width:768px)` 块（保留 lightbox 移动适配）：
```css
  .lightboxInner { flex-direction: column; align-items: center; }
  .lightboxImg { max-width: 90vw; max-height: 60vh; }
  .lightboxInfo { width: 100%; }
```
（第 1、2 段功能已被新移动块覆盖，直接删除。`formRow.split` 若仍需要，迁移 `.formRow.split { grid-template-columns: 1fr; }` 进新块。）

- [ ] **Step 2: 构建 + lint**

Run:
```bash
pnpm --filter @geo/web build
pnpm --filter @geo/web lint
```
Expected: build 成功；lint 不新增 error。

- [ ] **Step 3: 全量视觉巡检**

手机尺寸逐页过一遍（外壳/底栏/更多/内容 master-detail/分段子页/各双栏/表格/弹窗/灯箱）；桌面（>768px）逐页回归（侧边栏/嵌套子页/双栏/滚动模型/灯箱）。

- [ ] **Step 4: Commit**

```bash
git add web/src/styles.css
git commit -m "feat(mobile): 清理旧媒体查询，统一收敛到 768px 断点" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 自查（计划层面）

- **Spec 覆盖：** useIsMobile（T1）、底栏+更多（T2/T4）、App 外壳+AppBar（T3/T4）、子页移动分段（T5/T6/T7）、内容 master-detail（T6/T7）、双栏堆叠/节点编排/表格横滚/工具栏横滚/弹窗全宽（T7）、触控字号/安全区（T4/T7）、删旧媒体查询（T8）、桌面零回归（移动样式全在 `@media`/`isMobile` 分支）、验证（每任务 build+视觉，T8 lint）。spec 各节均有对应任务。
- **占位符：** 无 TBD；组件/hook 给完整代码；CSS 给完整块；少量"类名以实际为准"已标注需 dev server 核对（`.editorToolbar`/`.peNodeList`）。
- **类型一致：** `isMobile`/`onReviewTabChange`/`onScopeChange`/`mobileView` 在 T3 调用、T5/T6 定义，签名一致；MobileNav props（activeNav/onNavigate/isAdmin/username/onLogout）T2 定义、T3 传入一致；NavKey 复用现有类型。
