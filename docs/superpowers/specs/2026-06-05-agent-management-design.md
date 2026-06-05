# geo-collab 智能体管理 + 定时调度 — 设计方案

- **日期**：2026-06-05
- **聚焦**：原始需求 Section 一（智能体管理 tab 置首）+ Section 二（智能体 CRUD / 字段 / 校验）+「运行时间」的真实定时调度。
- **落地工程**：`geo-collab` 主仓库。参考项目只读不可改。
- **前置**：`pipelines` 编排引擎 + 审核/分发节点已合入 main（PR #20，head `0038`）。

> 「智能体」= 现有 `Pipeline` + 运营元数据，**不新建实体**。智能体管理 tab 管元数据/调度/启用，工作流编排 tab 管节点图（已有）。

---

## 1. 背景与复用

- 现有 `Pipeline`（`server/app/modules/pipelines/models.py`）：`id/user_id/name/description/draft_snapshot/has_draft/created_at/updated_at`。本设计为其扩字段。
- 调度器镜像 `ai_generation/sync_scheduler.py`：`run_sync_once(session_factory)`（纯函数、可单测）+ `start_auto_sync(session_factory)`（后台线程 `while not stop: run_once; wait(interval)`）+ `create_app()` 在 env 开关下启动。
- 执行入口 `pipelines/executor.py:run_pipeline(run_id, session_factory)`（已有）；触发用 `create_run` + `Thread(run_pipeline)`（参照现有 router run 端点）。
- CRUD 在 `pipelines/{schemas,service,router}.py`（已有），本设计扩展之。
- 前端：导航 `web/src/types.ts:navItems` + `App.tsx` tab 渲染；feature 在 `web/src/features/pipelines/`。

---

## 2. 目标 / 非目标

### 目标
1. `Pipeline` 扩元数据：type / tags / ignore_exception / is_enabled / 调度字段。
2. 智能体 CRUD（复用并扩展现有 pipeline CRUD）+ 校验 + 删除二次确认。
3. **真实定时调度**：预设档位（每小时 / 每天 / 每周）+ 时间窗，后台线程按时自动触发 `run_pipeline`，多实例 claim 防重、运行中不重叠。
4. `ignore_exception` 真生效：执行器节点失败时 fail-fast（默认）或继续。
5. 前端「智能体管理」tab 置首 + 管理界面（列表 / 表单 / 调度选择器 / 立即运行 / 编辑流程跳转）。

### 非目标（YAGNI）
- 不新建智能体实体/表；不做独立审核库 tab；不改 worker；不支持视频类型（type 用 general 兜底）。
- 不做复杂 cron 表达式；不做多触发器/依赖触发。

---

## 3. 数据模型（`Pipeline` 扩字段，Alembic `0039` 接 `0038`）

```
type               String(20)  default 'general'   -- generation|distribution|general
tags               JSON        default list        -- list[str]，≤5
ignore_exception   Boolean     default False / server_default '0'
is_enabled         Boolean     default True / server_default '1'
schedule_kind      String(20)  default 'none'       -- none|hourly|daily|weekly
schedule_minute    Integer     nullable             -- 0-59（hourly/daily/weekly）
schedule_hour      Integer     nullable             -- 0-23（daily/weekly）
schedule_weekday   Integer     nullable             -- 0-6 周一=0（weekly）
window_start       Time        nullable             -- 时间窗起（含）
window_end         Time        nullable             -- 时间窗止（含）
last_scheduled_run_at  DateTime nullable            -- 上次定时触发的 slot（claim 防重）
```
迁移用条件式 `add_column`（参照 `0036`/`0037` 风格，幂等）；`CheckConstraint` 限 `type` 与 `schedule_kind` 取值。downgrade 反向 drop。

---

## 4. 校验（service 层，抛 `ValidationError`）

- `name`：必填、去空格后非空、长度 ≤50。
- `type` ∈ {generation, distribution, general}。
- `tags`：list[str]，长度 ≤5，每项去空格非空、整体去重。
- `schedule_kind` ∈ {none, hourly, daily, weekly}，且子字段一致：
  - hourly：需 `schedule_minute`（0-59）。
  - daily：需 `schedule_minute` + `schedule_hour`（0-23）。
  - weekly：需 minute + hour + `schedule_weekday`（0-6）。
  - none：忽略 schedule_* 子字段。
- 时间窗：要么都空，要么 `window_start < window_end`。
- 校验封装为 `validate_agent_fields(...)`，create 与 patch 共用。

---

## 5. 调度器（`server/app/modules/pipelines/scheduler.py`）

