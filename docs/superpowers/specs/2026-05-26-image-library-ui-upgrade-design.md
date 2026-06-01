# 图片库 UI 升级设计规格

**日期：** 2026-05-26  
**状态：** 已批准  
**改动范围：** 纯前端，无后端改动

---

## 背景

图片库当前存在两个问题：
1. 图片数量多时，网格区域不出现滚动条，导致底部内容不可见
2. 卡片样式较粗糙，缺乏悬停反馈和视觉层次感

本次升级在保留现有网格布局的基础上，修复滚动条、增强卡片交互效果，并新增 Lightbox 大图预览功能。

---

## 改动一：修复滚动条

**根因：** `.imageLibraryGrid` 是 flex 子元素 + grid 容器的组合，缺少 `min-height: 0`，浏览器不触发 `overflow-y: auto`。

**修复：**
```css
.imageLibraryGrid {
  /* 现有属性不变，新增 */
  min-height: 0;
  /* 自定义滚动条 */
  scrollbar-width: thin;
  scrollbar-color: #d0d0d0 transparent;
}
.imageLibraryGrid::-webkit-scrollbar { width: 5px; }
.imageLibraryGrid::-webkit-scrollbar-track { background: transparent; }
.imageLibraryGrid::-webkit-scrollbar-thumb { background: #d0d0d0; border-radius: 3px; }
.imageLibraryGrid::-webkit-scrollbar-thumb:hover { background: #b0b0b0; }
```

---

## 改动二：卡片视觉升级

### 悬停效果
- 图片区域 `transform: scale(1.04)` 轻微放大，配合 `overflow: hidden` 产生"画面充满"感
- 底部出现渐变遮罩（`linear-gradient(to top, rgba(0,0,0,0.55), transparent)`）
- 文件名白色文字从遮罩中淡入显示（`opacity` 过渡）
- 卡片 `border-color` 过渡到 `var(--accent)`

### 菜单按钮
现有 ⋯ 菜单按钮行为不变（hover 出现，点击展开编辑/删除下拉菜单）。

### CSS 新增
```css
.imageLibraryCardImg img { transition: transform 0.25s ease; }
.imageLibraryCard:hover .imageLibraryCardImg img { transform: scale(1.04); }
.imageLibraryCard { transition: border-color 0.2s; }
.imageLibraryCard:hover { border-color: var(--accent); }

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

### JSX 改动
在 `.imageLibraryCardImg` 内部、`img` 标签之后，添加覆盖层：
```jsx
<div className="imageLibraryCardOverlay">
  <span className="imageLibraryCardOverlayName">{img.filename}</span>
