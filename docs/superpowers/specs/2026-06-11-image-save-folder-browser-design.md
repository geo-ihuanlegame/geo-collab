# 编辑器图片保存：文件浏览式弹框 + 正文选图高亮 设计规格

**日期：** 2026-06-11
**状态：** 已批准（设计）
**改动范围：** 前端（内容管理 tab：编辑器工具栏、正文选图样式、保存弹框）+ 后端（image_library 删分类端点、bucket 自动派名、MinIO 删桶）

> 本规格只覆盖**编辑器里的"图片保存"这条链路**。整页图片库（`ImageLibraryWorkspace`）本次**不动**。

---

## 背景 / 现状

运营在内容管理 tab 的编辑器里，把正文中的一张图片存进图片库时，遇到两个体验问题：

1. **选中反馈太弱**：点选正文里的图片后，唯一的视觉反馈是工具栏最右那颗按钮从禁用变可点；被选中的图片**本身没有任何明显高亮**，肉眼很难确认到底选没选中。
2. **保存交互不够顺手**：当前 [`ImageSaveDialog`](../../web/src/components/editor/ImageSaveDialog.tsx) 是一个小弹框——主推/陪衬 tab → 单选文件夹的 radio 列表 → 文件名。运营希望换成"文件浏览式"的保存框（见下方截图描述），并且能在弹框里**直接新建 / 删除文件夹**。

此外工具栏里那颗按钮目前是 **"图片保存" 文字**，运营希望换成常用的**磁盘存盘图标**。

> "文件夹"是对用户友好的叫法，技术上就是一个 **MinIO bucket = 一个 `StockCategory`（栏目）**。现有数据模型层级：**主推/陪衬两类（`StockCategory.kind`）→ 文件夹（`StockCategory` / bucket）→ 图片（`StockImage`）**。

### 目标弹框形态（运营提供的截图）

- 标题：`保存至图片库`
- 顶部：`←` `→` 返回/前进 + 面包屑 `图片库 > 主推游戏 > 餐厅养成记`；右上角 `新建文件夹` / `删除文件夹` 两颗按钮。
- 左侧栏：`主推游戏` / `陪衬游戏`（kind 切换，当前项高亮）。
- 中间网格：进入某个文件夹后，展示该文件夹里**已有的图片**（参考用，如 `餐厅养成记01…07`）。
- 底部：`图片名称` 输入框（placeholder 示例 `餐厅养成记 · 封面`）+ `保存` / `取消`。

---

## 决策记录（运营澄清，务必遵守）

| 决策点 | 结论 |
|--------|------|
| "选中提示太弱"指哪里 | **编辑器正文里被选中的图片本身**，加显眼蓝色高亮（不是弹框缩略图） |
| 弹框文件夹导航 | **严格照截图两层下钻**：左栏选 kind → 网格第一层显示文件夹 → 点击下钻看图片，面包屑/返回键回上一层 |
| 删除非空文件夹 | **禁止**：文件夹里还有图片时不允许删，后端回 409，前端提示先清空 |
| 工具栏按钮 | 文字"图片保存" → Lucide `Save`（磁盘存盘图标），保留悬浮 `title` |
| 新建文件夹的 bucket 名 | **不向用户暴露 bucket**，只填文件夹名字，bucket 名由后端按拼音自动派生 |
| 整页图片库 | 本次**不改**，仍用其自带"新建栏目（手填 bucket）"流程 |

---

## 改动一：工具栏按钮改图标

文件：[`web/src/components/editor/EditorToolbar.tsx`](../../web/src/components/editor/EditorToolbar.tsx)（约 L83–90）

- 把末尾那颗文字按钮 `图片保存` 换成图标按钮：图标用 Lucide `Save`（顶部 import 增加 `Save`）。
- `title` 设为 `imageSelected ? "图片保存到图库" : "先选中正文中的图片"`（沿用现有动态提示）。
- `disabled={!imageSelected}`、`onClick={onSaveImage}` **逻辑不变**。
- 组件 props（`onSaveImage` / `imageSelected`）**不变**，`ContentWorkspace` 的 wiring 不动。

---

## 改动二：正文选中图片的高亮（纯 CSS）

文件：[`web/src/styles.css`](../../web/src/styles.css)（在 `.editorSurface img` 即约 L887 附近新增）

- Tiptap/ProseMirror 选中一个图片节点时，会自动给该节点元素加 `ProseMirror-selectednode` 类。当前样式表对它**没有任何规则**，所以选中态几乎不可见。
- 新增规则，给选中图片本身加明显高亮（蓝色描边 + 外发光）：

