# pipeline 生文「边生成边进组」(流式进每日分组) — 设计文档

- 日期：2026-06-15
- 分支：`chore/audit-bump-pytest-multipart`（在此基础上开实现分支）
- 状态：已与用户确认，待实现

## 1. 背景与问题

当前 pipeline 生文典型链路 `(问题源) → ai_generate → to_review(送审) → distribute`，**成组永远发生在最后**：

- `ai_generate`（[ai_generate_node.py](../../../server/app/modules/pipelines/nodes/ai_generate_node.py)）用 `ThreadPoolExecutor(max_workers=4)` **并发**把整批文章生成完，收齐所有 `article_id` 后**一次性**返回。
- `to_review`（[to_review.py](../../../server/app/modules/pipelines/nodes/to_review.py)）或执行器兜底（[executor.py:194-218](../../../server/app/modules/pipelines/executor.py)）**才**创建分组、把整批文章塞进去。

后果（测试不友好）：

- 运行全程看不到任何分组，最后才"啪"地整组冒出来——没有逐篇进度反馈。
- 运行中途崩溃（如第 3 篇出错），已成功的前 2 篇因为没走到成组步骤而可能变成"孤儿"——不在任何分组里。
- 分组名/位置在运行结束前不可预知，反复测试时不好定位。

## 2. 目标

为 `ai_generate` 增加一个开关，开启后：

1. **生成前先把目标分组建好**（哪怕还空），对应"提前创建一个分组"。
2. **每生成一篇立即标 `pending` 并进组、立刻 commit**——运行中刷新即可看到文章逐篇变多。
3. **中途失败不丢已生成的文章**——成功的已落地进组，失败的不进组。
4. 分组名**按日期自动生成** `每日生成 · YYYY-MM-DD`，**复用现有 daily_group 机制**：同一天多次运行 / 多条 pipeline 自动并入同一组（去重追加）——既"可预知"又"可持续追加"。

> 这四点正是用户明确要的全部诉求。分组名走日期是用户的选择（不要自定义文本框）。

## 3. 非目标（明确排除，YAGNI）

- 不改 `ai_compose` 节点（[ai_compose.py](../../../server/app/modules/pipelines/nodes/ai_compose.py)）。
- 不改「方案运行」（scheme run，`scheme_executor.py`）那条生文流。
- 不加自定义分组名文本框——分组名纯按日期。后续真要再说。
- 不改审核 / 分发门禁：流式只是把"标 pending"提前并打散到每篇，审批流（人工 approve → distribute 门禁）完全不变。
- 不改 `to_review` 自身的 `daily_group` 语义（仅新增一条"上游已成组就透传"的守卫，见 5.3）。

## 4. 方案总览

本质是 brainstorm 选定的「方案 A」特化到**纯按日期**：在 `ai_generate` 节点加一个 `daily_group` toggle，复用已有的日期分组机器（`每日生成 · YYYY-MM-DD` + 同日复用 + 去重追加），把它**提前到生成前建组、改成边生成边塞**。

- **后端 service**：拆出两个极小 helper——`resolve_or_create_daily_group`（生成前一次性建组/复用，返回 group_id + sort_order 起点）与 `append_article_to_group_pending`（每篇标 pending + 追加单 item，**绝不碰组行**）。
- **ai_generate 节点**：新增 `daily_group` toggle。开启时生成前建组，flat / units 两条路径的 worker 在每篇成功后流式进组，节点输出加 `group_id`。
- **to_review 节点**：新增守卫——上游已带 `group_id`（说明已流式成组）→ 透传不再建组（防 `daily_group=关` 的 to_review 用 `mark_pending_and_group` 另起一组导致文章进两个组）。
- **config_schema**：给 ai_generate 加一个 `toggle` 字段，前端通用渲染器自动出开关，**零前端代码**（与 to_review 的 `daily_group`、ai_illustrate 的 `web_fallback` 同模式）。

## 5. 后端详细设计

### 5.1 新增两个 service helper

文件：`server/app/modules/articles/service.py`

现有 `mark_pending_and_append_daily`（[service.py:572-667](../../../server/app/modules/articles/service.py)）是"整批一次性追加"，且在 line 654 执行 `group.updated_at = utcnow()`（**动组行**）。流式场景需要把"建组"与"逐篇追加"拆开，且追加时**不动组行**（见第 7 节死锁分析）。

