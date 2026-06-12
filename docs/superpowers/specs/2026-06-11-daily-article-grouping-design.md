# AI 生文后按日期归组 + 审核显示丝滑化 — 设计文档

- 日期：2026-06-11
- 分支：`feat/daily-article-grouping`
- 状态：已与用户确认，待实现

## 1. 背景与问题

当前流水线典型链路是 `ai_generate → to_review(送审) → distribute`。

- `to_review` 节点调用 `mark_pending_and_group()`（[articles/service.py:507](../../../server/app/modules/articles/service.py)），**每次运行都新建一个分组**，撞同名时还主动加后缀避免复用。结果：同一天多次运行 / 多条流水线产出散落成一堆分组，无法「持续归到同一个分组」。
- 前端 `ContentWorkspace` 把分组**派到单一标签**，由成员状态派生（[ContentWorkspace.tsx:437-475](../../../web/src/features/content/ContentWorkspace.tsx)）：混合状态的组只待在「未审核」标签显示 `4/5 已审核`，approve 完最后一篇时**整组突然跳到「已审核」标签**。这种跳变不符合人的操作直觉。

## 2. 目标

1. AI 生文产出的新文章能**持续追加**到「当天的分组」里——同一用户当天所有运行 / 所有流水线并入同一个日期分组（每天一个组，全局）。
2. 同一日期分组里已审核 / 未审核混在一起时，显示更丝滑：approve 单篇时组头不整体跳走，并能看到「另一侧还有几篇」。

## 3. 非目标（明确排除）

- 执行器兜底成组（`to_review` 缺失/跳过时的 fallback，[executor.py:194-216](../../../server/app/modules/pipelines/executor.py)）维持 per-run，不在本次范围内。
- 手动创建的文章、非流水线产出的归组逻辑不变。
- 自动分发门禁不变（仍只在「整组已审核」时出现分发入口）。
- 不引入新的 DB 列：日期维度完全用现有 `ArticleGroup.name`（按 `(user_id, name)` 唯一约束）承载，不加迁移。

## 4. 方案总览

两块协同改动：

- **后端**：`to_review` 节点新增 `daily_group` 布尔开关（默认 `false` = 现状 / `true` = 按天归组）；新增 service 函数 `mark_pending_and_append_daily`（按日期分组名查找-或-新建后**去重追加**，并发安全）。
- **前端**：
  - 编辑器：开关用 `toggle` 类型，由后端 `config_schema` 声明、前端**通用渲染器自动渲染**（[PipelineEditor.tsx:463-482](../../../web/src/features/pipelines/PipelineEditor.tsx) 已有 `toggle` 分支），**无需新增前端代码**。
  - 内容页：混合状态日期分组在「未审核」「已审核」两个标签**都可见**，每侧只列本状态文章，并提示「另有 N 篇在另一标签」。

> 设计取舍：原计划用 `group_mode` 枚举（per_run/per_day），但前端通用配置渲染器只支持 `toggle`/`checkbox`/`text` 等，没有 `select`/`options` 渲染。为避免为单个字段新增枚举渲染器（YAGNI），改用布尔 `daily_group` 开关，语义等价、零前端改动。

## 5. 后端详细设计

### 5.1 新增 service 函数

文件：`server/app/modules/articles/service.py`

```python
def mark_pending_and_append_daily(
    session_factory,
    *,
    article_ids: list[int],
    user_id: int,
    group_name: str,
) -> int | None:
    """把文章标 review_status='pending' 并追加进 (user_id, group_name) 指向的分组：
    - 该名分组（未软删）已存在 → 复用；不存在 → 新建。
    - 去重追加：只为尚不在组内的 article_id 建 ArticleGroupItem，sort_order 接现有 max+1。
    - 并发两个 run 同时建组撞 (user_id, name) 唯一约束 → rollback、重标 pending、回查已存在
      的组再追加（复刻现有 mark_pending_and_group 的 IntegrityError 重试思路）。
    - 尽力而为：失败记日志、不抛；用独立 session、函数内 commit+close。返回 group_id 或 None。
    """
```

