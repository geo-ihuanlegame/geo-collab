# 图片库 UI 升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复图片网格滚动条，并为图片库添加卡片悬停效果、Lightbox 大图预览、骨架屏和侧边栏优化。

**Architecture:** 纯前端改动，只修改一个 TSX 组件和一个全局 CSS 文件。Lightbox 通过 `lightboxIndex` state（`number | null`）控制开闭，键盘监听通过 `useEffect` 挂载/卸载。

**Tech Stack:** React 19, TypeScript, lucide-react（已引入），CSS custom properties

---

## 文件改动一览

| 文件 | 改动 |
|------|------|
| `web/src/features/image-library/ImageLibraryWorkspace.tsx` | 新增 imports、state、useEffect、JSX（overlay、lightbox、骨架屏、空状态、侧边栏 badge） |
| `web/src/styles.css` | 在现有 `/* ── Image Library ──` 区块末尾追加约 110 行 CSS |

---

## CSS 变量速查（`web/src/styles.css` `:root`）

```
--accent: #5B5FE9   --accent-soft: #ECEDFB   --accent-deep: #4549C5
--fg: #1A1A1A       --fg-2: #5C5751           --fg-3: #9A958D
--paper: #FFFFFF    --cream: #F5F2EB           --cream-2: #EDE8DD
--hair: #E8E5DE     --hair-2: #D6D2C8
```

---

## Task 1：修复网格滚动条 + 自定义滚动条样式

**Files:**
- Modify: `web/src/styles.css`（约第 1989 行 `.imageLibraryGrid`）

- [ ] **Step 1：在 `.imageLibraryGrid` 规则追加 `min-height: 0` 和滚动条样式**

  找到 `web/src/styles.css` 中的：
  ```css
  .imageLibraryGrid {
    flex: 1; overflow-y: auto; padding: 16px;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px; align-content: start;
  }
  ```
  替换为：
  ```css
  .imageLibraryGrid {
    flex: 1; overflow-y: auto; padding: 16px;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px; align-content: start;
    min-height: 0;
    scrollbar-width: thin;
    scrollbar-color: var(--hair-2) transparent;
  }
  .imageLibraryGrid::-webkit-scrollbar { width: 5px; }
  .imageLibraryGrid::-webkit-scrollbar-track { background: transparent; }
  .imageLibraryGrid::-webkit-scrollbar-thumb { background: var(--hair-2); border-radius: 3px; }
  .imageLibraryGrid::-webkit-scrollbar-thumb:hover { background: var(--fg-3); }
  ```

- [ ] **Step 2：TypeCheck**

  ```bash
  pnpm --filter @geo/web typecheck
  ```
  预期：0 errors

- [ ] **Step 3：Commit**

  ```bash
  git add web/src/styles.css
  git commit -m "fix: 修复图片库网格滚动条（min-height: 0 + 自定义滚动条样式）"
  ```

---

## Task 2：卡片悬停视觉升级

**Files:**
- Modify: `web/src/styles.css`（`.imageLibraryCard`、`.imageLibraryCardImg` 区域）
- Modify: `web/src/features/image-library/ImageLibraryWorkspace.tsx`（卡片 JSX 内 `.imageLibraryCardImg`）

- [ ] **Step 1：在 `styles.css` 追加卡片悬停 CSS**

  在文件末尾的 `/* ── Image Library ──` 区块末尾（约第 2043 行之后）追加：
  ```css
  /* ── Image Library: Card Hover ── */
  .imageLibraryCardImg { cursor: pointer; }
  .imageLibraryCardImg img { transition: transform 0.25s ease; }
  .imageLibraryCard { transition: border-color 0.2s, box-shadow 0.2s; }
  .imageLibraryCard:hover { border-color: var(--accent); box-shadow: 0 2px 12px rgba(91,95,233,0.15); }
  .imageLibraryCard:hover .imageLibraryCardImg img { transform: scale(1.04); }

  .imageLibraryCardOverlay {
    position: absolute; inset: 0;
    background: linear-gradient(to top, rgba(0,0,0,0.55) 0%, transparent 55%);
    opacity: 0; transition: opacity 0.2s;
    display: flex; align-items: flex-end; padding: 8px;
    pointer-events: none;
  }
  .imageLibraryCard:hover .imageLibraryCardOverlay { opacity: 1; }
  .imageLibraryCardOverlayName {
    font-size: 11px; color: #fff; font-weight: 500;
    text-shadow: 0 1px 3px rgba(0,0,0,0.5);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%;
  }
  ```

