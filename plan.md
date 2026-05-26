# AI 排版配图改造计划

## 问题

1. **插图太频繁** — 列表式文章 `min_spacing=1`（两张图间只隔 1 个节点），LLM 倾向于遇到段落就插
2. **没有内容感知** — `_maybe_insert_images` 只用单 `stock_category_id`，`pick_image_id()` 是 `ORDER BY RAND()` 不看任何内容
3. **单栏目限制** — 文章只有 1 个 `stock_category_id`，但内容可能覆盖多个话题（如推 5 款游戏但只能选 1 个栏目）

---

## 方案

### 1. 数据库 — 新增多对多关联表

**新增表** `article_stock_categories`（migration 0025）：

| 列 | 类型 |
|---|---|
| `article_id` | FK → articles.id |
| `stock_category_id` | FK → stock_categories.id |
| 联合唯一约束 | `uq_article_stock_cat` |

- 保留 `articles.stock_category_id` 列暂不删除（迁移旧数据后视为冗余，后续可清理）
- 新增 `Article.stock_categories: Mapped[list[StockCategory]]` 多对多关系

### 2. 提示词改造（内置）

修改 `_build_system_prompt_with_images()`：

- 传段落原文给 LLM，让其感知每段内容
- 新增约束：
  - `min_spacing` 统一为 3
  - "每个小标题段落最多一张配图"
  - "只有段落内容有明显可配图的主题时才插图，不确定就不插"
- 输出格式改为：
  ```json
  {"heading_indices": [2, 7], "image_positions": [{"index": 4, "hint": "环境描写"}, {"index": 10, "hint": "人物对话"}]}
  ```
  - `hint`：LLM 提取的短关键词，描述该配图应表达的内容主题
  - 不确定的段落不插图（不返回该位置或 hint 留空）
- 后端兼容旧格式 `image_positions: [4, 10]`（当作 hint 为空处理）

### 3. 智能选图

新增 `select_images_by_hints(category_ids, hints, db)`：

**匹配逻辑（按优先级）：**
1. **标签匹配** — `StockImage.tags` 中有元素包含 hint 关键词
2. **描述匹配** — `StockImage.description` 包含 hint 关键词
3. **都不匹配 → None**（跳过该位置，不降级随机）

`ImageQuery` 更新：
- `category_id: int` → `category_ids: list[int]`
- 新增 `hint: str | None`

### 4. `_maybe_insert_images` 改造

- 从 `article.stock_categories`（多对多）获取所有分类 ID
- 解析 `image_positions` 兼容新旧格式
- 有 hint 的 → `select_images_by_hints` 语义匹配
- hint 为空 / 不匹配 → 跳过该位置
- 旧格式（纯数字数组）→ hint 全部为空 → 不插图

### 5. 受影响的文件

| 文件 | 改动 |
|---|---|
| `server/app/modules/articles/models.py` | 新增 `stock_categories` 多对多关系 |
| `server/app/modules/articles/schemas.py` | `stock_category_ids: list[int]` 替代 `stock_category_id` |
| `server/app/modules/articles/service.py` | 创建/更新文章处理多栏目 |
| `server/app/modules/articles/router.py` | `include_images` 判断改为 `len(article.stock_categories) > 0` |
| `server/app/modules/articles/ai_format.py` | 提示词改造 + `_maybe_insert_images` 重写 |
| `server/app/modules/image_library/selector.py` | `ImageQuery` 改 `category_ids` + 新增 hint 匹配函数 |
| `server/app/alembic/versions/0025_article_stock_categories.py` | 新增 migration |
| `server/tests/test_ai_format.py` | 适配新格式 |
