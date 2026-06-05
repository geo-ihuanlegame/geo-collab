# Codex 生产级快速审查报告

审查日期：2026-06-05

审查范围：最近两轮 PR 合入后的 `main`，重点覆盖 `pipelines/agent`、调度器、后台线程、节点执行、任务分发、审核门禁、迁移兼容、死代码和重复实现。

结论：当前语法和前端类型检查通过，但这轮代码的主要风险不在编译层，而在生产并发、异常路径、删除语义和迁移兼容。建议 P0/P1 修完前不要打开 `GEO_PIPELINE_SCHEDULER_ENABLED` 上生产。

## 验证结果

- 通过：`python -m compileall -q server/app/modules/pipelines server/app/modules/ai_generation server/app/modules/articles server/app/modules/tasks`
- 通过：`pnpm --filter @geo/web typecheck`
- 部分受阻：`python -m pytest server/tests/test_agent_management.py -q`
  - 结果：9 passed，4 failed
  - 失败原因：本机环境缺少 `slowapi`，导入 `server.app.main` 时 `ModuleNotFoundError: No module named 'slowapi'`
  - 该失败不是业务断言失败，但说明当前本地依赖环境不完整

## P0 阻断生产

### 1. 运行中删除 pipeline 会产生未送审文章

位置：

- `server/app/modules/pipelines/service.py:185`
- `server/app/modules/pipelines/executor.py:169`
- `server/app/modules/pipelines/executor.py:179`

问题：

`delete_pipeline()` 直接删除 `PipelineNode`、`PipelineVersion`、`PipelineRun`，没有检查是否存在 `pending/running` run。后台线程已经启动后仍会继续执行节点，AI 文章仍可能被创建；但后处理阶段会重新读取 `PipelineRun` 来取 `user_id`、`pipeline_id`。如果 run 已经被删除，`uid=None`，`mark_pending_and_group()` 不会被调用。

生产影响：

AI 文章会以 `create_article()` 默认的 `review_status='approved'` 落库，但不会进入待审核分组。这等于绕过审核门禁，后续可被直接发布。

建议修复：

- 删除 pipeline 前拒绝存在 `pending/running` run，返回 409。
- 或引入软删除，不物理删除活跃 run。
- 或在 `PipelineRun` 创建时冻结 `user_id`、`pipeline_name`、执行 snapshot，后处理不依赖 pipeline/run 记录仍存在。

### 2. Scheduler 先提交 slot claim，后创建 run，失败会吞掉本次调度

位置：

- `server/app/modules/pipelines/scheduler.py:89`
- `server/app/modules/pipelines/scheduler.py:94`
- `server/app/modules/pipelines/scheduler.py:98`

问题：

`run_due_pipelines_once()` 先更新 `last_scheduled_run_at` 并 `commit()`，然后才调用 `create_run()`。如果 `create_run()` 失败，例如并发 active run、pipeline 被删、DB 错误、锁等待失败，本 slot 已经被标记为执行过，后续扫描不会补跑。

生产影响：

定时任务静默漏跑。日志里只会看到单个 pipeline trigger failed，但数据上 `last_scheduled_run_at` 已推进，运营侧难以发现。

建议修复：

- claim 和 `create_run()` 必须在同一事务内完成。
- 只有 run 创建成功后才能提交 `last_scheduled_run_at`。
- 失败时必须 rollback claim。

## P1 高风险

### 3. 后台线程 crash 只打日志，不把 run 置 failed

位置：

- `server/app/modules/pipelines/router.py:262`
- `server/app/modules/pipelines/executor.py:100`
- `server/app/modules/pipelines/executor.py:104`
- `server/app/modules/pipelines/flow_meta.py:13`
- `server/app/modules/pipelines/flow_meta.py:24`

问题：

路由 `_runner()` 捕获异常后只打日志，不更新 `PipelineRun` 状态。`run_pipeline()` 内部也只有节点 handler 包在 try 里，`should_skip()` 和 `apply_input_mapping()` 在 try 外。用户保存了结构异常的 `flow_meta` 时，例如 `condition` 不是 dict、`inputMapping` 元素不是 dict，run 可能直接线程崩溃并卡在 `running`。

