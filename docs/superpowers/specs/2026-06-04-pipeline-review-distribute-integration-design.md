# geo-collab 编排引擎打通审核 + 分发 — 设计方案

- **日期**：2026-06-04
- **聚焦**：在已有 `pipelines` 编排引擎上收尾两条工作流的审核 / 分发链路（原始需求 Section 四 的节点→审核库链路，Section 五 的内容→分发节点）。
- **落地工程**：`geo-collab` 主仓库。参考项目（content-library-public / pc-admin-conetnt-library-public）只读不可改。
- **前置**：`pipelines` 引擎已合入 main（见 `2026-06-04-geo-collab-pipeline-orchestration-design.md`）；审核态 / 自动分发机制已由 PR #19 合入 main。

> 本 spec 只做"引擎打通审核+分发"。独立审核库顶级 tab、智能体管理（Section 一/二）另起 spec，不在范围。

---

## 1. 背景与复用对象（全部已存在，禁止重建）

- **审核态**：`Article.review_status ∈ {pending, approved}`（默认 approved）。`articles/service.py` 有 `approve_article` / `compute_group_review_summary` 等；列表支持 `?review_status` 过滤。审核库 = 内容管理的「未审核 tab」，与内容管理库是同一 `articles` 表的两个视图（所以无需跨库同步 / 重试 / 告警）。
- **方案运行→pending+成组**：`ai_generation/scheme_executor.py:_group_run_articles(run_id, session_factory)` —— 把 run 产出文章标 `pending` 并归入新 `ArticleGroup`（名 `"{created_at:%Y/%m/%d %H:%M} · {scheme_name}"`），best-effort。本设计 Track A 镜像它。
- **分发**：`tasks/service.py:create_task(db, user_id, TaskCreate, role=...)` 是分发任务的统一入口，内部做**审核门禁**（`_validate_articles_approved`，未过审抛命名异常）+ 账号校验。`AutoDistributeRequest` + `/api/tasks/auto-distribute` 端点构造 `TaskCreate(task_type="group_round_robin", group_id=..., accounts=[round-robin])` 后调 `create_task`。本设计 Track B 直接复用 `create_task`。发布执行仍由现有 `server/worker/executor.py` 异步跑（单实例）。
- **引擎**：`pipelines/executor.py:run_pipeline(run_id, session_factory)` 线性遍历节点、聚合 run；`pipelines/nodes/base.py` 节点注册表（`register` / `get_handler` / `NodeRunContext{session_factory,user_id,config,inputs,upstream}` / `NodeResult{output,article_ids}`）；`flow_meta.py` 数据传递。`PipelineRun` 有 `article_ids` / `node_results`。

---

## 2. 目标与非目标

### 目标
1. **Track A**：含 `ai_generate` 的 pipeline 运行结束后，产出文章自动标 `pending` + 成组，进入审核库（与方案运行一致）。
2. **Track B**：新增 `article_group_source` 与 `distribute` 两个节点，使「已审核分组 → 分发」成为可编排的第二工作流；分发复用 `create_task` 的 round-robin + 审核门禁。
3. 前端属性面板支持两种新 config 字段类型：`article_group`（选分组）、`accounts`（多选账号），并补 `/node-types`。
4. mysql 集成测试覆盖两条链路。

### 非目标（YAGNI）
- 不新建任何审核 / 分发 / 分组表；不改 `articles` / `tasks` 数据模型。
- 不改 `scheme_executor`（Track A 不强制重构它复用新 helper —— 可后续再 DRY，本次控风险）。
- 不做独立审核库 tab、不做智能体管理。
- 不做"同一条 pipeline 既生成又分发"——生成型（止于 pending 审核）与分发型（始于 approved 分组）是两条独立 pipeline，人工审核夹在中间（门禁使然）。
- 分发节点不等待发布完成（建任务即返回 task_id），不做发布进度轮询（现有 task SSE / 记录已覆盖）。

---

## 3. Track A — pipeline 产出 → 审核库

### 3.1 可复用 helper（articles/service.py 新增）
```python
def mark_pending_and_group(
    session_factory, *, article_ids: list[int], user_id: int, base_name: str
) -> int | None:
    """把文章标 review_status='pending' 并归入一个新 ArticleGroup（名 base_name，
    撞 (user_id,name) 唯一约束时追加后缀）。返回 group_id 或 None。
    best-effort：失败记日志、不抛。用独立 session、本函数内 commit+close。"""
```
逻辑镜像 `_group_run_articles`（同名防御 + IntegrityError 兜底 + 顺序加 `ArticleGroupItem`）。放 articles/service 便于复用与单测。

### 3.2 executor 接入
`run_pipeline` 末尾（状态聚合、写回 run 之后）：若 `run.article_ids` 非空，取 `pipeline.name` 组 `base_name = "{run.created_at:%Y/%m/%d %H:%M} · {pipeline_name}"`，调 `mark_pending_and_group(session_factory, article_ids=run.article_ids, user_id=run.user_id, base_name=base_name)`。**best-effort，包 try/except，不改 run 状态**（与 scheme 一致）。pipeline_name 在末尾的写回 session 里取（`db.get(Pipeline, run.pipeline_id).name`）。

### 3.3 效果
pipeline 生成的文章 = `pending`，出现在「内容管理 · 未审核 tab」(=审核库)，可走现有 approve 流程；与方案运行产物行为一致。

---

## 4. Track B — 已审核分组 → 分发

### 4.1 节点 `article_group_source`（nodes/article_group_source.py）
- config：`{"group_id": int}`
- 行为：`db.get(ArticleGroup, group_id)`，校验存在、属当前 `ctx.user_id`（非 admin）、未删除；取组内未删除文章 id（按 sort_order）。输出 `{"group_id": group_id, "article_ids": [...]}`。
- 无效组 / 越权 → `ValidationError`。