```python
def resolve_or_create_daily_group(
    session_factory, *, user_id: int, group_name: str
) -> tuple[int, int] | None:
    """查找-或-新建 (user_id, group_name) 分组，返回 (group_id, next_sort_order_start)。
    - 未软删同名组存在 → 复用；软删同名 → 复活并清空成员；都没有 → 新建。
    - next_sort_order_start = 现有 max(sort_order)+1（空组/新建/复活 → 0）。
    - 并发首建撞 (user_id, name) 唯一约束 → rollback 回查复用。
      catch IntegrityError 与 OperationalError（InnoDB 并发唯一 INSERT 偶发死锁 1213，
      见第 7 节）；回查后仍无 → 抛到外层（best-effort 返回 None）。
    - 只解析/建组，不标 pending、不插 item、不在此处动 updated_at。
    - 独立 session、本函数内 commit+close。失败记日志、返回 None。"""


def append_article_to_group_pending(
    session_factory, *, group_id: int, article_id: int, sort_order: int
) -> bool:
    """把单篇标 review_status='pending' 并追加进已存在 group_id。
    - 只插 ArticleGroupItem(group_id, article_id, sort_order) + 标该篇 pending。
    - **绝不 UPDATE 组行**（不 bump version/updated_at）——避免并发 worker 抢父行排他锁。
    - 去重：撞 (group_id, article_id) 唯一约束（理论上不会，文章是本次新建的）→ 当作已在组、
      rollback 后忽略，返回 True。
    - 独立 session、commit+close。失败记日志返回 False。"""
```

要点：
- `resolve_or_create_daily_group` 的"复用 / 复活 / 新建 + IntegrityError 回查"逻辑直接搬现有 `mark_pending_and_append_daily` 的 `_resolve_group`，只是**多 catch 一个 `OperationalError` 并多回查一次**（用户确认纳入的小加固），并把 `max(sort_order)+1` 一并算出返回。
- `append_article_to_group_pending` 与现有批量函数的本质区别：**单篇、不动组行**。

### 5.2 改 `ai_generate` 节点

文件：`server/app/modules/pipelines/nodes/ai_generate_node.py`

新增逻辑（flat 与 units 两条路径共用）：

```python
import itertools
import threading
import datetime as dt
from zoneinfo import ZoneInfo
from server.app.modules.articles.service import (
    resolve_or_create_daily_group,
    append_article_to_group_pending,
)

# 在 run_ai_generate 内、通过校验（units: total>0；flat: count>0）之后、开线程池之前：
group_id = None
order_lock = threading.Lock()
order_counter = None
if cfg.get("daily_group"):
    today = dt.datetime.now(ZoneInfo(get_settings().scheduler_tz)).date()
    group_name = f"每日生成 · {today:%Y-%m-%d}"
    resolved = resolve_or_create_daily_group(
        ctx.session_factory, user_id=ctx.user_id, group_name=group_name
    )
    if resolved is not None:
        group_id, next_start = resolved
        order_counter = itertools.count(next_start)
    # resolved is None（建组失败）→ group_id 保持 None → 退回非流式老路径，
    # 文章照旧最后由 to_review / 执行器兜底成组，不丢文章（优雅降级）。

# 每个 worker 的成功路径（_one 返回 aid 后）：
def _stream_into_group(aid: int) -> None:
    if group_id is None:
        return
    with order_lock:          # 锁只护内存计数器自增（微秒级）
        so = next(order_counter)
    append_article_to_group_pending(   # DB 追加在锁外并发执行（各自不同行，无竞争）
        ctx.session_factory, group_id=group_id, article_id=aid, sort_order=so
    )
```

- **放置位置（两路径各自建组）**：units 路径的 `total>0` 校验在 `_run_units` 内（[ai_generate_node.py:65-71](../../../server/app/modules/pipelines/nodes/ai_generate_node.py)）、flat 的 `count>0` 在主函数（[:144-147](../../../server/app/modules/pipelines/nodes/ai_generate_node.py)）——故建组 + 计数器要在**各自校验通过后、各自开线程池前**分别构建。建议抽一个小 helper（如 `_make_group_streamer(ctx, cfg) -> tuple[group_id|None, _stream_into_group]`）在两路径开头各调一次，避免重复。
- `_stream_into_group(aid)` 在 `_one()`（units 路径）和扁平路径的 `_one()` 成功拿到 `aid` 后调用；放在 worker 线程内（每 worker 自建 session，符合"session 非线程安全"约束）。
- **节点输出加 `group_id`**：`output={"article_ids": ..., "errors": ..., "group_id": group_id}`。`group_id` 为 None（开关关 / 建组失败）时不影响——执行器据 `output.get("group_id")` 判 `grouped`。
- `daily_group` 关（默认）：`group_id` 全程 None，**完全旧行为**，零变化。

### 5.3 改 `to_review` 节点（加守卫）

文件：`server/app/modules/pipelines/nodes/to_review.py`

在函数开头、取到 `article_ids` 之后加守卫：