生产影响：

前端轮询会一直看到 `running`，只能等进程重启后的 recovery 才变 `failed`。如果线上进程长期不重启，这类 run 会长期僵死。

建议修复：

- `run_pipeline()` 顶层包一层兜底，任何未捕获异常都写回 `failed + error_message + completed_at`。
- 发布 draft 时校验 `flow_meta` schema。
- `flow_meta.py` 对输入结构做防御式校验，不要假定前端永远传对。

### 4. 无全局执行闸，pipeline 生文可打满 DB 池和 LLM

位置：

- `server/app/db/session.py:13`
- `server/app/db/session.py:14`
- `server/app/modules/pipelines/router.py:267`
- `server/app/modules/pipelines/scheduler.py:103`
- `server/app/modules/pipelines/nodes/ai_generate_node.py:17`
- `server/app/modules/pipelines/nodes/ai_generate_node.py:43`

问题：

DB engine 默认 `pool_size=5`、`max_overflow=10`。但 API 可以无限开 pipeline run 线程，scheduler 也可以开线程。每个 run 的 `ai_generate` 节点又固定 `ThreadPoolExecutor(max_workers=4)`，并且 `count` 没有限制，直接为 `range(count)` 提交 future。

生产影响：

一次错误配置或恶意请求可以创建大量 LLM 调用和 DB session，造成连接池耗尽、请求排队、写库超时，甚至影响正常发布 worker。

建议修复：

- 引入全局 pipeline run semaphore 或持久队列。
- 限制 `ai_generate.count` 最大值，例如单节点 1 到 20，按成本预算配置。
- pipeline 生文最好进入独立 worker，不要在 API 进程 daemon thread 中无上限执行。

### 5. 0040 迁移对存量 pipeline 不兼容

位置：

- `server/alembic/versions/0040_agent_fields.py:22`
- `server/app/modules/pipelines/schemas.py:52`
- `server/app/modules/pipelines/router.py:42`

问题：

0040 新增 `tags` 列时允许 NULL 且没有 backfill。ORM 模型给了 Python default，但这不会修复存量行。读模型 `PipelineRead.tags` 要求 `list[str]`，路由直接 `PipelineRead.model_validate(p)`。

生产影响：

线上已有 0038/0039 pipeline 的库升级到 0040 后，`tags=NULL` 的记录在列表或详情接口可能 500，智能体管理和工作流编排入口打不开。

建议修复：

- 迁移里补 `UPDATE pipelines SET tags = JSON_ARRAY() WHERE tags IS NULL`。
- 模型层把 `tags` 改成非空并设置 server default。
- `_to_read()` 做 `data["tags"] = data.get("tags") or []` 的兜底。

### 6. PipelineRun 没有冻结版本或快照，运行版本不确定

位置：

- `server/app/modules/pipelines/service.py:214`
- `server/app/modules/pipelines/executor.py:56`

问题：

`publish_draft()` 会删除并重建 live nodes；`run_pipeline()` 启动时才读取 live nodes。run 创建后到后台线程实际读取节点之间，如果有人发布了新版本，本次 run 执行的可能不是用户点击运行时的版本。

生产影响：

审计不可追溯，排障时无法解释某次 run 到底跑的是哪个版本。定时任务更明显，触发时刻和执行时刻之间的发布会改变运行内容。

建议修复：

- `PipelineRun` 增加 `version_id` 或 `snapshot`。
- 创建 run 时冻结 snapshot。
- executor 只读 run snapshot，不读当前 live nodes。

## P2 正确性和技术债

### 7. PATCH 无法清空时间窗等 nullable 字段

位置：

- `server/app/modules/pipelines/service.py:155`
- `server/app/modules/pipelines/service.py:174`
- `web/src/features/pipelines/AgentManagementWorkspace.tsx:69`

问题：

前端清空时间窗会发送 `window_start: null`、`window_end: null`。服务端 `patch_pipeline()` 对所有字段都用 `fields[k] is not None` 过滤，导致显式 null 被忽略。用户设置过时间窗后无法清空。

建议修复：