### 5.1 纯逻辑判定（可无 DB 单测）
`pipelines/schedule_calc.py`：
- `current_slot(kind, minute, hour, weekday, now) -> datetime | None`：返回"当前这一分钟所属的应触发 slot 时间"，不到点返回 None。
  - hourly：now.minute == minute → slot = now 截到分。
  - daily：now.hour==hour and now.minute==minute → slot。
  - weekly：再加 now.weekday()==weekday。
- `in_window(window_start, window_end, now) -> bool`：窗为空恒 True；否则 `start <= now.time() <= end`。

### 5.2 扫描+触发一轮（`run_due_pipelines_once(session_factory, now=None) -> int`）
- **时区**：调度按本地时区解释。`now = datetime.now(ZoneInfo(GEO_SCHEDULER_TZ))`（默认 `Asia/Shanghai`，可配置）。`current_slot` / `in_window` 都在该本地时区下比较 schedule_hour/minute/weekday 与 window。`last_scheduled_run_at` 存为该 slot 的 **UTC naive** 瞬间（`slot_local.astimezone(timezone.utc).replace(tzinfo=None)`），与列的 `utcnow()` 基准一致，便于 claim 的 `<` 比较。`run_due_pipelines_once(session_factory, now=None)` 的 `now` 参数为**带时区的本地 datetime**（单测传固定值，不依赖真实时钟/真实时区）。
- 查 `is_enabled=True AND schedule_kind != 'none'` 的 pipeline。对每个：
  1. `slot = current_slot(...)`；None 跳过。
  2. `in_window(...)` 否则跳过。
  3. 已有 `PipelineRun.status in (pending, running)` 跳过（不重叠）。
  4. 有已发布节点（`pipeline_nodes` 非空）否则跳过。
  5. **claim**：`UPDATE pipelines SET last_scheduled_run_at=:slot WHERE id=:id AND (last_scheduled_run_at IS NULL OR last_scheduled_run_at < :slot)`；`rowcount==1` 才算抢到（commit）。
  6. 抢到 → `create_run` + `Thread(target=run_pipeline, args=(run_id, session_factory), daemon=True).start()`。计数+1。
- best-effort：单个 pipeline 异常只记日志、不影响其它。返回触发数（便于单测）。

### 5.3 后台线程
`start_pipeline_scheduler(session_factory) -> bool` / `stop_pipeline_scheduler()`：`_loop: while not stop: run_due_pipelines_once(); stop.wait(interval)`，interval = `max(30, GEO_PIPELINE_SCHEDULER_INTERVAL_SECONDS默认60)`。`create_app()` 在 `GEO_PIPELINE_SCHEDULER_ENABLED=true` 时 `start_pipeline_scheduler(SessionLocal)`（紧随 `start_auto_sync` 之后）。新增 settings：`pipeline_scheduler_enabled` / `pipeline_scheduler_interval_seconds` / `scheduler_tz`（默认 `Asia/Shanghai`）（`core/config.py`，前缀 GEO_）。

---

## 6. 异常忽略接入执行器

`executor.run_pipeline` 读 `pipeline.ignore_exception`（在加载 nodes 的那段 session 里一并取）。节点循环中捕获到节点异常或 `result.output.errors` 时：
- 记 `node_results` + `had_failure=True`（现有）。
- **新增**：若 `ignore_exception is False` → `break`（停掉后续节点，fail-fast）；为 True → `continue`（现状）。
- 聚合状态不变（done/partial_failed/failed）。默认 False = fail-fast。

> 这是对现有"总是继续"行为的有意改动：分发流"源失败就别分发"等场景更正确。已有 mysql 测试若假设"失败后续仍跑"需相应调整（实现时检查 `test_pipelines_api.py` / `test_pipeline_review_distribute.py`，预期它们的节点链不受影响：现有用例要么全成功、要么 distribute 单节点失败）。

---

## 7. API

扩展现有（不新增端点）：
- `PipelineCreate` / `PipelinePatch`：加 `type / tags / ignore_exception / is_enabled / schedule_kind / schedule_minute / schedule_hour / schedule_weekday / window_start / window_end`（均可选；patch 走 `exclude_unset`）。
- `PipelineRead`：返回上述字段 + `last_scheduled_run_at`。
- `service.create_pipeline` / `patch_pipeline`：调 `validate_agent_fields` 后赋值。
- 时间字段序列化：`window_start/end` 用 `HH:MM:SS`（参照 tasks 的 `LocalTime` + `JsonFormat`，FastAPI/pydantic 用 `datetime.time`）。
- 删除二次确认是前端行为；后端 delete 已有。

---

## 8. 前端

