# 配图兜底：检查 + 随机补图（illustration fallback random fill）

- 日期：2026-06-29
- 状态：设计已确认，待写实现计划
- 范围：`illustrate_one`（共享配图 service）末尾追加一个串行兜底步骤
- 关联记忆：`project_illustrate_undercount_rootcause`（ai_format 自报 requested 漏点、partial_images 盲区）

## 1. 背景与目标

当前一篇文章的「AI 配图」走 `articles/ai_illustrate_svc.py:illustrate_one()`，内部调
`run_ai_format`（或 `run_ai_format_from_game_list`）让模型点名游戏 / 栏目并插图。已知问题
（见关联记忆）：模型经常**该配 N 张只配上 M 张**（`requested > inserted`），甚至零图，
而这些"漏掉的位置"目前不会被补上——文章静默进入待审池时图偏少。

**目标**：在配图主流程**最末尾**串一个简单的"检查 + 随机补图"兜底步骤——不做精准语义
匹配，只保证"该有图的文章不会图太少"。具体：若实际图数低于应配数，就**从图库随机取图补足**。

### Non-goals（明确不做）

- 不做精准匹配（不按游戏名 / hint 选图）。补的是**随机**图，用户已确认接受。
- **不改 `save_article` / web 保存节点本身**。这两个入口在落库时没有 `main_category_id`、
  也没有 ai_format 诊断，随机取图无从下手；"生成完一篇文章的末尾"在技术上就是配图步骤的末尾。
- **不覆盖方案运行（scheme_executor）**。用户确认本次只做 `illustrate_one`
  （= MCP `ai_illustrate_article` 工具 + web `ai_illustrate` 节点两条路径）。
- 不引入 content_html / plain_text 三份同步——沿用现有插图代码的既定行为（只回写
  `content_json` + `version`，见 `image_library/inserter.py` 文档说明）。

## 2. 触发规则（已确认：补到 requested，至少 1 张）

在 `illustrate_one` 内，`run_ai_format` 返回后已经有：

- `fmt_diag["requested"]` —— AI 点名且能定位到栏目的位置数（"应该配上图"的张数）
- `fmt_diag["inserted"]` —— 实配数
- `candidate_categories` —— `[{id,name,description}, ...]`（主推 + 陪衬栏目）
- `max_images` —— 本次配图硬上限

兜底目标与缺口：

```
current   = count_body_images(article.content_json)        # 当前正文实际图数
target    = min(max(requested, 1), max_images)             # 应配张数，至少 1，封顶 max_images
gap       = target - current
```

`gap <= 0` → 不补（no-op）。`gap > 0` → 随机补 `gap` 张。

边界行为（设计意图，写测试覆盖）：

- 正文**本来就有图**（run_ai_format 因 `already_has_images` 跳过、requested=0）：
  `target=1`，`current>=1` → `gap<=0` → 不补。**不会在已有图上叠加。**
- requested>0 但栏目里**一张图都没有**（`no_match_in_categories`，inserted=0）：
  `target=requested`、`current=0` → 随机从 candidate 栏目补足；若 candidate 栏目确实
  没有任何图 → `pick_image_id` 返回 None → 静默 no-op。
- 模型完全没点名（requested=0）且零图：`target=1` → 补 1 张兜底。

## 3. 新增模块：`image_library/fallback.py`（约 60–80 行）

两个小函数，纯逻辑 + 一个 DB 写入函数，全部 best-effort。

### 3.1 `count_body_images(content_json: dict) -> int`

数 Tiptap 顶层 `content` 数组里 `type == "image"` 的节点数。复用
`inserter.has_images_in_content` 的遍历思路（那个只判 bool，这里要计数）。

### 3.2 `collect_used_stock_image_ids(content_json: dict) -> set[int]`

扫顶层 image 节点的 `attrs.stockImageId`，收集已用图片 id，用于补图去重，避免随机补到
正文里已经出现过的同一张。

### 3.3 `fill_random_images(db, article, *, category_ids, gap, max_images) -> int`

- 用 `selector.pick_image_id(ImageQuery(category_ids=..., excluded_ids=...))` 逐张随机取
  （`func.rand()`，MySQL only），每取一张把 id 加入 `excluded_ids` 去重，
  `fetch_image_by_id` 拿 `StockImageRef`。最多取 `gap` 张；取不到（None）就提前停。
- 用 `inserter.insert_images_at_positions(content_json, refs, positions)` 插入。
  - **位置策略（不求精准）**：在正文顶层块节点中**均匀挑 N 个位置**插在其后，
    跳过紧邻已有 image 的位置，避免两张图贴在一起。N 不足时按实际可用位置。
    （沿用 inserter 既有的"按位置之后插入 + 自动处理索引偏移"能力。）
- 回写 `article.content_json = 新文档`、`article.version += 1`、`db.commit()`。
- 返回**实际补入的张数**。