```css
.editorSurface img.ProseMirror-selectednode {
  outline: 3px solid var(--accent);
  outline-offset: 2px;
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 25%, transparent);
}
```

- 纯样式改动，零逻辑。`--accent` 为现有主题蓝。若 `color-mix` 兼容性存疑，回退为半透明固定色（实现时确认目标浏览器，本项目面向桌面 Chromium，`color-mix` 可用）。

---

## 改动三：重写保存弹框为"文件浏览式"

文件：[`web/src/components/editor/ImageSaveDialog.tsx`](../../web/src/components/editor/ImageSaveDialog.tsx)（整体重写，props 接口不变）

### Props（不变）
`{ imageSrc, onClose, onSaved, onError }` —— `ContentWorkspace` 调用方不改。

### 内部状态
- `kind: "main" | "companion"`（默认 `"main"`，照截图主推优先）
- `folders: StockCategory[]`（当前 kind 下的文件夹列表）
- `currentFolder: StockCategory | null`（`null` = 文件夹列表层；非空 = 已下钻进某文件夹，展示其图片）
- `images: StockImage[]`（`currentFolder` 内的图片，仅参考展示）
- `filename: string`、`saving`、`error`
- 新建文件夹的轻量内联输入态：`creating: boolean`、`newFolderName: string`

### 布局与交互
- **左栏**：`主推游戏` / `陪衬游戏` 两个按钮，切 kind → 重新 `listCategories(kind)`、`currentFolder` 复位为 `null`。
- **顶部**：
  - 面包屑 `图片库 > {kind 中文} > {currentFolder?.name}`。点 `图片库` 或 kind 文字 → 回文件夹列表层（`currentFolder=null`）。
  - `←` 返回键：在文件夹内时回到文件夹列表层；在列表层时禁用。（`→` 前进可选，先做成禁用占位，避免维护额外历史栈——YAGNI。）
  - 右上 `新建文件夹`（始终可点）/ `删除文件夹`（仅 `currentFolder != null` 时可点）。
- **中间网格**：
  - `currentFolder === null`：渲染**文件夹卡**（Lucide `Folder` 图标 + 文件夹名），点击 → `setCurrentFolder(folder)` 并 `listImages({category_id})` 下钻。
  - `currentFolder != null`：渲染该文件夹**已有图片缩略图**（`img.url`，参考用，不可选）。空文件夹给空状态文案。
- **底部**：`图片名称` 输入框 + `保存` / `取消`。
  - `保存` **仅在 `currentFolder != null` 时可点**（未进文件夹时禁用，提示"请先进入一个文件夹"）。
  - 保存链路沿用现有逻辑：`fetch(imageSrc)→Blob→File(命名)→uploadImage({category_id: currentFolder.id, file})`；成功 `onSaved` + `onClose`，失败 `setError`+`onError`（跨源 CORS 提示保留）。

### 新建文件夹（内联，不嵌套大弹框）
- 点 `新建文件夹` → 顶部/网格上方出现一行内联输入（文件夹名）+ `确认`/`取消`。
- `确认` → `createCategory({ name, kind })`（**不传 bucket_name**，后端自动派名）→ 刷新 `folders`，并自动 `setCurrentFolder(新建的)` 下钻。
- 失败 → `error` + `onError`。

### 删除文件夹
- 点 `删除文件夹`（当前在某文件夹内）→ `window.confirm("确定删除文件夹「{name}」？")` → `deleteCategory(currentFolder.id)`。
- 成功 → 回文件夹列表层 + 刷新 `folders` + toast。
- 后端 409（非空）→ `onError("该文件夹内还有图片，请先清空后再删除")`。

---

## 改动四：后端

### 4.1 MinIO 删桶
文件：[`server/app/modules/image_library/store.py`](../../server/app/modules/image_library/store.py)

```python
def remove_bucket(bucket_name: str) -> None:
    """删除空分桶。MinIO 仅允许删空桶，非空时 client 抛错——与"非空禁止删"语义天然一致。"""
    client = _client()
    client.remove_bucket(bucket_name)
```

### 4.2 删分类端点
文件：[`server/app/modules/image_library/router.py`](../../server/app/modules/image_library/router.py)

新增 `DELETE /api/image-library/categories/{category_id}`，`status_code=204`：

1. `cat = db.get(StockCategory, category_id)`；`None` → `HTTPException(404, "栏目不存在")`。
2. 该分类下还有图片 → `HTTPException(409, "该文件夹内还有图片，请先清空")`：
   `count = db.query(StockImage).filter(StockImage.category_id == category_id).count()`，`count>0` 即拒。