- [ ] **Step 2：在 `ImageLibraryWorkspace.tsx` 的 `.imageLibraryCardImg` 内添加覆盖层 div**

  找到组件中 `.imageLibraryCardImg` 的 JSX（约第 202 行）：
  ```jsx
  <div className="imageLibraryCardImg">
    <img src={img.url} alt={img.filename} loading="lazy" />
  </div>
  ```
  替换为：
  ```jsx
  <div className="imageLibraryCardImg">
    <img src={img.url} alt={img.filename} loading="lazy" />
    <div className="imageLibraryCardOverlay">
      <span className="imageLibraryCardOverlayName">{img.filename}</span>
    </div>
  </div>
  ```

- [ ] **Step 3：TypeCheck**

  ```bash
  pnpm --filter @geo/web typecheck
  ```
  预期：0 errors

- [ ] **Step 4：Commit**

  ```bash
  git add web/src/styles.css web/src/features/image-library/ImageLibraryWorkspace.tsx
  git commit -m "feat: 图片库卡片悬停升级（scale + 渐变遮罩 + 文件名浮层）"
  ```

---

## Task 3：Lightbox 大图预览

**Files:**
- Modify: `web/src/features/image-library/ImageLibraryWorkspace.tsx`
- Modify: `web/src/styles.css`

### 3a. TSX — 新增 imports、state、键盘 useEffect、派生变量

- [ ] **Step 1：更新 imports（顶部第 2 行）**

  找到：
  ```typescript
  import { MoreHorizontal, Plus, Trash2, Upload, Pencil } from "lucide-react";
  ```
  替换为：
  ```typescript
  import { MoreHorizontal, Plus, Trash2, Upload, Pencil, ChevronLeft, ChevronRight, X } from "lucide-react";
  ```

- [ ] **Step 2：在现有 state 声明块末尾（第 35 行 `editSaving` 之后）添加 lightbox state**

  在 `const [editSaving, setEditSaving] = useState(false);` 后新增一行：
  ```typescript
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  ```

- [ ] **Step 3：在现有 `useEffect`（menuRef click outside 监听）之后添加键盘 useEffect**

  在第 45 行 `}, []);`（menuRef effect 结束）后插入：
  ```typescript
  useEffect(() => {
    if (lightboxIndex === null) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") { setLightboxIndex(null); return; }
      if (e.key === "ArrowLeft") setLightboxIndex((i) => i === null ? null : (i - 1 + images.length) % images.length);
      if (e.key === "ArrowRight") setLightboxIndex((i) => i === null ? null : (i + 1) % images.length);
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [lightboxIndex, images.length]);
  ```

- [ ] **Step 4：在 `return (` 之前（第 152 行）添加派生变量**

  ```typescript
  const lightboxImage = lightboxIndex !== null ? (images[lightboxIndex] ?? null) : null;
  ```

### 3b. TSX — 卡片点击绑定

- [ ] **Step 5：将 `.map((img) =>` 改为 `.map((img, idx) =>`**

  找到（约第 200 行）：
  ```jsx
  {images.map((img) => (
    <div key={img.id} className="imageLibraryCard">
  ```
  替换为：
  ```jsx
  {images.map((img, idx) => (
    <div key={img.id} className="imageLibraryCard">
  ```