- 对 `description/window_start/window_end/schedule_*` 区分“未传”和“显式 null”。
- `PipelinePatch.model_dump(exclude_unset=True)` 已经能表达这个差异，不要再全局忽略 None。

### 8. `mark_pending_and_group()` 是 best-effort，但调用方语义不一致

位置：

- `server/app/modules/articles/service.py:475`
- `server/app/modules/pipelines/executor.py:180`
- `server/app/modules/ai_generation/scheme_executor.py:291`

问题：

helper 内部吞异常并返回 None。pipeline executor 会根据 None 降级 run 状态；scheme executor 只是调用，不处理返回值。这导致同样的“生文后送审/成组失败”，pipeline 和 scheme 的结果表达不一致。

建议修复：

- 统一语义。要么 helper 抛命名异常，由调用方决定状态；要么所有调用方都检查返回值并写错误。
- 对审核门禁相关逻辑，不建议长期 best-effort。

### 9. 临时封面兜底是硬编码生产逻辑

位置：

- `server/app/modules/ai_generation/scheme_executor.py:174`
- `server/app/modules/ai_generation/scheme_executor.py:354`
- `server/app/modules/ai_generation/scheme_executor.py:396`

问题：

`_TEMP_COVER_BUCKET = "cantingyangchengji"` 是硬编码临时逻辑，且随机图使用 `order_by(func.rand())`。数据量变大时性能差，业务上也不可控。

建议修复：

- 移除临时代码，改成正式的配置项或复用 image library selector。
- 随机取图不要用全表 `ORDER BY RAND()`，改为预采样 ID 或按索引范围抽取。

## 死代码和重复造轮子判断

### 可保留但必须继续标注的旧代码

- `server/app/modules/ai_generation/pipeline.py`
- `server/app/modules/ai_generation/router.py` 中旧 `/sessions` 410 路径
- `QuestionItem.status/article_id/CategoryUsage` 旧消费队列字段

这些已经在代码注释里说明是旧路径兼容或休眠，不建议本轮盲删。真正的问题是这些旧路径和新 pipeline/scheme 都有后台线程、ThreadPoolExecutor、生文、成组逻辑，长期会造成行为分叉。

### 重复实现风险

- scheme run 和 pipeline run 都在 API 进程里开 daemon thread。
- scheme run 和 pipeline `ai_generate` 都各自维护 `ThreadPoolExecutor(max_workers=4)`。
- pipeline 和 scheme 都做“生成文章后成组/送审”，但失败语义不一致。

建议抽一个统一的后台执行框架，至少统一：

- run 创建和 claim 事务
- 全局并发限制
- crash 后状态落库
- 文章成组/送审失败语义
- run snapshot 审计

## 建议修复顺序

1. P0：删除 pipeline 时拒绝活跃 run。
2. P0：scheduler claim 和 create_run 合并事务。
3. P1：run_pipeline 顶层异常兜底，线程 crash 必落 failed。
4. P1：flow_meta schema 校验。
5. P1：补 0040 tags backfill 和读出兜底。
6. P1：增加 pipeline 全局并发闸和 count 上限。
7. P1：PipelineRun 冻结 snapshot/version。
8. P2：修 PATCH 显式 null 清空。
9. P2：移除或配置化临时封面兜底。

## 总体结论

这批代码不是“不能跑”，而是典型 vibecoding 后的生产边界缺失：happy path 测试不少，但异常路径、删除竞态、调度事务、资源上限和迁移兼容不足。最危险的是审核绕过和定时漏跑，建议先修 P0，再考虑打开定时调度和智能体管理入口。

---

## 第二轮复核补充

复核动作：

- 对比 `check/DeepSeek_review.md` 与本报告。
- 重新核对 `pipelines`、`scheduler`、`executor`、`snapshot`、`AgentManagementWorkspace`、`App` 等路径。
- 新增验证：
  - 通过：`ruff check server/app/modules/pipelines server/app/modules/ai_generation/scheme_executor.py server/app/modules/articles/service.py server/app/modules/tasks/service.py`
  - 通过：`python -m pytest server/tests/test_agent_management.py -q -m "not mysql"`，结果 `9 passed, 4 deselected`
  - 通过：`pnpm --filter @geo/web typecheck`