</div>
```
同时将 `.imageLibraryCard` 的点击事件绑定为打开 Lightbox（见下节），但 ⋯ 按钮的 `e.stopPropagation()` 阻止冒泡，避免误触。

---

## 改动三：Lightbox 大图预览

### 状态
```typescript
const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
```
用 `images[lightboxIndex]` 取当前预览图，`null` 表示关闭。

### 打开/关闭
- 点击 `.imageLibraryCardImg` 区域：`setLightboxIndex(images.indexOf(img))`
- 关闭：点击遮罩背景、点击 ✕ 按钮、按 Escape 键

### 左右切换
- 点击左箭头：`setLightboxIndex((i) => (i! - 1 + images.length) % images.length)`
- 点击右箭头：`setLightboxIndex((i) => (i! + 1) % images.length)`
- 键盘 `ArrowLeft` / `ArrowRight` 同效果
- 通过 `useEffect` 监听 `keydown`，在 `lightboxIndex !== null` 时激活

### 布局
```
┌────────────────────────────────────────────────────────┐
│  遮罩（rgba(0,0,0,0.85) + backdrop-filter: blur(6px)） │
│  ┌──────────────┐  ┌───────────────────────────┐       │
│  │              │  │  filename.jpg             │       │
│  │   大图        │  │  1920 × 1080              │       │
│  │  (max 85vw   │  │  ─────────────            │       │
│  │   max 85vh)  │  │  标签: [角色] [战斗]       │       │
│  │              │  │  描述: 图片描述文字...     │       │
│  └──────────────┘  └───────────────────────────┘       │
│   ← 左箭头                           右箭头 →           │
│                                            ✕ 关闭      │
└────────────────────────────────────────────────────────┘
```

右侧信息面板宽度约 220px，flex 布局；图片区 flex: 1。在窄屏下（< 640px）信息面板折叠到图片下方。

### CSS 新增（关键部分）
```css
.lightboxOverlay {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,0.85);
  backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center;
}
.lightboxInner {
  display: flex; align-items: center; gap: 24px;
  max-width: 90vw; max-height: 90vh;
  position: relative;
}
.lightboxImg {
  max-width: 70vw; max-height: 85vh;
  object-fit: contain; border-radius: 8px;
  box-shadow: 0 8px 40px rgba(0,0,0,0.5);
}
.lightboxInfo {
  width: 220px; flex-shrink: 0; color: #fff;
}
.lightboxClose {
  position: absolute; top: -40px; right: 0;
  background: none; border: none; color: rgba(255,255,255,0.75);
  cursor: pointer; padding: 8px; border-radius: 6px;
  transition: color 0.15s;
}
.lightboxClose:hover { color: #fff; }
.lightboxArrow {
  position: fixed; top: 50%; transform: translateY(-50%);
  /* left/right variants */
}
```

---

## 改动四：侧边栏小升级

### 活跃指示器
```css
.imageLibraryCatBtn.active {
  /* 现有 background 保留，新增 */
  border-left: 3px solid var(--accent);
  padding-left: 11px; /* 补偿 border 宽度 */
}
```

### 图片数量 Badge
仅对当前选中栏目显示 badge（已有 `images` 数据，无需新请求）：
```jsx
<span className="imageLibraryCatName">{cat.name}</span>
{selectedCategoryId === cat.id && images.length > 0 && (
  <span className="imageLibraryCatCount">{images.length}</span>
)}
```

---

## 改动五：加载骨架屏 & 空状态

### 骨架屏
`loading` 为 `true` 时，渲染 8 个骨架卡片替代文字：
```jsx
{loading && Array.from({ length: 8 }).map((_, i) => (
  <div key={i} className="imageLibraryCardSkeleton" />
))}
```
```css
.imageLibraryCardSkeleton {
  border-radius: 8px; aspect-ratio: 1;
  background: linear-gradient(90deg, var(--bg-2) 25%, var(--bg-1) 50%, var(--bg-2) 75%);
  background-size: 200% 100%;
  animation: skeletonShimmer 1.4s infinite;
}
@keyframes skeletonShimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
```

### 空状态
```jsx
{!loading && images.length === 0 && (
  <div className="imageLibraryEmptyState">
    <ImageIcon size={40} />
    <p>这个栏目还没有图片</p>
    <p>点击右上角「上传图片」开始添加</p>
  </div>
)}
```

---

## 改动文件汇总

| 文件 | 改动类型 |
|------|----------|
| `web/src/features/image-library/ImageLibraryWorkspace.tsx` | 新增 state、事件处理、Lightbox JSX、骨架屏、空状态 |
| `web/src/styles.css` | 新增约 80 行 CSS |

**无后端改动。**

---

## 验证方法

1. `pnpm --filter @geo/web dev` 启动前端开发服务器
2. 进入「图片库」页面
3. 验证：
   - 上传多张图片后，网格出现垂直滚动条
   - 鼠标悬停卡片，图片轻微放大 + 文件名浮层出现
   - 点击图片，Lightbox 打开，显示大图 + 右侧信息
   - 在 Lightbox 内用 ← → 键切换图片
   - 按 Escape / 点击遮罩关闭 Lightbox
   - 栏目加载时出现骨架屏动画
   - 空栏目显示图标 + 友好提示
   - active 栏目左侧有蓝色边框指示器 + 图片数量 badge
4. `pnpm --filter @geo/web typecheck` 通过，无类型错误