- [ ] **Step 6：给 `.imageLibraryCardImg` 绑定点击事件**

  找到：
  ```jsx
  <div className="imageLibraryCardImg">
    <img src={img.url} alt={img.filename} loading="lazy" />
    <div className="imageLibraryCardOverlay">
      <span className="imageLibraryCardOverlayName">{img.filename}</span>
    </div>
  </div>
  ```
  替换为：
  ```jsx
  <div className="imageLibraryCardImg" onClick={() => setLightboxIndex(idx)}>
    <img src={img.url} alt={img.filename} loading="lazy" />
    <div className="imageLibraryCardOverlay">
      <span className="imageLibraryCardOverlayName">{img.filename}</span>
    </div>
  </div>
  ```

### 3c. TSX — Lightbox JSX

- [ ] **Step 7：在 `editingImage` Modal 之后（约第 337 行 `</div>` 之前）插入 Lightbox JSX**

  在组件 return 最后的 `</div>` 之前（紧接在 `{editingImage && ( ... )}` 块之后）插入：
  ```jsx
  {lightboxImage && (
    <div className="lightboxOverlay" onClick={() => setLightboxIndex(null)}>
      <div className="lightboxInner" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="lightboxClose" onClick={() => setLightboxIndex(null)}>
          <X size={20} />
        </button>
        <img className="lightboxImg" src={lightboxImage.url} alt={lightboxImage.filename} />
        <div className="lightboxInfo">
          <p className="lightboxInfoName">{lightboxImage.filename}</p>
          {lightboxImage.width != null && lightboxImage.height != null && (
            <p className="lightboxInfoDim">{lightboxImage.width} × {lightboxImage.height}</p>
          )}
          {lightboxImage.tags.length > 0 && (
            <div className="lightboxInfoTags">
              {lightboxImage.tags.map((tag) => (
                <span key={tag} className="lightboxTag">{tag}</span>
              ))}
            </div>
          )}
          {lightboxImage.description && (
            <p className="lightboxInfoDesc">{lightboxImage.description}</p>
          )}
        </div>
      </div>
      {images.length > 1 && (
        <>
          <button
            type="button"
            className="lightboxArrow lightboxArrowLeft"
            onClick={(e) => {
              e.stopPropagation();
              setLightboxIndex((i) => i === null ? null : (i - 1 + images.length) % images.length);
            }}
          >
            <ChevronLeft size={28} />
          </button>
          <button
            type="button"
            className="lightboxArrow lightboxArrowRight"
            onClick={(e) => {
              e.stopPropagation();
              setLightboxIndex((i) => i === null ? null : (i + 1) % images.length);
            }}
          >
            <ChevronRight size={28} />
          </button>
        </>
      )}
    </div>
  )}
  ```

### 3d. CSS — Lightbox 样式

- [ ] **Step 8：在 `styles.css` 末尾追加 Lightbox CSS**

  ```css
  /* ── Lightbox ── */
  .lightboxOverlay {
    position: fixed; inset: 0; z-index: 1000;
    background: rgba(0,0,0,0.85);
    backdrop-filter: blur(6px);
    display: flex; align-items: center; justify-content: center;
    padding: 24px;
  }
  .lightboxInner {
    position: relative;
    display: flex; align-items: flex-start; gap: 24px;
    max-width: 90vw; max-height: 90vh;
  }
  .lightboxClose {
    position: absolute; top: -40px; right: 0;
    background: none; border: none; color: rgba(255,255,255,0.75);
    cursor: pointer; padding: 6px; border-radius: 6px;
    display: grid; place-items: center;
    transition: color 0.15s;
  }
  .lightboxClose:hover { color: #fff; }
  .lightboxImg {
    max-width: min(70vw, 900px); max-height: 85vh;
    object-fit: contain; border-radius: 8px;
    box-shadow: 0 8px 40px rgba(0,0,0,0.5);
    flex-shrink: 0;
  }
  .lightboxInfo {
    width: 220px; flex-shrink: 0;
    display: flex; flex-direction: column; gap: 10px;
    padding-top: 4px;
  }
  .lightboxInfoName {
    font-size: 14px; font-weight: 600; color: #fff;
    word-break: break-all; line-height: 1.4;
  }
  .lightboxInfoDim { font-size: 12px; color: rgba(255,255,255,0.55); }
  .lightboxInfoTags { display: flex; flex-wrap: wrap; gap: 6px; }
  .lightboxTag {
    font-size: 11px; padding: 3px 8px; border-radius: 4px;
    background: rgba(255,255,255,0.15); color: #fff;
  }
  .lightboxInfoDesc { font-size: 12px; color: rgba(255,255,255,0.7); line-height: 1.55; }
  .lightboxArrow {
    position: fixed; top: 50%; transform: translateY(-50%);
    background: rgba(0,0,0,0.45); border: none; color: #fff;
    border-radius: 50%; width: 44px; height: 44px;
    display: grid; place-items: center;
    cursor: pointer; transition: background 0.15s;
    z-index: 1001;
  }
  .lightboxArrow:hover { background: rgba(0,0,0,0.7); }
  .lightboxArrowLeft { left: 20px; }
  .lightboxArrowRight { right: 20px; }
  @media (max-width: 640px) {
    .lightboxInner { flex-direction: column; align-items: center; }
    .lightboxImg { max-width: 90vw; max-height: 60vh; }
    .lightboxInfo { width: 100%; }
  }
  ```

