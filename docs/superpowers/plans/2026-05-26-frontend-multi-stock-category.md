# 前端多栏目配图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把文章配图栏目从单选（`stock_category_id: number | null`）升级为多选（`stock_category_ids: number[]`），UI 从 `<select>` 改为 checkbox 组，并同步发送新字段给后端。

**Architecture:** 改动集中在三个文件：`types.ts` 定义新类型、`ContentWorkspace.tsx` 管理状态与保存逻辑、UI 渲染改为 checkbox 组。后端已支持 `stock_category_ids`（`ArticleUpdate` 已有该字段），前端只需对齐。旧的 `stock_category_id` 字段后端向后兼容，前端不再读写。

**Tech Stack:** React 19, TypeScript，原生 HTML（无额外组件库）

---

## 受影响文件一览

| 文件 | 改动性质 |
|---|---|
| `web/src/types.ts` | 修改 `Article`、`Draft`、`ArticleUpdatePayload` 类型 |
| `web/src/features/content/ContentWorkspace.tsx` | 修改 Draft 初始化、load、save 逻辑 + UI checkbox |
| `web/src/components/editor/EditorToolbar.tsx` | prop 名保持不变，调用方传参逻辑微调 |

---

## Task 1：更新 TypeScript 类型

**Files:**
- Modify: `web/src/types.ts`

### 背景

当前：
- `Article.stock_category_id: number | null` — 后端已新增 `stock_category_ids: number[]`，前端类型未跟上
- `Draft.stock_category_id: number | null` — 内部草稿状态，改为数组
- `ArticleUpdatePayload.stock_category_id?: number | null` — 改为发送 `stock_category_ids`

- [ ] **Step 1: 修改 Article 类型**

在 `types.ts` 找到 `Article` 定义，增加 `stock_category_ids` 字段（保留旧字段兼容后端旧响应，标注 deprecated）：

```typescript
// 修改前
export type Article = ArticleSummary & {
  content_json: Record<string, unknown>;
  content_html: string;
  plain_text: string;
  body_assets: ArticleBodyAsset[];
  stock_category_id: number | null;
  ai_checking: boolean;
  ai_format_error: string | null;
};

// 修改后
export type Article = ArticleSummary & {
  content_json: Record<string, unknown>;
  content_html: string;
  plain_text: string;
  body_assets: ArticleBodyAsset[];
  /** @deprecated 使用 stock_category_ids */
  stock_category_id: number | null;
  stock_category_ids: number[];
  ai_checking: boolean;
  ai_format_error: string | null;
};
```

- [ ] **Step 2: 修改 Draft 类型**

```typescript
// 修改前
export type Draft = {
  id: number | null;
  title: string;
  author: string;
  cover_asset_id: string | null;
  status: string;
  version: number | null;
  stock_category_id: number | null;
};

// 修改后
export type Draft = {
  id: number | null;
  title: string;
  author: string;
  cover_asset_id: string | null;
  status: string;
  version: number | null;
  stock_category_ids: number[];
};
```

- [ ] **Step 3: 修改 ArticleUpdatePayload 类型**

```typescript
// 修改前
export type ArticleUpdatePayload = {
  title?: string;
  author?: string | null;
  cover_asset_id?: string | null;
  content_json?: Record<string, unknown>;
  content_html?: string;
  plain_text?: string;
  word_count?: number;
  status?: string;
  version?: number | null;
  stock_category_id?: number | null;
  client_request_id?: string;
};

// 修改后
export type ArticleUpdatePayload = {
  title?: string;
  author?: string | null;
  cover_asset_id?: string | null;
  content_json?: Record<string, unknown>;
  content_html?: string;
  plain_text?: string;
  word_count?: number;
  status?: string;
  version?: number | null;
  stock_category_ids?: number[];
  client_request_id?: string;
};
```

- [ ] **Step 4: 运行 TypeScript 类型检查，确认只有 ContentWorkspace 报错（其他文件无引用）**

```bash
pnpm --filter @geo/web typecheck 2>&1 | head -40
```

预期：只有 `ContentWorkspace.tsx` 有类型错误（因为它还用旧字段），其余文件无报错。

---

## Task 2：更新 ContentWorkspace 状态与保存逻辑

**Files:**
- Modify: `web/src/features/content/ContentWorkspace.tsx`

### 背景

`ContentWorkspace` 里有 4 处用到 `stock_category_id`：
1. `emptyDraft()` — 初始草稿状态
2. 打开文章时从 `detail` 读取（两处：`handleOpenDetail` + `handleSelectArticle`）
3. 关闭并新建时重置（`handleCloseAndNew`）
4. 保存时写入 payload

- [ ] **Step 1: 更新 `emptyDraft()` 初始值**

找到函数（约第 40-50 行）：

```typescript
// 修改前
function emptyDraft(): Draft {
  return {
    id: null,
    title: "",
    author: "",
    cover_asset_id: null,
    status: "draft",
    version: null,
    stock_category_id: null,
  };
}

// 修改后
function emptyDraft(): Draft {
  return {
    id: null,
    title: "",
    author: "",
    cover_asset_id: null,
    status: "draft",
    version: null,
    stock_category_ids: [],
  };
}
```

- [ ] **Step 2: 更新从后端加载文章时的状态填充**

共有两处（`handleOpenDetail` 约第 440-460 行，`handleSelectArticle` 约第 585-600 行），均改为读取 `stock_category_ids`：