- **导航置首**：`types.ts` `NavKey` 加 `"agents"`，`navItems` 把 `{key:"agents",label:"智能体管理",icon:Bot/Boxes}` 放到**数组首位**（在「工作流编排」「AI 生文」之前）。`App.tsx` 加渲染块。
- **`web/src/features/pipelines/AgentManagementWorkspace.tsx`**（新）：
  - 列表：名称 / 类型 / 标签 / 调度摘要（"每天 09:30" 之类）/ 启用开关 / 草稿标记 / 最近运行；操作：编辑、删除（`window.confirm` 二次确认）、立即运行、编辑流程（切到工作流编排并定位该 pipeline）。
  - 新建/编辑表单：名称、类型(下拉)、标签(≤5 标签输入)、异常忽略(开关)、启用(开关)、调度选择器（kind 下拉 → 按 kind 显隐 minute/hour/weekday）、时间窗(可选 起止)。时间均为**本地时间（Asia/Shanghai）**，表单旁标注"（北京时间）"。前端做基本校验，最终以后端为准。
- **API 客户端**：`web/src/api/pipelines.ts` 的 create/patch/类型扩字段；`web/src/types.ts` 的 `Pipeline` 扩字段。
- 复用现有运行轮询展示（可选，列表里显示最近 run 状态）。

---

## 9. 测试

- **纯逻辑单测（无 DB）**：`schedule_calc`（current_slot 各 kind 到点/不到点、in_window 边界）。
- **集成（@pytest.mark.mysql）**：
  1. CRUD + 校验：建/改 agent 带新字段；名称>50、tags>5、daily 缺 hour、window 反序 → 400。
  2. 调度触发：建一个 daily 且 now 命中 slot、有已发布节点、enabled 的 pipeline，monkeypatch `run_pipeline`，调 `run_due_pipelines_once(session_factory, now=固定命中时刻)` → 返回 1 且 `last_scheduled_run_at` 写入；再调一次（同 slot）→ 返回 0（claim 幂等）；窗外/disabled/无节点/运行中 → 0。
  3. ignore_exception：双节点 pipeline 第一个节点失败，`ignore_exception=False` → run failed 且第二节点未执行（node_results 无第二节点 / 标记未跑）；`True` → 第二节点仍执行。
- **前端**：`pnpm --filter @geo/web typecheck && build`。

---

## 10. 关键决策（已与用户确认）

1. 调度 = 预设档位（hourly/daily/weekly）+ 时间窗；时间按**本地时区 Asia/Shanghai**解释（`GEO_SCHEDULER_TZ` 可配置），存储仍 UTC。
2. 调度器 = web 后台线程（镜像 sync_scheduler）+ DB claim 防重 + 运行中不重叠 + env 开关。
3. type 枚举 = generation / distribution / general。
4. tags = Pipeline 上 JSON `list[str]`，≤5（自由标签，不新建分类实体）。
5. 「智能体」= Pipeline 扩字段（非新实体）。
6. `ignore_exception` 默认 False = fail-fast（改 executor 现有"总是继续"行为）。

## 11. 风险与缓解

- **时区**：调度按本地时区解释（`GEO_SCHEDULER_TZ` 默认 `Asia/Shanghai`，用 `zoneinfo.ZoneInfo`）。运营填"每天 09:30"= 北京时间 09:30。slot/window 比较在本地时区下进行；`last_scheduled_run_at` 仍存 UTC naive 瞬间（与 `utcnow()` 列基准一致）。DST 对 Asia/Shanghai 无影响（无夏令时）。UI 标注"（北京时间）"。
- **多实例防重**：claim 用条件 UPDATE + rowcount，幂等到 slot。
- **不重叠**：触发前查运行中 run；长任务跨过下一个 slot 时跳过该 slot（可接受）。
- **后台线程 session**：scheduler 自建 session、本线程 commit/close；触发的 `run_pipeline` 仍自管 session。
- **executor 行为变更**：fail-fast 默认——实现时跑现有 pipeline 集成测试确认未破坏（现有链路不依赖"失败后继续"）。
- **迁移**：写前 `ls server/alembic/versions/` 确认 head（现 `0038`），用 `0039`。

## 12. 验收标准

1. 可新建/编辑/删除智能体，字段含类型/标签/异常忽略/启用/调度；校验（名称≤50、类型枚举、标签≤5、调度子字段一致、窗有序）全部生效；删除有二次确认。
2. 开启调度后，到点的 enabled + 已发布智能体被自动触发运行一次（同 slot 不重复、运行中不重叠、窗外不跑）。
3. `ignore_exception=False` 时节点失败停掉后续；`True` 时继续。
4. 「智能体管理」tab 出现在导航首位；列表/表单/调度选择器可用；typecheck/build 绿。
5. 不新建实体/表；worker 未改；纯逻辑 + mysql + 前端门禁全绿。