### 4.2 节点 `distribute`（nodes/distribute_node.py）
- config：`{"account_ids": list[int], "name": str | null}`
- 输入：`group_id` 取自 `ctx.inputs.get("group_id")`（上游经 inputMapping）→ 兜底 `ctx.config.get("group_id")`。缺失 → `ValidationError`。
- 行为：取当前 user 的 role（`db.get(User, ctx.user_id).role`），构造
  ```python
  TaskCreate(name=name or f"自动分发 分组 {group_id}", task_type="group_round_robin",
             group_id=group_id,
             accounts=[TaskAccountInput(account_id=a, sort_order=i) for i,a in enumerate(account_ids)],
             stop_before_publish=False)
  ```
  调 `create_task(db, ctx.user_id, task_create, role=role)`（内部做**审核门禁**：组内有未过审文章 → 抛 `ClientError/ValidationError`，节点失败 → run `failed`）。`account_ids` 为空 → `ValidationError`。
- 输出：`{"task_id": task.id}`；`NodeResult.article_ids` 留空（分发不产文章）。
- 发布执行由现有 worker 异步完成；节点只负责建任务。

### 4.3 注册
`nodes/__init__.py` 增 import 触发注册：`article_group_source`、`distribute`（连同既有 input/ai_generate）。

---

## 5. API / node-types

`GET /api/pipelines/node-types` 增补两项 config_schema（驱动前端属性面板）：
```json
{"type":"article_group_source","label":"已审核分组源",
 "config_schema":[{"key":"group_id","type":"article_group","label":"内容分组"}]}
{"type":"distribute","label":"内容分发",
 "config_schema":[{"key":"account_ids","type":"accounts","label":"分发账号"},
                  {"key":"name","type":"text","label":"任务名(可空)"}]}
```
不新增 pipelines REST 端点（沿用现有 CRUD / draft / publish / run）。

---

## 6. 前端

`web/src/features/pipelines/PipelineEditor.tsx` 的 config 字段渲染（按 `config_schema[].type`）新增两种类型：
- `article_group`：下拉，选项来自现有 article-groups 列表 api（`web/src/api/articles.ts` 或 groups api；取 `?review_status` 不限，展示组名 + 审核进度）。值存 `group_id`(number)。
- `accounts`：多选，选项来自现有 accounts 列表 api（`web/src/api/accounts.ts`）。值存 `account_ids`(number[])。

其余（节点增删/排序、数据传递、草稿/发布/版本、运行轮询、node_results 展示 task_id/article_ids）复用现有编辑器。前端类型：`NodeTypeDef.config_schema[].type` 已是 string，无需改类型；若需要可加联合补充。

---

## 7. 测试（@pytest.mark.mysql，monkeypatch LLM）

新增 `server/tests/test_pipeline_review_distribute.py`：
1. **Track A**：建 input→ai_generate pipeline（monkeypatch `generate_article_from_prompt` 返回真实落库文章 id —— 用 helper 经 `create_article` 造 2 篇），publish + run；断言产出文章 `review_status=="pending"` 且存在一个含这些文章的新 `ArticleGroup`。
2. **Track B（成功）**：预置一个**全部 approved** 的分组，建 article_group_source→distribute pipeline（inputMapping 传 group_id），配 account_ids（用测试夹具账号），run；断言 run `done`、`node_results` 含 task_id、库中新建了 `group_round_robin` PublishTask。
3. **Track B（门禁）**：分组含 `pending` 文章 → distribute → run `failed`，无 task 创建。
4. **纯逻辑**：`mark_pending_and_group` 单测（成组 + pending + 同名后缀）可放 logic 测试或集成测试。

`article_group_source` 越权/无效组 → `ValidationError` 用例。
前端 `pnpm --filter @geo/web typecheck && build`。

---

## 8. 关键决策（已与用户确认）

1. 焦点 = 引擎打通审核+分发，**复用 PR #19**，不重建。
2. 第二工作流 = `article_group_source` + `distribute` 两节点（对称于 input+ai_generate，体现数据传递）。
3. 生成型与分发型为两条独立 pipeline（人工审核在中间）。

## 9. 风险与缓解
- **后台线程 session**：节点 / helper 各自建 session、本线程 commit+close（与现有一致）。
- **role 获取**：distribute 节点从 `db.get(User, ctx.user_id).role` 取 role 传给 `create_task`。
- **审核门禁**：未过审分组分发必失败 —— 这是预期行为，测试覆盖。
- **best-effort 成组**：Track A 成组失败不影响 run 状态（try/except + 日志），与 scheme 一致。
- **唯一约束差异**：组名 (user_id,name) 唯一约束在测试库存在、生产库已 drop —— helper 同时做同名防御 + IntegrityError 兜底（照搬 scheme 做法）。
- **DRY vs 风险**：暂不重构 scheme_executor 复用新 helper（避免动已上线代码）；后续可单独 DRY。

## 10. 验收标准
1. 含 ai_generate 的 pipeline run 后，产出文章 = pending 且成组，出现在审核库（未审核 tab）。
2. article_group_source→distribute pipeline 对**已审核**分组 run 成功，创建 round-robin 分发任务（task_id 回填 node_results），发布由 worker 异步执行。
3. 对**含未审核**文章的分组，distribute run 失败（门禁），不建任务。
4. 前端能在 distribute / source 节点属性面板选分组与多选账号；typecheck/build 绿。
5. 不新增任何审核/分发/分组表；scheme runner 行为不变。
