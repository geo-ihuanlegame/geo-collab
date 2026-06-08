# 自动分发内容工作流 — 设计方案

- **日期**：2026-06-05
- **聚焦**：新增「自动分发内容」分发型工作流 = `已审核待发布 → 内容分发`。第一个节点 **已审核待发布** 从内容管理直接取「已审核 **且尚未分发过**」的内容，第二个节点把上游 article_ids round-robin 分发到所选账号（**单选或多选均可**）。配合已有定时调度器 → 定时自动分发智能体。
- **落地工程**：`geo-collab` 主仓库。参考项目（content-library-public / pc-admin-conetnt-library-public）只读、仅作参照。
- **前置**：pipelines 引擎 + 审核态 + `article_group_source`/`distribute` 节点 + 定时调度 + tasks 分发引擎均已在 main。

> **整体愿景（用户确认）**：唯一的人工步骤是**每天到内容管理审核内容**；上游生产（AI生文智能体，已实现）与下游分发（本特性）都由工作流**定时自动触发**。所以「已审核待发布」必须只取"已审核 + 未分发过"的内容并自动去重——这样每天的定时分发**只发新过审的、发过的不再发**。
>
> **参考依据**：① 参考项目 content-library-public 的分发模型（按内容取数据，来源含"已审核内容库"，账号 round-robin；**它不做去重**）；② 原系统现有的「自动分发」按钮（`POST /api/tasks/auto-distribute` → `AutoDistributeRequest{article_id|group_id, account_ids[], name}` → `create_task`，账号 1..N round-robin）。本设计复用同一 `create_task` 派发与审核门禁，**新增自动去重**（geo-collab 有 `PublishRecord.article_id` 可判定已分发），更适合无人值守的定时分发。

---

## 1. 背景与复用

- 现有 `article_group_source` 节点需要**预先建好的分组**（`group_id`）；`distribute` 节点只吃 `group_id`（`group_round_robin`）。本次新增"直接从已审核内容取数据"的源节点，并让 distribute 支持 article_ids 列表。
- 复用：
  - 审核态：`Article.review_status ∈ {pending, approved}`；`articles/service.list_articles(..., review_status=...)` 支持按审核态过滤。
  - 已分发判定：`tasks/models.PublishRecord.article_id`（每篇分发产生 PublishRecord）。
  - 分发引擎：`tasks/service.create_task(db, user_id, TaskCreate, role=...)`，内部 `_article_ids_for_task` 取 article_ids、`_validate_articles_approved` 审核门禁、`_build_assignments(article_ids, accounts)` 已按 article_ids round-robin 派号。
  - 节点框架：`pipelines/nodes/base.py`（register/get_handler/NodeRunContext/NodeResult）。
  - 调度器：`pipelines/scheduler.py`（定时触发 run_pipeline）。

---

## 2. 目标 / 非目标

### 目标
1. 新节点 `approved_content_source`（**界面名「已审核待发布」**）：从内容管理取 `approved` 且未删除、**未分发过（去重）** 的文章，按上限取，输出 `article_ids`。
2. `distribute` 节点 + tasks 引擎**加性扩展**：支持按上游 `article_ids` 列表 round-robin 分发到所选账号（**单选或多选 1..N**，单账号即该批全部发到该账号）；新增 `task_type="article_round_robin"`，保留 `group_id` 旧路径兼容。
3. 空 article_ids（无新内容）→ distribute 安静跳过、不建任务（定时跑且无新内容时不报错）。
4. 前端 `/node-types` 增源节点 config；编辑器复用。
5. 与定时调度组合 → "每天自动分发新审核内容、发过不再发"。

### 非目标（YAGNI）
- 不改参考项目；不做"分类→账号映射"（参考有 DistributeAccountCategory，本次不做）。
- 不持久化 round-robin 位置（去重已保证不重发，每次运行是新批次）。
- 不改审核态模型；不新建分发/历史表（用现有 PublishTask/PublishRecord）。
- 不为每次运行建临时 ArticleGroup（避免内容管理分组堆积）。

---

## 3. 新节点 `approved_content_source`（界面名「已审核待发布」）

`server/app/modules/pipelines/nodes/approved_content_source.py`（节点 type 用 `approved_content_source`，`/node-types` 的 label 用「已审核待发布」）

> 语义：「已审核待发布」= 已审核（`review_status='approved'`）**且尚未分发过**（未进入分发引擎）。"待发布"正是靠去重（排除已有 `PublishRecord` 的文章）实现。

- config：
  - `limit: int`（默认 20，上限取多少篇；1..200 合理范围，越界夹紧）
  - `exclude_distributed: bool`（默认 True，去重：排除已分发过的；"已审核待发布"语义下应保持 True）
- 行为（自建 session）：
  1. 基础查询：`Article` where `review_status == "approved"` AND `is_deleted == False`（owner 限定 `user_id == ctx.user_id`，admin 放行——与其它节点一致从 `User.role` 取）。
  2. `exclude_distributed=True` 时：排除 `article_id IN (SELECT DISTINCT article_id FROM publish_records)`（已分发/已尝试）。
  3. `ORDER BY Article.updated_at DESC`，`LIMIT limit`。
  4. 输出 `NodeResult(output={"article_ids": [...]}, article_ids=[])`。空集合是正常结果（无新内容）。

> "已分发"定义 = 该文章存在任意 `PublishRecord`。够稳（不会重复入队）；不细分成功/失败，避免失败内容被反复重试刷屏（失败可人工在分发引擎重发）。

---

## 4. `distribute` 节点 + tasks 引擎扩展