## 4. 接入点：`illustrate_one()` 末尾新增一个串行阶段

`ai_illustrate_svc.py:illustrate_one()` 现有阶段：①配图(run_ai_format) → ②封面 → ③回读
ai_format_error。在 **①之后**（拿到 `fmt_diag` 与 `candidate_categories`）新增：

### 阶段 1.5：随机补图兜底（新）

```python
fallback_inserted = 0
try:
    requested = int(fmt_diag.get("requested", 0) or 0)
    category_ids = [c["id"] for c in candidate_categories if c.get("id")]
    db = session_factory()
    try:
        article = db.get(Article, article_id)
        if article is not None and not article.is_deleted and category_ids:
            current = count_body_images(article.content_json or {})
            target = min(max(requested, 1), max_images)
            gap = target - current
            if gap > 0:
                used = collect_used_stock_image_ids(article.content_json or {})
                fallback_inserted = fill_random_images(
                    db, article, category_ids=category_ids, gap=gap, max_images=max_images,
                )
    finally:
        db.close()
except Exception:           # best-effort，绝不影响配图主结果（同封面步骤）
    _logger.exception("fallback random fill failed for article %s", article_id)
```

- 用 `session_factory()` 开独立短 session（与封面阶段同款），不复用阶段 1 的 session。
- 异常全吞 + log，绝不上抛——兜底失败不能拖垮"文章已落库可用"的事实。

### 结果回传（观测性）

- `IllustrateResult` 新增字段 `fallback_inserted: int = 0`。
- `images_inserted` 累加上 `fallback_inserted`（对外仍是"这篇一共配了几张图"的总数）。
- MCP 端 `AiIllustrateResponse` 同步加 `fallback_inserted` 字段（默认 0），让 /goal loop /
  飞书可见"有几张是随机兜底补的"。web `ai_illustrate` 节点的汇总 output 也把
  `fallback_inserted` 加进去（非破坏性新增）。

> 观测性理由见关联记忆：零图 / 漏配过去会静默进审池。补图本身是好事，但要让运营看到
> "这几张是兜底随机来的、非语义匹配"，不要假装是精准配图。

## 5. 影响的文件清单

| 文件 | 改动 |
|---|---|
| `server/app/modules/image_library/fallback.py` | **新增**：`count_body_images` / `collect_used_stock_image_ids` / `fill_random_images` |
| `server/app/modules/articles/ai_illustrate_svc.py` | `illustrate_one` 加阶段 1.5；`IllustrateResult` 加 `fallback_inserted` |
| `server/app/modules/articles/router.py` | `AiIllustrateResponse` 加 `fallback_inserted`，端点透传 |
| `server/app/modules/pipelines/nodes/ai_illustrate.py` | 节点 output 汇总加 `fallback_inserted`（非破坏性） |
| `server/tests/test_illustration_fallback.py`（或并入现有配图测试） | **新增**测试 |

## 6. 错误处理 / 一致性

- 阶段 1.5 全程 try/except + `logger.exception`，失败静默；与现有封面阶段（stage 2）一致。
- 只写 `content_json` + `version`，不动 content_html / plain_text（沿用现状，见 Non-goals）。
- 去重：补图排除正文已用 `stockImageId`，候选不足时 `pick_image_id` 自然返回 None 提前停。
- 幂等性：重复对同一篇调用配图，若已达 target 则 `gap<=0` 不再补；不会无限叠图。

## 7. 测试计划

纯函数 / service 级（无需真 LLM）：

1. `count_body_images`：空文档=0；含 2 个 image 节点=2。
2. 零图 + requested=3 + 栏目有图 → 补 3 张，且都来自 candidate 栏目、互不重复。
3. 已配 2 张 + requested=3 → 只补 1 张（补到 target）。
4. 已达/超过 target（current>=target）→ 不补。
5. 栏目无图（pick 返回 None）→ no-op、不抛异常、`fallback_inserted=0`。
6. 去重：正文已含某 stockImageId，补图不再选中它（mock 候选池验证 excluded）。
7. 集成：mock `run_ai_format` 制造"requested=3 / inserted=1"，走 `illustrate_one`，
   断言最终正文图数补到 3、`IllustrateResult.fallback_inserted==2`、
   `images_inserted` 含兜底数。

测试遵循仓库约定：`@pytest.mark.mysql` + `build_test_app` 的集成测试在 `finally` 里
`cleanup()`；纯函数测试直接构造 content_json dict、用 mysql session 或对 selector 打桩。

## 8. 开放项 / 已决策

- ✅ 触发目标：补到 `requested`，至少 1 张（封顶 `max_images`）。
- ✅ 范围：仅 `illustrate_one`（MCP 工具 + web 配图节点）；方案运行不动。
- ✅ 随机非精准：用户已确认接受随机补图。
- ✅ 只写 content_json + version：沿用现状，不引入三份同步。