```python
def run_to_review(ctx):
    cfg = ctx.config or {}
    article_ids = ctx.inputs.get("article_ids") or cfg.get("article_ids") or []
    if not article_ids:
        return NodeResult(output={"skipped": "无文章"}, article_ids=[])

    # 守卫：上游已带 group_id（ai_generate 已流式成组）→ 透传，不再建新组。
    # 同查 inputs 与 upstream（防下游 inputMapping 把 group_id 字段筛掉，与 ai_generate
    # 兜底 generation_units 同款思路）。
    already_gid = ctx.inputs.get("group_id") or (ctx.upstream or {}).get("group_id")
    if already_gid:
        return NodeResult(
            output={"group_id": already_gid, "article_ids": list(article_ids)},
            article_ids=[],
        )

    # ... 以下维持现状（daily_group / 普通分支）...
```

### 5.4 编辑器节点配置（后端 config_schema，前端零改）

文件：`server/app/modules/pipelines/router.py`（`get_node_types` 的 ai_generate 段，[router.py:86-94](../../../server/app/modules/pipelines/router.py)）

在 model 字段后加一个 toggle：

```python
{
    "type": "ai_generate",
    "label": "AI 生文",
    "config_schema": [
        {"key": "prompt_template_id", "type": "prompt_template", "label": "提示词模板"},
        {"key": "count", "type": "number", "label": "生成数量"},
        {"key": "model", "type": "ai_engine", "label": "模型"},
        {
            "key": "daily_group",
            "type": "toggle",
            "label": "边生成边进每日分组",
            "hint": "开启后：生成前先建好「每日生成 · 日期」分组，每生成一篇立即进组并标待审；"
                    "运行中可实时看到逐篇进组，中途失败也不丢已生成的文章。同一天多次运行并入同一组。",
            "default": False,
        },
    ],
},
```

前端 `PipelineEditor` 的 `f.type === "toggle"` 分支自动渲染，**无需改前端代码**。

## 6. 优先级与交互矩阵

新 ai_generate `daily_group` 与现有 to_review `daily_group` 的关系：**ai_generate 一旦开启流式，它就独占该日期组；to_review 自动让位（透传）**，由 5.3 的守卫保证确定性（不靠"撞运气去重"）。

| ai_generate `daily_group` | to_review `daily_group` | 结果 |
|---|---|---|
| 关（默认） | 关 / 开 / 无 | **完全旧行为**，零变化。 |
| 开 | 开 | ai_generate 流式进当天组、输出 group_id；to_review 守卫命中 → 透传。**一个组**。 |
| 开 | 关 | 同上 —— 守卫命中、透传。**守卫专防此格**：否则 `daily_group=关` 的 to_review 会用 `mark_pending_and_group` 另起组、文章进两个组。 |
| 开 | 无 to_review | 输出 group_id → 执行器 `grouped=True` → 不兜底另起组。**一个组**。 |

## 7. 死锁与并发分析

设计让 4 个并发 worker **只碰各自不相干的行**，从而避免死锁：

1. **组提前建好一次**（开线程池之前、单线程）。worker 跑起来时组已存在，无任何 worker 去 INSERT 组 → 不存在"4 worker 并发建同名组"的唯一键互锁。（现 daily_group 也是每 run 建一次，风险面不变大、反而更小。）
2. 每 worker 只：`UPDATE` 自己那篇文章的 `review_status`（各自不同 article_id，不撞）+ `INSERT` 一行 `ArticleGroupItem(group_id, 自己的 article_id)`（唯一键 `(group_id, article_id)` 各异，不撞）。
3. 多子行 INSERT 引用同一父组行 → InnoDB 只对父行加**共享 FK 锁**，共享锁互相兼容、不阻塞。**前提：append 时绝不 `UPDATE` 组行**（5.1 已规定）；否则抢父行排他锁会死锁。
4. **`sort_order` 不走 `SELECT MAX(...) FOR UPDATE`**（跨范围 gap 锁是 InnoDB 最易死锁写法）。改为建组时单线程读一次 max、进程内 `threading.Lock` + `itertools.count` 发号。**锁只护内存计数器自增，DB 追加在锁外并发**。

唯一**早已存在、非本次引入**的窄窗口：同日**两个 run 几乎同时首次建该日期组** → 撞唯一键。`resolve_or_create_daily_group` 用 IntegrityError + OperationalError 回滚回查兜住（比"4 worker 各自建"更不易触发）。

代价（诚实记录）：两 run 同日并发填同一组时，`sort_order` 可能**并列**（各自从内存计数发号）。`sort_order` 非唯一、允许并列，仅影响展示排序、不影响正确性——对"未审核库"分组无所谓。以"零死锁"换"偶发排序并列"，划算。

## 8. 数据流与边界