### 复核结论

第一轮报告里的 P0/P1 判断仍成立。DeepSeek 报告中有部分问题被标得过重，例如 `type` 参数遮蔽内建函数不是 P0，当前 ruff 规则也不会因此失败；但它指出的 `_next_version_no` 全量取数、publish/run 锁竞争、状态先 done 后降级，确实是遗漏的中低优先级问题，下面补充。

## 补充 P1

### 10. Draft snapshot 完全未做结构校验，发布和运行都可能被非法图拖垮

位置：

- `server/app/modules/pipelines/schemas.py:68`
- `server/app/modules/pipelines/snapshot.py:27`
- `server/app/modules/pipelines/service.py:210`
- `server/app/modules/pipelines/service.py:219`
- `server/app/modules/pipelines/flow_meta.py:13`
- `server/app/modules/pipelines/flow_meta.py:24`

问题：

`DraftSave.snapshot` 是裸 `dict`。`snapshot_to_node_dicts()` 直接从 JSON 里取 `node_type/name/node_index/config/flow_meta`，没有校验：

- `schemaVersion` 是否支持。
- `nodes` 是否为 list。
- `node_type` 是否已注册。
- `node_index` 是否唯一、连续、非负。
- `dependsOnIndex` 是否指向已存在且更早的节点。
- `inputMapping` 是否为 `{from,to}` 列表。
- `condition.op` 是否在 `eq/neq/contains`。
- `config` 是否为 dict。
- `ai_generate.count` 是否在合理上限内。

前端会生成正常 snapshot，但 API 本身没有保证。非法 snapshot 轻则 publish 500，重则 run 线程在 `should_skip()` / `apply_input_mapping()` 处崩溃并卡 `running`。

建议修复：

- 为 snapshot 建 Pydantic schema，`save_draft` 或 `publish_draft` 前校验。
- 发布时强制 node index 唯一且拓扑合法。
- 对每种 node type 做最小 config 校验，尤其是 `count` 上限。

### 11. Scheduler 轮询缺少索引和集群级 owner，数据量上来后会变成周期性负载

位置：

- `server/app/modules/pipelines/scheduler.py:41`
- `server/alembic/versions/0038_pipelines.py:34`
- `server/alembic/versions/0040_agent_fields.py:21`

问题：

Scheduler 每轮查询：

`Pipeline.is_enabled == true AND Pipeline.schedule_kind != 'none'`

但迁移只给 `pipelines.user_id` 建了索引，没有给 `is_enabled/schedule_kind/last_scheduled_run_at` 建索引。调度默认 60 秒，最低 30 秒；pipeline 数量上来后会周期性全表扫。

另外，`start_pipeline_scheduler()` 是进程内 singleton。如果生产 API 多进程或多实例部署，每个进程都会启动自己的 scheduler。条件 UPDATE 可以降低同 slot 重复触发概率，但不能消除所有进程一起扫描和竞争 claim 的开销，也不能解决本报告 P0 中“claim 提前提交”的漏跑问题。

建议修复：

- 加索引，例如 `(is_enabled, schedule_kind)`，视查询策略补 `last_scheduled_run_at`。
- 生产只允许一个 scheduler owner，或做 DB lease/leader election。
- 至少在部署文档里明确 `GEO_PIPELINE_SCHEDULER_ENABLED` 只能开在单一 API 实例。

## 补充 P2

### 12. `_next_version_no()` 全量加载版本号，长期会退化

位置：

- `server/app/modules/pipelines/service.py:245`

问题：

当前实现把某个 pipeline 的所有 `version_no` 拉到 Python 内存后 `max(rows)`。频繁发布后，单个 pipeline 版本数上千时，这会变成不必要的全量读取。

建议修复：

改为 DB 聚合：

```python
select(func.max(PipelineVersion.version_no)).where(PipelineVersion.pipeline_id == pipeline_id)
```

### 13. `publish_draft()` 的行锁覆盖 DML 重建过程，和 run 创建存在锁竞争

位置：

- `server/app/modules/pipelines/service.py:207`
- `server/app/modules/pipelines/service.py:214`
- `server/app/modules/pipelines/service.py:228`
- `server/app/modules/pipelines/executor.py:21`

