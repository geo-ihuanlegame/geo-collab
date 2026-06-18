# 图片库栏目删除 — 设计稿

- 日期：2026-06-18
- 模块：`image_library`（后端）/ `web/src/features/image-library`（前端）
- 状态：设计已确认，待实现

## 背景与问题

图片库以「栏目」为单位组织素材，一个栏目对应一个 MinIO bucket（`StockCategory.bucket_name` 唯一），桶里是该栏目的图片对象，DB 里是 `StockImage` 记录（`cascade="all, delete-orphan"` 挂在 `StockCategory.images` 关系上）。

现状：
- 后端**已有** `DELETE /api/image-library/categories/{id}` 端点，但**栏目内有图片时直接 409 拒绝**（"该文件夹内还有图片，请先清空"），只能删空栏目。
- 前端 `api/image-library.ts` 里 `deleteCategory` 客户端函数已存在，但 `ImageLibraryWorkspace.tsx` **没有任何 UI 调用它**——事实上删不了带图的栏目。

需求：选中一个栏目 → 单击「删除」→ 弹二次确认窗（带栏目名 + 图片数量）→ 打通前端 / 后端 / MinIO 做删除。

## 关键事实（决定方案形态）

图片嵌入文章的方式：文章正文里是 `<img src="/api/stock-images/{id}/file">`（图片节点 attrs 带 `stockImageId`，见 `image_library/inserter.py:build_image_node`）。这是**实时从 MinIO 读**的代理 URL（`router.py:serve_image_file`）。由此：

- **已发布到外部平台（头条 / 公众号）的文章不会坏**：发布时 runner 把图从 MinIO 拉到本地临时文件再上传到对方 CDN（`tasks/runner.py:_resolve_stock_image_path`），外部平台存的是自己的副本，删源图不影响。
- **会坏的只有「平台内」仍引用这些图的文章**：草稿 / 待审 / 待发布的文章，在编辑器、预览、平台内文章详情里会显示裂图；待发布的在发布时按 `missing_ok` 跳过该图或报错。
- **没有「图 ↔ 文章」引用表**：要知道某张图被哪些文章引用，只能全表扫 `articles.content_html` / `content_json`（匹配 `/api/stock-images/{id}/file` 或 `stockImageId`）。

## 决策

1. **删除语义 = 硬删 + 引用预警**（不做软删，不做按引用区分的混合方案）。
   - 硬删：清空桶对象 → 删桶 → 级联删图片记录 → 删栏目记录，真正回收存储。
   - 引用预警：二次确认窗除了栏目名 + 图片数，再扫一遍告诉用户「有 M 篇平台内文章正引用本栏目图片，删后会裂图」。这是**提示，不是拦截**——用户知情后仍可删。
   - 不选软删的理由：已发布文章本来就不会坏，真正会裂图的只有平台内草稿；软删会让存储永远收不回、桶一直占着、还得另建清理机制，复杂度不划算。
2. **权限 = 任何登录用户**，与现有「删单张图 / 建栏目」一致（端点维持 `Depends(get_current_user)`，不加 `require_admin`）。

## 后端改动

### `image_library/store.py`
新增清桶函数：

```python
def empty_bucket(bucket_name: str) -> None:
    """删除桶内所有对象（list_objects(recursive=True) → remove_objects 批量删）。best-effort。"""
```

`remove_bucket` 不变（MinIO 仅允许删空桶）。

### `image_library/router.py` — `delete_category`
- **去掉** `image_count > 0 → raise HTTPException(409)` 那道拦截。
- **先解开指向本栏目的外键引用**（关键，否则 `db.delete(cat)` 触发 MySQL FK 约束 1451）：
  - `articles.stock_category_id`：FK 无 `ON DELETE`（migration 0024）→ 默认 RESTRICT。必须先 `UPDATE articles SET stock_category_id = NULL WHERE stock_category_id = :id`（ORM bulk update，`synchronize_session=False`）。这是「主推栏目=封面来源」的单值关联，栏目没了置空是正确语义，静默清理、不计入预警。
  - `article_stock_categories` 多对多：FK 带 `ON DELETE CASCADE`（migration 0028）→ DB 自动清理 join 行，**无需手动处理**。
  - 无其它表 FK 指向 `stock_images.id`，图片记录级联删除安全。
- 删除流程：先 `empty_bucket(bucket_name)` 再 `remove_bucket(bucket_name)`，两步都 **best-effort**（捕获异常、只 log warning、不阻断），随后 `db.delete(cat)` + `db.commit()`（relationship `cascade="all, delete-orphan"` 会删该栏目所有 `StockImage` 行）。
  - 理由：与现有 `delete_image`「MinIO 删失败不阻断，以 DB 记录为准，宁可残留孤儿对象」哲学一致，避免「DB 删了桶没删」或「桶删了 DB 没删」的半删状态。
  - 残留孤儿桶因桶名是 `_unique_bucket_name` 自动唯一生成，不影响后续建桶。