要点：
- 与现有 `mark_pending_and_group` 的本质区别——**复用同名组并去重追加**，而不是加后缀新建。
- 去重：先查该组现有 `ArticleGroupItem.article_id` 集合，过滤掉已存在的，避免撞 `uq_article_group_items_group_article`。
- `sort_order`：取该组现有 `max(sort_order)`，新成员从 `max+1` 起递增。
- 软删同名组的处理：查找时只认未软删的组（`is_deleted == False`）；若同名组是软删状态，新建会撞唯一约束 → 走复活逻辑（参考 `create_group` 复活分支）或在 IntegrityError 重试里处理。实现时统一：查未软删组没有 → 尝试新建 → IntegrityError 则回查（可能是软删撞名）→ 复活并清空成员后追加。**实现需覆盖软删撞名这一边角**。

### 5.2 改 `to_review` 节点

文件：`server/app/modules/pipelines/nodes/to_review.py`

```python
def run_to_review(ctx):
    cfg = ctx.config or {}
    article_ids = ctx.inputs.get("article_ids") or cfg.get("article_ids") or []
    if not article_ids:
        return NodeResult(output={"skipped": "无文章"}, article_ids=[])

    if cfg.get("daily_group"):  # 按天归组
        today = dt.datetime.now(ZoneInfo(get_settings().scheduler_tz)).date()
        group_name = f"每日生成 · {today:%Y-%m-%d}"
        gid = mark_pending_and_append_daily(
            ctx.session_factory, article_ids=list(article_ids),
            user_id=ctx.user_id, group_name=group_name,
        )
    else:  # 保持现状：每次运行一个新组
        base_name = (cfg.get("group_name") or "").strip() or "未审核 · 智能体生成"
        gid = mark_pending_and_group(
            ctx.session_factory, article_ids=list(article_ids),
            user_id=ctx.user_id, base_name=base_name, fallback_suffix=f"#{article_ids[0]}",
        )
    return NodeResult(output={"group_id": gid, "article_ids": list(article_ids)}, article_ids=[])
```

- 日期取 `GEO_SCHEDULER_TZ`，复用 `dt.datetime.now(ZoneInfo(get_settings().scheduler_tz))`（[scheduler.py:35](../../../server/app/modules/pipelines/scheduler.py) 同款）。
- 组名固定前缀 `每日生成 · {YYYY-MM-DD}`，**不随流水线/节点名变化**——保证「每天一个组·全局」。`daily_group=true` 时忽略 `group_name` 配置。
- 两种分支都输出 `group_id` → 执行器照旧不重复兜底成组。

### 5.3 编辑器节点配置（后端 config_schema，前端零改）

文件：`server/app/modules/pipelines/router.py`（`get_node_types` 的 `to_review` 段，[router.py:142-147](../../../server/app/modules/pipelines/router.py)）

给 to_review 的 `config_schema` 增一个 toggle 字段：

```python
{
    "type": "to_review",
    "label": "进入未审核库",
    "config_schema": [
        {"key": "group_name", "type": "text", "label": "分组名(可空)"},
        {
            "key": "daily_group",
            "type": "toggle",
            "label": "按天归组",
            "hint": "开启后，当天所有运行/流水线产出并入同一个「每日生成 · 日期」分组",
            "default": False,
        },
    ],
},
```

前端 `PipelineEditor` 的 `f.type === "toggle"` 分支会自动渲染该开关，**无需改前端代码**（与 `ai_illustrate` 的 `web_fallback` 同模式）。

## 6. 前端详细设计

文件：`web/src/features/content/ContentWorkspace.tsx`

改 [437-475](../../../web/src/features/content/ContentWorkspace.tsx) 的标签归属与渲染逻辑（纯前端，无后端依赖）：

1. **标签归属**：不再用单一 `groupReviewTab` 把组派到一个标签。改为：
   - 组有 ≥1 篇待审 → 出现在「未审核」标签；
   - 组有 ≥1 篇已审 → 出现在「已审核」标签；
   - 混合状态 → 两个标签都出现；
   - 空组（total=0）→ 仅「未审核」（维持现状）。