3. `minio_store.remove_bucket(cat.bucket_name)`（包 try/except：MinIO 删桶失败 → `HTTPException(500, ...)`；不静默吞，桶若意外非空这里也会抛）。
4. `db.delete(cat)` + `db.commit()`。
5. `add_audit_entry(action="stock_category.delete", target_type="stock_category", target_id=category_id, payload={"name": cat.name})`。

> 遵循本文件现有风格：直接用 `HTTPException`（不抛 `ConflictError`），与 `create_category` 的 409 一致。

### 4.3 创建分类 bucket 名可选 + 自动派名
文件：`router.py` 的 `CategoryCreate` + `create_category`

- `CategoryCreate.bucket_name: str | None = Field(default=None, max_length=63)`（由必填改可选）。
- `create_category` 逻辑：
  - 若 `payload.bucket_name` 非空：保持现有"显式 bucket + 撞名 409"路径不变。
  - 若为空：`bucket = service._unique_bucket_name(db, service.slugify_bucket(payload.name))` 自动派名（`_unique_bucket_name` 已保证唯一，不会撞 409）。
  - 其余（`ensure_bucket` 建桶、建行、审计、返回）不变。
- 复用 [`server/app/modules/image_library/service.py`](../../server/app/modules/image_library/service.py) 现成的 `slugify_bucket` / `_unique_bucket_name`，不重复造轮子。

---

## 改动五：前端 API 层

文件：[`web/src/api/image-library.ts`](../../web/src/api/image-library.ts)

- `createCategory`：把 `bucket_name` 由必填改为**可选**（`bucket_name?: string`）。
- 新增：
  ```ts
  export function deleteCategory(categoryId: number): Promise<void> {
    return api<void>(`/api/image-library/categories/${categoryId}`, { method: "DELETE" });
  }
  ```

> `ImageLibraryWorkspace` 仍传 `bucket_name`，行为不受影响。

---

## 不在本次范围

- 整页图片库 `ImageLibraryWorkspace` 的布局 / 新建栏目流程：**不改**。
- `→` 前进历史栈：先占位禁用，不实现。
- 文件夹卡显示封面缩略图 / 图片数角标：v1 只用文件夹图标 + 名字（避免对每个文件夹预拉图片列表）。
- 弹框内编辑文件夹（改名 / 改 kind）、图片改标签等管理动作：不做，留在整页图片库。

---

## 改动文件汇总

| 文件 | 改动 |
|------|------|
| `web/src/components/editor/EditorToolbar.tsx` | "图片保存"文字 → `Save` 图标按钮 |
| `web/src/styles.css` | 新增 `.editorSurface img.ProseMirror-selectednode` 高亮 |
| `web/src/components/editor/ImageSaveDialog.tsx` | 重写为文件浏览式弹框（两层下钻 + 新建/删除文件夹） |
| `web/src/api/image-library.ts` | `createCategory` bucket 可选 + 新增 `deleteCategory` |
| `server/app/modules/image_library/store.py` | 新增 `remove_bucket` |
| `server/app/modules/image_library/router.py` | 新增 `DELETE /categories/{id}` + `CategoryCreate.bucket_name` 可选 + 自动派名 |

---

## 测试

- **后端**（pytest，MySQL）：
  - `DELETE /categories/{id}`：成功删空文件夹（验证 DB 行删除 + 调到 `remove_bucket`，MinIO 用 mock/monkeypatch）；非空 → 409；不存在 → 404；审计记录写入。
  - `POST /categories` 不传 `bucket_name`：自动派生拼音 bucket、唯一、建桶成功；传了 `bucket_name` 时旧路径与 409 撞名行为不变。
  - MinIO 交互全程 monkeypatch `minio_store`（沿用现有测试套路，不真连 MinIO）。
- **前端**：`pnpm --filter @geo/web typecheck` + `build` 通过（前端无单测框架，typecheck+build 即门禁）。
- **手动验收**：正文点选图片有明显蓝框；工具栏磁盘图标可点；弹框两层下钻、新建/删除文件夹、命名保存全链路。

---

## 风险 / 注意

1. **MinIO 删桶非空会抛错**：4.2 已先在 DB 层用 `StockImage` 计数挡掉非空；即便有 DB 无记录的孤儿对象导致 `remove_bucket` 抛错，也走 500 显式报错、不静默。
2. **跨源图片 fetch**：保存链路对外链图仍可能被 CORS 挡（与现状一致）；同源 `/api/stock-images/*`、文章附件可正常取。本次不引入 `from-url` 兜底。
3. **bucket 自动派名仅走弹框新建这条路**：整页图片库仍手填 bucket，互不影响。
4. **`color-mix` 兼容性**：面向桌面 Chromium，可用；实现时若发现目标环境不支持，回退固定半透明蓝。