- 日期组 = `ArticleGroup`，名 `每日生成 · YYYY-MM-DD`，受 `(user_id, name)` 唯一约束保证"每天一个组"。跨天 → 组名变 → 自动新建次日组。
- **全失败留空组**：开关开 + count>0 时先建组；若所有生成都失败 → 留一个空的当天组。**这是预期行为**（同日后续运行 / 重试会填进同一组，最多一个空日期组/天，自愈），不做清理。
- **建组失败优雅降级**：`resolve_or_create_daily_group` 返回 None → 不流式、不输出 group_id → 文章照旧最后由 to_review / 执行器兜底成组，**不丢文章**。
- distribute 不受影响：照常消费 `article_ids`、照常走 `_validate_articles_approved` 审核门禁。流式产出的文章是 `pending`，仍需人工 approve 后才进分发——审批流不变。
- 时区统一 `GEO_SCHEDULER_TZ`（与 scheduler、现有 daily_group 同款）。

## 9. 测试计划

### 后端（pytest，MySQL，需 `GEO_TEST_DATABASE_URL`）

新增 `server/tests/test_streaming_daily_group.py`（不匹配 `test_pipeline*` glob，单独命名）：

- **service 级**：
  - `resolve_or_create_daily_group`：不存在 → 新建返回 (gid, 0)；已存在有 N 个成员 → 复用返回 (gid, maxsort+1)；软删同名 → 复活清空返回 (gid, 0)。
  - `append_article_to_group_pending`：标 pending + 插 item；重复同 article_id → 去重不报错；**不改组 `updated_at`/`version`**（断言组行未被动）。
  - OperationalError 加固：monkeypatch flush 触发一次 `OperationalError` → 回查复用同组（验证重试分支）。
- **节点级**：构造 `NodeRunContext`，`daily_group=True` 跑一次 → 每篇都 pending 且在当天组、节点输出含 `group_id`；同日跑两次 → 累加进同一组、`ArticleGroupItem` 不重复、`sort_order` 递增（或并列但不报错）。
  - `daily_group=False`（默认）→ 节点输出无 `group_id`、行为与旧版一致（回归）。
  - 中途失败：注入部分 `_one` 抛错 → 成功篇已在组、失败篇不在组、节点 `errors` 非空。
  - 建组失败降级：monkeypatch `resolve_or_create_daily_group` 返回 None → 不流式、输出无 group_id、article_ids 照常返回。
- **交互矩阵**（第 6 节）：
  - ai_generate(开) → to_review(关) → 守卫命中、**只有一个组**、文章不在第二个组里。
  - ai_generate(开) → to_review(开) → 一个组。
  - ai_generate(开) → 无 to_review → 执行器不兜底另起组（用现有 pipeline 跑通断言只一个组）。
  - ai_generate(关) → to_review(开/关) → 旧行为不变。
- **node-types**：`GET /api/pipelines/node-types` 的 ai_generate 段含 `daily_group` 且 `type=="toggle"`、`default==False`。

### 前端

- 无单测框架 → `pnpm --filter @geo/web typecheck` + `build` 为门禁。
- 手动验证：编辑器里 ai_generate 出现"边生成边进每日分组"开关；开启跑一个生成数较多的 pipeline，运行中刷新内容页能看到当天组里文章逐篇变多。

## 10. 实现任务拆分（供并行执行）

依赖：T1 → T2（节点依赖 service helper）。T3（to_review 守卫）与 T1/T2 解耦但同属后端。

- **T1（后端·service）**：`resolve_or_create_daily_group` + `append_article_to_group_pending`（含 OperationalError 加固、不动组行）+ service 级单测。
- **T2（后端·ai_generate 节点 + config_schema）**：`daily_group` 分支（flat + units 两路径流式进组、输出 group_id、建组失败降级）+ router config_schema 加 toggle + 节点级 / node-types 测试。依赖 T1。
- **T3（后端·to_review 守卫）**：上游已成组透传 + 交互矩阵测试。
- **集成**：ruff check / ruff format / mypy / pytest（后端）、typecheck / build（前端）全绿。

## 11. 自检清单（落地后核对）

- [ ] 默认（`daily_group` 关），现有 pipeline 行为零变化（节点输出无 group_id、整批最后成组）。
- [ ] `daily_group` 开：生成前组已建；每篇即时进组 + pending + commit；同日累加、去重、跨天新建。
- [ ] 中途失败：成功篇在组、失败篇不在组、run 终态 partial_failed。
- [ ] 交互矩阵四格全部符合第 6 节；守卫防住 ON+关 双重成组。
- [ ] 并发：append 不动组行；sort_order 用内存计数器、无 `FOR UPDATE`；resolve 兼容 IntegrityError + OperationalError。
- [ ] 后端测试全绿；前端 typecheck + build 全绿。
- [ ] CLAUDE.md pipelines 段补一句 ai_generate `daily_group` 流式进组说明。