```typescript
// 修改前（两处均相同）
stock_category_id: detail.stock_category_id ?? null,

// 修改后（两处均相同）
stock_category_ids: detail.stock_category_ids ?? [],
```

注意：`handleCloseAndNew` 里（约第 575-581 行）有一处 `stock_category_id: null` 是临时占位，会立刻被 `handleSelectArticle` 内的 load 覆盖，改为 `stock_category_ids: []`：

```typescript
// 修改前
stock_category_id: null,

// 修改后
stock_category_ids: [],
```

另外 `handleOpenDetail` 里 saved 回调（约第 620-627 行）也有一处：

```typescript
// 修改前
stock_category_id: saved.stock_category_id ?? null,

// 修改后
stock_category_ids: saved.stock_category_ids ?? [],
```

- [ ] **Step 3: 更新保存时发送的 payload**

找到保存逻辑（约第 650-665 行）：

```typescript
// 修改前
const base = {
  ...
  stock_category_id: draft.stock_category_id,
};

// 修改后
const base = {
  ...
  stock_category_ids: draft.stock_category_ids,
};
```

- [ ] **Step 4: 更新传给 EditorToolbar 的 prop**

找到（约第 1192 行）：

```typescript
// 修改前
stockCategorySelected={!!draft.stock_category_id}

// 修改后
stockCategorySelected={draft.stock_category_ids.length > 0}
```

- [ ] **Step 5: 运行类型检查，确认 ContentWorkspace 无报错**

```bash
pnpm --filter @geo/web typecheck 2>&1 | head -40
```

预期：0 errors。

- [ ] **Step 6: 提交**

```bash
git add web/src/types.ts web/src/features/content/ContentWorkspace.tsx
git commit -m "feat: 前端类型和状态升级为多栏目 stock_category_ids"
```

---

## Task 3：UI 改为 Checkbox 多选组

**Files:**
- Modify: `web/src/features/content/ContentWorkspace.tsx`（仅 JSX 部分）

### 背景

当前 UI（约第 1142-1154 行）：
```jsx
{stockCategories.length > 0 && (
  <label>
    配图栏目
    <select
      value={draft.stock_category_id ?? ""}
      onChange={(e) => setDraft({ ...draft, stock_category_id: e.target.value ? Number(e.target.value) : null })}
    >
      <option value="">— 不自动配图 —</option>
      {stockCategories.map((cat) => (
        <option key={cat.id} value={cat.id}>{cat.name}</option>
      ))}
    </select>
  </label>
)}
```

改为 checkbox 组，每个栏目一个 checkbox，可多选。

- [ ] **Step 1: 替换 UI 为 checkbox 组**

用下面的 JSX 替换上述代码块（注意缩进与周围代码保持一致）：

```jsx
{stockCategories.length > 0 && (
  <div>
    <span style={{ fontSize: 12, color: "#666", display: "block", marginBottom: 4 }}>配图栏目</span>
    <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 12px" }}>
      {stockCategories.map((cat) => {
        const checked = draft.stock_category_ids.includes(cat.id);
        return (
          <label key={cat.id} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13, cursor: "pointer", fontWeight: "normal" }}>
            <input
              type="checkbox"
              checked={checked}
              onChange={(e) => {
                const next = e.target.checked
                  ? [...draft.stock_category_ids, cat.id]
                  : draft.stock_category_ids.filter((id) => id !== cat.id);
                setDraft({ ...draft, stock_category_ids: next });
              }}
            />
            {cat.name}
          </label>
        );
      })}
    </div>
  </div>
)}
```

- [ ] **Step 2: 运行类型检查确认无报错**

```bash
pnpm --filter @geo/web typecheck 2>&1 | head -20
```

预期：0 errors。

- [ ] **Step 3: 启动前端开发服务器，手动验证**

```bash
pnpm --filter @geo/web dev
```

验证步骤：
1. 打开任意文章
2. 右侧面板"配图栏目"显示为 checkbox 列表（每个栏目一行）
3. 勾选多个栏目 → 保存 → 重新打开文章 → checkbox 状态仍然是勾选状态
4. 取消所有勾选 → 保存 → 重新打开 → 全部 checkbox 未选中
5. 勾选至少一个栏目时，工具栏 AI 格式按钮显示"AI格式·配图"；全部取消时显示"AI 格式"

- [ ] **Step 4: 提交**

```bash
git add web/src/features/content/ContentWorkspace.tsx
git commit -m "feat: 配图栏目 UI 改为 checkbox 多选组"
```

---

## Self-Review

**Spec 覆盖检查：**
- ✅ `Article` 类型新增 `stock_category_ids: number[]`
- ✅ `Draft` 类型从单值改为数组
- ✅ `ArticleUpdatePayload` 发送 `stock_category_ids`
- ✅ UI 从单选 `<select>` 改为 checkbox 组
- ✅ EditorToolbar `stockCategorySelected` prop 用新逻辑计算
- ✅ 加载文章时从 `detail.stock_category_ids` 读取

**Placeholder 检查：** 无 TBD / TODO / "适当处理" 等占位符。

**类型一致性：**
- `Draft.stock_category_ids: number[]` → 初始化 `[]`，load 时 `detail.stock_category_ids ?? []`，save 时 `draft.stock_category_ids`，checkbox onChange 生成 `number[]` — 全链路一致。
- `ArticleUpdatePayload.stock_category_ids?: number[]` — 与后端 schema 一致。