2. **`reviewCounts`**：混合组在两侧各计 1（与现有「组=1 单位」计数口径一致）。
3. **`unifiedList`**：当前标签包含「在该标签状态下有成员（或空组+pending）」的组。
4. **组内渲染**：展开时只列**当前 `reviewTab` 状态**的文章；若另一侧还有成员，加一行提示，如 `另有 2 篇已审核 →（切到「已审核」标签查看）`。
5. **组头**：进度徽章 `2/5 已审核` 保留；`全部通过` 出现在「未审核」侧且有待审成员时；`自动分发` 仍只在「整组已审核」时出现（自然落在「已审核」侧）。

效果：approve 单篇 → 该篇从本侧列表移到另一侧，组头不整体跳走，提示数字更新。这就是要的丝滑。

## 7. 数据流与边界

- 日期分组 = `ArticleGroup`，名 `每日生成 · YYYY-MM-DD`，受 `(user_id, name)` 唯一约束天然保证「每天一个组」。
- 跨天：日期变 → 组名变 → 自动新建次日组。
- 并发：同日两个 run 几乎同时收尾 → 一个建组成功，另一个 IntegrityError 后回查复用。
- 去重：同一篇被重复送审 → 不重复建 item。
- 时区：统一 `GEO_SCHEDULER_TZ`（生产为无 DST 的 `Asia/Shanghai`）。

## 8. 测试计划

### 后端（pytest，MySQL，需 `GEO_TEST_DATABASE_URL`）

新增测试文件 `server/tests/test_daily_grouping.py`（不匹配 `test_pipeline*` glob，单独命名）：

- `per_day` 同日两次 to_review → 同一个组，文章累加、`ArticleGroupItem` 不重复、`sort_order` 递增。
- 跨天（注入/monkeypatch「当前日期」）→ 落到不同组名。
- 重复提交同一 article → 去重，不新增 item。
- 并发 `IntegrityError` 路径 → 回查复用同组（可用 monkeypatch flush 触发一次 IntegrityError 验证重试分支）。
- 软删同名组撞名 → 复活并追加（边角）。
- 默认（`daily_group` 关）行为不变（回归既有 `mark_pending_and_group`，每次新组）。
- 节点级：构造 `NodeRunContext`，`daily_group=True` 跑两次验证累加；缺省时等于旧行为。
- node-types：`GET /api/pipelines/node-types` 的 to_review 段含 `daily_group` 且 `type=="toggle"`。

### 前端

- 无单测框架 → `pnpm --filter @geo/web typecheck` + `build` 为门禁。
- 手动验证：混合组在两标签都显示、各列本侧文章、跨标签提示正确；approve 单篇组头不跳走；全部审核后只在「已审核」侧且出现分发入口。

## 9. 实现任务拆分（供并行执行）

依赖关系：T1 → T2（节点依赖 service）。T3（前端内容页）与后端无代码依赖，可与 T1/T2 **并行**。编辑器开关已并入 T2 的后端 `config_schema`，无独立前端任务。

- **T1（后端·service）**：`mark_pending_and_append_daily` + 后端单测（service 级 + 去重/并发/软删边角）。
- **T2（后端·节点 + config_schema）**：`to_review` 的 `daily_group` 分支 + router `config_schema` 加 toggle + 节点级/node-types 测试。依赖 T1。
- **T3（前端·内容页）**：ContentWorkspace 双标签可见 + 跨标签提示 + 计数调整。与 T1/T2 并行。
- **集成**：ruff check / ruff format / mypy / pytest（后端）、typecheck / build（前端）全绿。

## 10. 自检清单（落地后核对）

- [ ] 默认（`daily_group` 关），现有流水线行为零变化。
- [ ] `daily_group` 开：同日累加、跨天新建、去重、并发安全。
- [ ] 前端混合组双标签 + 跨标签提示；approve 不跳组。
- [ ] 后端测试全绿；前端 typecheck + build 全绿。
- [ ] CLAUDE.md 若需补 `group_mode` 说明则同步（pipelines 模块段落）。