- [ ] **Step 9：TypeCheck**

  ```bash
  pnpm --filter @geo/web typecheck
  ```
  预期：0 errors

- [ ] **Step 10：Commit**

  ```bash
  git add web/src/styles.css web/src/features/image-library/ImageLibraryWorkspace.tsx
  git commit -m "feat: 图片库 Lightbox 大图预览（点击图片/键盘左右/Esc 关闭）"
  ```

---

## Task 4：侧边栏优化（活跃指示器 + 图片数量 badge）

**Files:**
- Modify: `web/src/styles.css`
- Modify: `web/src/features/image-library/ImageLibraryWorkspace.tsx`

- [ ] **Step 1：在 `styles.css` 追加侧边栏 CSS**

  在已有的 `/* ── Image Library ──` 样式后追加：
  ```css
  /* ── Image Library: Sidebar Upgrade ── */
  .imageLibraryCatBtn.active {
    border-left: 3px solid var(--accent);
    padding-left: 11px;
  }
  .imageLibraryCatBtnRow {
    display: flex; align-items: center; justify-content: space-between;
    width: 100%;
  }
  .imageLibraryCatCount {
    font-size: 10px; padding: 1px 6px; border-radius: 10px;
    background: var(--accent-soft); color: var(--accent-deep);
    font-weight: 600; flex-shrink: 0; margin-left: 4px;
  }
  ```

- [ ] **Step 2：更新侧边栏栏目按钮 JSX**

  找到（约第 180 行）：
  ```jsx
  <button
    key={cat.id}
    type="button"
    className={`imageLibraryCatBtn${selectedCategoryId === cat.id ? " active" : ""}`}
    onClick={() => setSelectedCategoryId(cat.id)}
  >
    <span className="imageLibraryCatName">{cat.name}</span>
    <span className="imageLibraryCatBucket">{cat.bucket_name}</span>
  </button>
  ```
  替换为：
  ```jsx
  <button
    key={cat.id}
    type="button"
    className={`imageLibraryCatBtn${selectedCategoryId === cat.id ? " active" : ""}`}
    onClick={() => setSelectedCategoryId(cat.id)}
  >
    <div className="imageLibraryCatBtnRow">
      <span className="imageLibraryCatName">{cat.name}</span>
      {selectedCategoryId === cat.id && images.length > 0 && (
        <span className="imageLibraryCatCount">{images.length}</span>
      )}
    </div>
    <span className="imageLibraryCatBucket">{cat.bucket_name}</span>
  </button>
  ```

- [ ] **Step 3：TypeCheck**

  ```bash
  pnpm --filter @geo/web typecheck
  ```
  预期：0 errors

- [ ] **Step 4：Commit**

  ```bash
  git add web/src/styles.css web/src/features/image-library/ImageLibraryWorkspace.tsx
  git commit -m "feat: 图片库侧边栏升级（active 左边框指示器 + 图片数量 badge）"
  ```