### 4.1 tasks 加性扩展（`tasks/{schemas,service}.py`）
- `VALID_TASK_TYPES` 增 `"article_round_robin"`。
- `TaskCreate` 增 `article_ids: list[int] | None = None`。
- `_article_ids_for_task`：新增分支 `task_type == "article_round_robin"`：
  - `payload.article_ids` 为空 → `ClientError`（distribute 节点在调用前已拦空，见 4.2）。
  - 校验每篇存在、未删除、owner（`user_id` 限定或 admin）；返回该列表（保序）。
- 其余不变：`_validate_articles_approved` 仍对 article_ids 做审核门禁；`_build_assignments` 已支持任意 article_ids round-robin；`article_round_robin` 不限账号数（≥1）。
- `AutoDistributeRequest` / `/api/tasks/auto-distribute` 端点**不变**（本次扩展只服务于 pipeline 节点；REST 端点保持现状）。

### 4.2 distribute 节点（`distribute_node.py` 改造）
- 取数优先级：`article_ids = ctx.inputs.get("article_ids")`（上游已审核内容源）→ 若无则走旧 `group_id` 路径（兼容 article_group_source）。
- **空 article_ids（上游给了但为空列表）→ 节点直接返回 `NodeResult(output={"skipped": "无可分发内容"})`，不建任务**（定时跑无新内容时安静跳过，不抛错、不让 run 失败）。
- 有 article_ids：构造 `TaskCreate(task_type="article_round_robin", article_ids=[...], accounts=[round-robin], name=...)`，调 `create_task`（审核门禁 + 账号校验仍生效），输出 `{task_id}`。
- 有 group_id（无 article_ids）：维持现有 `group_round_robin` 路径不变。
- `account_ids` 仍必填（空 → ValidationError）。**单选或多选均可（1..N）**：现有 `distribute` 节点的 `accounts` 配置已是多选字段（编辑器 `accounts` 字段类型），选 1 个即"单选"（该批文章全部发到该账号），选多个即 round-robin 派发——无需新增"单选"分支，多选天然覆盖单选。

---

## 5. 前端

- `/api/pipelines/node-types` 增 `approved_content_source`：
  ```json
  {"type":"approved_content_source","label":"已审核待发布",
   "config_schema":[
     {"key":"limit","type":"number","label":"取多少篇(默认20)"},
     {"key":"exclude_distributed","type":"checkbox","label":"跳过已分发过的"}
   ]}
  ```
- 编辑器需支持 `checkbox` 字段类型（现有 config 渲染只有 text/number/textarea/article_group/accounts）→ 加一个 `checkbox` 分支（复用现有渲染模式）。
- distribute 节点 config（account_ids/name）不变；数据来源经 inputMapping 把上游 `article_ids` 映射进来（与现有 group_id 映射同理）。

---

## 6. 测试（@pytest.mark.mysql，monkeypatch 不需要——不真发布）

新增 `server/tests/test_auto_distribute.py`：
1. **源节点去重**：建 3 篇 approved 文章，其中 1 篇预置一条 `PublishRecord`（已分发）。run `approved_content_source(exclude_distributed=True, limit=10)` → 只返回另外 2 篇；`exclude_distributed=False` → 返回 3 篇；pending 文章不被取到。
2. **端到端成功**：approved 文章 + 账号夹具（参照 `test_pipeline_review_distribute.py` 的账号/审核夹具），建 `approved_content_source → distribute(account_ids)` pipeline（inputMapping `article_ids→article_ids`），run → run done、`node_results` 含 task_id、库里建了 `article_round_robin` PublishTask、PublishRecord 覆盖这些文章 round-robin 到账号。
3. **空集跳过**：所有 approved 文章都已分发 → source 返回空 → distribute `skipped`、不建任务、run done（不 failed）。
4. **审核门禁双保险**：直接给 distribute 传一篇 pending 文章的 article_ids（绕过 source）→ create_task 门禁 → 节点失败、run failed、无任务。
5. **tasks 单元**：`article_round_robin` 的 `_article_ids_for_task` 校验（空列表 ClientError、不存在文章 ClientError）。

前端：`pnpm --filter @geo/web typecheck && build`。

---

## 7. 关键决策（已与用户确认）

1. 源 = 已审核 + **未分发过（PublishRecord 去重）**，按 limit 取最新。
2. distribute 消费 article_ids 走**扩展 tasks（article_round_robin）**，不建临时分组（避免堆积）。
3. round-robin 位置不持久化（去重替代）。

## 8. 风险与缓解
- **后台线程 session**：节点/服务各自建 session、本线程 commit/close（与现有一致）。
- **去重子查询性能**：`publish_records.article_id` 已有索引；`NOT IN (select distinct ...)` 在数据量大时可改 `LEFT JOIN ... IS NULL`，实现时择优。
- **空集语义**：distribute 对空 article_ids 跳过（不报错）—— 定时自动分发的关键，测试覆盖。
- **与 PR #24 对齐**：main 刚合入 PR #24（审核绕过/并发等整改）；实现时先读当前 `create_task`/`_validate_articles_approved`/distribute_node 实际代码，按现状对接（勿假设旧签名）。
- **owner/admin**：源节点与 article_round_robin 校验均从 `User.role` 取，admin 放行，与既有节点一致。

## 9. 验收标准
1. `approved_content_source` 取已审核且未分发过的内容（去重生效），可配 limit；pending/已分发不取。
2. `approved_content_source → distribute` 对已审核内容建 `article_round_robin` 分发任务，文章 round-robin 派到账号；发布仍由现有 worker 异步执行。
3. 无新内容时 distribute 跳过、run 不失败；挂定时调度可"每天自动分发新内容、发过不再发"。
4. 给 distribute 传未审核内容 → 门禁拦截、run failed、无任务。
5. 不新建表、不改参考项目、不改审核模型；纯逻辑/集成/前端门禁全绿。