问题：

`publish_draft()` 对 pipeline 行 `with_for_update()` 后，在同一事务里删除 live nodes、逐个插入新 nodes、重新查询 live nodes、计算版本号、插入 version。`create_run()` 也会锁同一 pipeline 行。

这不一定构成死锁，但节点数变大时，发布会阻塞运行请求；运行请求也可能让用户感知为卡顿。

建议修复：

- 缩小锁范围，只保护版本号分配和发布状态切换。
- 或给 pipeline 增加版本计数字段，用原子 update 产生 `version_no`。
- 重建 nodes 与生成 snapshot 尽量不要在长时间持有 pipeline 行锁时做。

### 14. run 先写 `done`，后处理失败再降级，会让前端看到状态回滚

位置：

- `server/app/modules/pipelines/executor.py:152`
- `server/app/modules/pipelines/executor.py:159`
- `server/app/modules/pipelines/executor.py:190`
- `server/app/modules/pipelines/executor.py:196`

问题：

`run_pipeline()` 先提交 run 终态，然后才执行 `mark_pending_and_group()`。如果成组/送审失败，后续又把 `done` 降级为 `partial_failed`。前端轮询可能先看到 `done`，随后变成 `partial_failed`。

生产影响：

这不是数据丢失级别问题，但会造成 UI 状态闪烁和误判。运营可能在短时间窗口内以为 run 成功。

建议修复：

- 把文章成组/送审放入最终 run 状态提交前。
- 或把成组状态单独建字段，例如 `postprocess_status`，不要回滚主状态。

### 15. 智能体列表“编辑流程”没有选中对应 pipeline

位置：

- `web/src/features/pipelines/AgentManagementWorkspace.tsx:44`
- `web/src/features/pipelines/AgentManagementWorkspace.tsx:118`
- `web/src/App.tsx:117`
- `web/src/features/pipelines/PipelinesWorkspace.tsx:10`
- `web/src/features/pipelines/PipelinesWorkspace.tsx:16`

问题：

`AgentManagementWorkspace` 的 `onEditFlow` 类型是 `(id: number) => void`，点击时也传了 `p.id`。但 `App.tsx` 里写成 `onEditFlow={() => handleNavClick("pipelines")}`，完全忽略 id。进入 `PipelinesWorkspace` 后，它只会默认选第一个 pipeline，不一定是用户刚才点击的智能体。

生产影响：

这是用户流程 bug，不是后端生产事故。用户从智能体列表点“编辑流程”，可能编辑错对象。

建议修复：

- 在 `AppShell` 提升 `selectedPipelineId` 状态。
- `onEditFlow={(id) => { setSelectedPipelineId(id); handleNavClick("pipelines"); }}`
- `PipelinesWorkspace` 接收 `initialSelectedId/selectedId` 并在列表加载后选中对应项。

## 对 DeepSeek 报告的校正

以下 DeepSeek 条目不建议按其原优先级处理：

- `validate_agent_fields(..., type=...)` 遮蔽内建函数：可改名为 `agent_type`，但当前 ruff 规则未启用该检查，不能算 P0。
- `IntegrityError rollback 后复用 Session`：当前代码 rollback 后用 `db.get()` 重新读取，风险存在但不是首要生产事故。更好的做法是 `expire_all()` 或新 session，但优先级低于审核绕过、漏跑和无并发闸。
- 预留 endpoint：`get_version`、`list_runs` 已有注释说明预留用途，不建议仅因前端未调用就删除。更合理的是补测试或明确 API 文档。
- `schedule_calc` 秒级判断：调度器最低间隔被限制为 30 秒，当前逻辑不会在同一分钟重复触发成功，主要成本是多做一次扫描，不是正确性 P3。
- timezone 问题：当前 SQLAlchemy 连接设置了 MySQL session `time_zone='+00:00'`，`slot_utc` naive UTC 与 DB 写入策略基本一致。仍可长期改成 aware datetime，但不是当前 P1。

第二轮后的优先级不变：先修删除活跃 run、scheduler claim 事务、run crash 落库、snapshot 校验、0040 tags 迁移和并发闸。