- router.py 目前无 logger，需加 `import logging` + `logger = logging.getLogger(__name__)`。
- 需 `from server.app.modules.articles.models import Article`（null 更新 + 引用扫描都要用；articles.models 不反向依赖 image_library，无循环导入）。
- audit 沿用现有 `stock_category.delete`，payload 补 `image_count`。

### `image_library/router.py` — 新增删除预览端点
给确认窗填引用数：

```
GET /api/image-library/categories/{id}/delete-preview
→ { image_count: int, referenced_article_count: int | null }
```

- `image_count`：该栏目图片数。
- `referenced_article_count`：平台内仍引用本栏目任意图片的（未软删）文章数；扫描异常时返回 `null`。
- 404 当栏目不存在。

#### 引用扫描逻辑
1. 取该栏目所有 `StockImage.id` 集合 `image_ids`。
2. 候选集：`SELECT id, content_html FROM articles WHERE content_html LIKE '%/api/stock-images/%'`（只捞嵌过任意图库图的文章，并排除已软删文章），把全表扫窄到「用过图库的文章」子集。
3. Python 侧用正则 `/api/stock-images/(\d+)/file` 从每篇 `content_html` 抽出被引用的 id，与 `image_ids` 取交集，非空则该文章计入。
4. 返回 distinct 文章数。
5. best-effort：扫描抛异常时端点返回 `referenced_article_count: null`，前端显示「引用统计失败，请谨慎删除」，**不阻断删除**。

> 只扫 `content_html` 足够：图片节点渲染的 HTML 必带 `/api/stock-images/{id}/file`，三份正文里它最规整。结尾 `/file` 天然防 `12` 误中 `123`。复杂度 O(用过图库的文章数)，内部工具规模可接受。

## 前端改动

### `web/src/api/image-library.ts`
- 新增 `getCategoryDeletePreview(id: number): Promise<{ image_count: number; referenced_article_count: number | null }>`。
- `deleteCategory(id)` 已存在，直接复用。

### `web/src/features/image-library/ImageLibraryWorkspace.tsx`
- 顶部操作栏「编辑栏目」旁加 **danger 样式「删除栏目」按钮**，`disabled={!selectedCategory}`。
- 点击 → 打开删除确认 modal：
  - 标题「删除栏目」，正文显示 **栏目名 + 图片数量**。
  - 打开时调 `getCategoryDeletePreview`，统计中显示「正在统计引用…」，出结果后：
    - `referenced_article_count > 0`：红字警告「有 M 篇平台内文章正引用本栏目图片，删除后它们在平台内会显示裂图（已发布到外部平台的不受影响）」。
    - `=== 0`：「无文章引用，可安全删除」。
    - `=== null`：「引用统计失败，请谨慎删除」。
  - 「取消」/「确认删除」（danger，删除中禁用）。
- 删成功：从 `categories` 移除该项；若是当前选中项则 `selectedCategoryId` 重选首项或置 `null`；toast「栏目已删除」。
- 删失败：toast 错误信息。

## 搜索 / 列表 —— 不改动

硬删后 `StockImage` 记录已级联删除，`search` / `list_images` / `list_categories` 都是查实表，自然不会再返回它们。**不需要加 `is_deleted` 判断**——这正是选硬删（而非软删）省下的复杂度。直接回答「搜索是否需要改判断有没有被删除」：不需要。

## 权限与审计

- 端点维持 `Depends(get_current_user)`（任何登录用户）。
- 审计记 `stock_category.delete`（payload 含 `name`、`image_count`）。

## 测试

新增 `server/tests/test_image_library_delete.py`（`@pytest.mark.mysql`，monkeypatch `minio_store.empty_bucket` / `remove_bucket` 打桩）：
1. 删非空栏目成功，桶被清空 + 删除，`StockImage` 记录级联删除，`StockCategory` 删除。
2. `delete-preview` 在有 / 无文章引用时计数正确（造引用 `/api/stock-images/{id}/file` 的文章 + 不引用的文章，验证 distinct 计数与交集精度，含 `12` vs `123` 不误中）。
3. MinIO 删除抛异常时仍删 DB 记录（best-effort 不阻断）。
4. 删不存在的栏目 → 404。

前端无单测框架，门禁是 `pnpm --filter @geo/web typecheck` + `build`。

## 取舍 / 已知边界

- **孤儿对象 / 桶**：MinIO best-effort 删失败时可能残留对象或桶，记 warning，可接受（与 `delete_image` 同哲学）。不引入补偿性清理任务。
- **预警是提示非拦截**：用户知情后仍可删，符合「硬删 + 引用预警」决策。
- **引用扫描是全表 LIKE 预筛**：内部工具规模下可接受；若未来文章量极大需优化，可引入 `stockImageId` 索引或「图 ↔ 文章」引用表，但本次 YAGNI。
- **`serve_image_file` 对已删图返回 404**：即「裂图」行为，已有逻辑，无需改。