---

## Task 5：骨架屏 + 空状态

**Files:**
- Modify: `web/src/features/image-library/ImageLibraryWorkspace.tsx`
- Modify: `web/src/styles.css`

- [ ] **Step 1：更新 imports（顶部）添加 `Images` 图标**

  找到：
  ```typescript
  import { MoreHorizontal, Plus, Trash2, Upload, Pencil, ChevronLeft, ChevronRight, X } from "lucide-react";
  ```
  替换为：
  ```typescript
  import { MoreHorizontal, Plus, Trash2, Upload, Pencil, ChevronLeft, ChevronRight, X, Images } from "lucide-react";
  ```

- [ ] **Step 2：替换网格区域的 loading/empty JSX**

  找到（约第 195 行）：
  ```jsx
  <div className="imageLibraryGrid">
    {loading && <p className="imageLibraryLoading">加载中...</p>}
    {!loading && images.length === 0 && (
      <p className="imageLibraryEmpty">暂无图片，上传第一张吧</p>
    )}
    {images.map((img, idx) => (
  ```
  替换为：
  ```jsx
  <div className="imageLibraryGrid">
    {loading && Array.from({ length: 8 }).map((_, i) => (
      <div key={i} className="imageLibraryCardSkeleton" />
    ))}
    {!loading && images.length === 0 && selectedCategoryId !== null && (
      <div className="imageLibraryEmptyState">
        <Images size={40} strokeWidth={1.2} />
        <p className="imageLibraryEmptyTitle">这个栏目还没有图片</p>
        <p>点击右上角「上传图片」开始添加</p>
      </div>
    )}
    {!loading && images.map((img, idx) => (
  ```

- [ ] **Step 3：在 `styles.css` 追加骨架屏 & 空状态 CSS**

  ```css
  /* ── Image Library: Skeleton & Empty State ── */
  .imageLibraryCardSkeleton {
    border-radius: 8px; aspect-ratio: 1;
    background: linear-gradient(90deg, var(--cream-2) 25%, var(--cream) 50%, var(--cream-2) 75%);
    background-size: 200% 100%;
    animation: imgSkeletonShimmer 1.4s ease-in-out infinite;
  }
  @keyframes imgSkeletonShimmer {
    0%   { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }
  .imageLibraryEmptyState {
    grid-column: 1 / -1;
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    padding: 60px 20px;
    color: var(--fg-3);
  }
  .imageLibraryEmptyTitle { font-size: 14px; font-weight: 500; color: var(--fg-2); }
  .imageLibraryEmptyState > p:not(.imageLibraryEmptyTitle) { font-size: 12px; }
  ```

- [ ] **Step 4：TypeCheck**

  ```bash
  pnpm --filter @geo/web typecheck
  ```
  预期：0 errors

- [ ] **Step 5：Commit**

  ```bash
  git add web/src/styles.css web/src/features/image-library/ImageLibraryWorkspace.tsx
  git commit -m "feat: 图片库骨架屏 + 优化空状态提示"
  ```

---

## 验证清单（全部任务完成后）

- [ ] `pnpm --filter @geo/web typecheck` — 0 errors
- [ ] 上传多张图片后，网格区域出现细滚动条（5px 宽）
- [ ] 鼠标 hover 卡片：图片轻微放大 + 文件名渐入 + 边框变蓝
- [ ] 点击图片区域：Lightbox 打开，大图居中，右侧信息面板（文件名、尺寸、标签、描述）
- [ ] Lightbox 内 ← / → 箭头切换图片；键盘 ArrowLeft / ArrowRight 同效果
- [ ] Lightbox 内按 Escape、点击遮罩背景或 ✕ 按钮均可关闭
- [ ] 切换栏目时出现 8 个骨架屏动画格
- [ ] 空栏目显示图标 + 「这个栏目还没有图片」文字
- [ ] 当前选中栏目：左侧蓝色 3px border + 图片数量 badge
