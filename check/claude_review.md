# 生产级代码审查报告 · Pipeline 编排引擎 + 智能体调度

> 审查人：Claude (Opus 4.8) ｜ 日期：2026-06-05
> 审查范围：`ba2c07f..54d1410`（PR #20 编排套件 / #21 加固 / #22 智能体管理），约 11.5k 行
> 核心：新增模块 `server/app/modules/pipelines/` + 对 `scheme_executor.py` / `articles/service.py` / `main.py` 的改动
> 方法：核心并发文件人工逐行通读 + 4 路并行子代理（重复 / 前端 / 迁移 / #22）+ 高危项回源核实

## 总体结论

架构骨架是**合格**的（事务边界、单行锁、claim 去重、迁移索引顺序都有正确设计），但带着 **3 个会在生产命中的高危问题**，全部属于典型 vibe-coding 特征——"单机单进程、happy-path 能跑"，一旦多进程 / 高峰 / 边界输入就塌。

**建议：合并前必须修掉 P1 三项。**

---

## 严重度速览

| # | 级别 | 问题 | 命中条件 | 置信度 |
|---|---|---|---|---|
| 1 | 🔴 P1 | 启动恢复全量复位，多进程会误杀别的进程正在跑的 run | 多 web worker | 高 |
| 2 | 🔴 P1 | 调度器精确匹配 `now.minute`，轮询间隔 >60s 永久漏跑；60s 默认也会漂移偶发漏跑 | 调度功能开启 | 高 |
| 3 | 🔴 P1 | 前端轮询竞态：切换工作流后在途请求回填错状态 / 误清新轮询 | 用户切换工作流 | 高（已回源） |
| 4 | 🟠 P2 | 无全局并发上限，高峰打爆 DB 连接池（仅 15 连接） | 多 pipeline 同点触发 | 中 |
| 5 | 🟠 P2 | `patch_pipeline` 改调度类型不清理过期字段，且无法 PATCH 清空 | 改调度 | 高 |
| 6 | 🟠 P2 | 跨午夜时间窗无法表达，校验器用误导信息硬拒 | 配夜间窗 | 高 |
| 7 | 🟡 P3 | 重复造轮子：3 份 recovery / 2 份 scheduler 脚手架 / 2 份 executor 骨架 | — | 高 |
| 8 | 🟡 P3 | 迁移 / 模型 JSON 字段 nullable 不一致（autogenerate 抖动） | — | 高 |
| 9 | 🟡 P3 | `Pipeline.name` `String(200)` vs 校验 ≤50 不一致 | — | 高 |
| 10 | 🟡 P3 | 测试偏 happy-path：调度漂移 / overlap 闸 / 并发 claim / 改 kind / 跨午夜窗均无覆盖 | — | 高 |
| 11 | 🟡 P3 | 前端 `VersionHistory` 回滚静默失败 | 回滚 500 | 高 |

---

## 🔴 P1-1 多进程下启动恢复会误杀正在执行的 run

**位置**：`server/app/modules/pipelines/recovery.py:15-33`、`server/app/modules/ai_generation/scheme_executor.py:217-240`、`server/app/main.py:110-123`

两个恢复函数把所有 `running`/`pending` 的 run **无条件**置为 `failed`，理由写在注释里：*"进程刚启动时没有任何 run 真正在执行"*。

这个前提只在**单 web 进程**成立。而恢复在 `main.py:110-123` 是**无条件**跑的（不受 `pipeline_scheduler_enabled` 开关约束），且 pipeline / scheme 的 run 都跑在 **web 进程的后台线程**里。生产 FastAPI 常用 `gunicorn -w N` / 多 uvicorn worker——此时 worker B 启动会把 worker A 正在跑的 run 标记成 `failed`，文章已生成但状态被打成失败，UI 与数据对不上。

**关键对比**：本仓库**早就有**正确范式——`tasks` 模块的 `recover_stuck_records`（`server/app/modules/tasks/service.py`）用 `lease_until < now` 租约判定，只回收真正过期的记录、复位成可重跑的 `pending`，正是为多实例安全设计的。这两个新函数照抄了它的"形"（启动复位），却扔掉了它的"魂"（租约），还**抄了两份**。典型 vibe-coding：复制范式但不理解它为什么那么写。

**修复方向**：要么沿用租约判定（给 run 加 `lease_until` / 心跳），要么把恢复收敛到单一 leader 进程（如只在 worker 进程跑），不要在每个 web 进程无条件全量复位。

---

## 🔴 P1-2 调度器精确匹配分钟，间隔 >60s 永久漏跑

**位置**：`server/app/modules/pipelines/schedule_calc.py:8-31`、`scheduler.py:118-126`、`server/app/core/config.py:61`

`current_slot` 要求 `now.minute == minute` **精确相等**才算到点；调度循环每 `interval` 秒轮询一次，而 `pipeline_scheduler_interval_seconds` 默认 60、下限 30、**无上限**。

把间隔设成 90s：轮询落在 0:00、1:30、3:00…**系统性跳过一半分钟值**。一个配在 `09:31` 的 daily 智能体，若 `:31` 这一分钟没被任何一次轮询命中，就**静默永不触发**——无报错、无日志。即便保持 60s 默认，`run_due_pipelines_once` 本身有 N 次串行 DB 往返，每轮耗时 T 会让轮询点逐渐向后漂移，累计漂过一分钟边界就漏掉那一分钟——对"定时发文"这种功能是实打实的可靠性 bug。

**根因**：把"是不是计划的那一分钟"和"轮询恰好落在那一分钟"耦合在了一起。
**修复方向**：claim 条件改成"自 `last_scheduled_run_at` 以来存在一个 `<= now` 的未认领 slot"（区间判定），而不是要求轮询点精确等于计划分钟。

---

## 🔴 P1-3 前端轮询竞态（已回源核实）

**位置**：`web/src/features/pipelines/PipelineEditor.tsx:23, 40-43, 100-125`

用**单例** `pollRef`（L23），轮询回调是 `async`（L106-L121）。`clearInterval` 只能阻止后续 tick，**无法中止已经卡在 `await getRun()` 的在途 tick**。

- **切换工作流回填错状态**：切换时 L40-43 清 interval + `setRunStatus(null)`，但 A 的在途请求一返回，L110 仍无条件 `setRunStatus(...)`，把 B 的状态行刷成 A 的状态。组件是按 prop 复用、切换不卸载（`PipelinesWorkspace.tsx:63` 无 `key`），脏写会留住。
- **误清新轮询**：终止 / 失败分支以 `pollRef.current != null` 为条件 `clearInterval(pollRef.current)`（L112、L116-117），但此时 `pollRef.current` 可能已被重新赋值为 B 的 interval——A 的残留 tick 会把 **B 的轮询**清掉，B 状态行卡死在 "running" 永不更新。

PR#21 宣称修了"前端轮询不再无限重试"——只对单工作流 happy-path 成立；真正的 bug 全在"边轮询边切换"的交互上，现有 cleanup 处理不了。
**修复方向**：每个轮询闭包捕获自己的 interval id（局部变量）做清理；回填前用 epoch / pipelineId 守卫比对。

---

## 🟠 P2 中等问题

### 4. 无全局并发上限，会打爆连接池

**位置**：`scheduler.py:103-105`、`nodes/ai_generate_node.py:43`、`server/app/db/session.py`

调度器对每个到期 pipeline `threading.Thread(...).start()` 裸起线程，ai_generate 节点内再 `ThreadPoolExecutor(max_workers=4)`。50 个 pipeline 同点触发 → 50 线程 × 4 = 200 路并发生文，而 DB 连接池只有 `pool_size=5 + max_overflow=10 = 15` → `QueuePool limit reached, timed out`。发布侧明明有 `MAX_CONCURRENT_RECORDS` 全局信号量挡这个，生文 / 编排侧完全没有。建议加跨 run 的全局并发闸。

### 5. `patch_pipeline` 留下过期调度字段

**位置**：`server/app/modules/pipelines/service.py:141-182`（L155、L174）

weekly→hourly 改完，旧的 `schedule_hour` / `schedule_weekday` 不清理；又因 `None` 被吞（与 CLAUDE.md 记的 `ArticleUpdate` 同款行为），PATCH 也无法清空。校验只看 merge 后的视图、不校验"与新 kind 无关的字段应为空"，脏数据对校验器隐形。建议改 kind 时联动清空无关字段。

### 6. 跨午夜时间窗无法表达

**位置**：`service.py:66`、`schedule_calc.py:34-37`

`validate_agent_fields` 强制 `window_start < window_end`，`in_window` 是单段 `start <= t <= end`。配 `22:00–06:00` 会被以"起须早于止"这种**误导性**错误拒掉。对"调度"功能是实打实的能力缺口，且报错让人摸不着头脑。

---

## 🟡 P3 重复造轮子 / 死代码 / 其它

### 7. 重复造轮子（用户重点关注项）

- **3 份恢复函数**：`recover_stuck_pipeline_runs` ≈ `recover_stuck_scheme_runs`（同一 commit 互抄、连报错文案 "进程重启：运行在上次执行中意外中断" 都一字不差），且都偏离了 tasks 的租约范式（见 P1-1）。
- **2 份调度脚手架**：`scheduler.py` 文件头自述"镜像 ai_generation.sync_scheduler"，`_stop` / `_thread` / `start` / `stop` / `_loop` 逐行克隆。
- **2 份 `create_run` / `run` / `aggregate` 三段式骨架**：pipeline 与 scheme executor 同构。值得注意的**分叉**：pipeline 的 `create_run` 加了 `with_for_update` 单活跃闸（`executor.py:21`），scheme 的没有——两个"对称"的克隆体一个加固了一个没加固。

> 注：成组逻辑已正确抽取成共享的 `mark_pending_and_group` 复用，这部分做得对，反衬出上面三处该抽没抽。

### 8. 迁移 / 模型 nullable 不一致

`pipeline_nodes.config`、`pipeline_runs.node_results/article_ids`、`pipelines.tags` 模型是 NOT NULL，迁移建成 `nullable=True`（MySQL 的 JSON 列本就不能加 server_default）。运行时无害，但 `alembic autogenerate` 会反复抖动。建议把模型字段改 `nullable=True` 对齐。

### 9. `Pipeline.name` 长度上限不一致

`models.py:27` 是 `String(200)`，但 `validate_agent_fields` 限 ≤50。无实害，建议统一。

### 10. 测试偏 happy-path

调度漂移漏跑、运行中 overlap 闸、并发 claim 竞争、PATCH 改 kind、跨午夜窗——全无覆盖；`test_validate_window_order` 反而把"夜间窗应被拒"固化成了断言。

### 11. 前端 `VersionHistory` 回滚静默失败

`web/src/features/pipelines/VersionHistory.tsx:16-20` 回滚 `await` 无 try/catch，500 时既不刷新也不报错，按钮像坏了。

---

## 死代码核查（结论：基本干净，这点做得不错）

路由 `get_version` / `list_runs`、`NodeRunContext.upstream` 都有显式"预留勿当死代码删"注释，按规则不算 flag。唯一可议的 `_assign_temp_cover_from_bucket` 硬编码 bucket `"cantingyangchengji"` 落在共享生文路径——但**经核实它是 PR#19 引入的**（本次范围基线），不算这两个 PR 的账，已标 `[临时]` 带删除说明。

## 死锁核查（结论：无）

`with_for_update` 只在 `create_run` / `publish_draft` 锁**单个 Pipeline 行**，加锁对象一致、无 AB-BA 交叉；`delete_pipeline` 手动删子表与 FK CASCADE 冗余但无害。未发现经典死锁。

---

## 亮点（客观）

claim 用条件 `UPDATE ... rowcount==1` 去重、`create_run` 单活跃闸、`publish_draft` 行锁串行化 version_no、迁移 0039 "先建新唯一索引再删旧索引"规避 MySQL 1553、上游失败传播 + 成组失败降级 run 状态、`schedule_calc` / `flow_meta` 拆成无 DB 纯函数易测——骨架设计意识是在线的，问题集中在"多进程 / 高峰 / 边界"这些 vibe-coding 一贯的盲区。

---

## 建议修复优先级

1. **合并前必修**：P1-1（恢复加租约或收敛 leader）、P1-2（区间判定替代精确分钟匹配）、P1-3（轮询闭包捕获自身 interval + 回填守卫）。
2. **本迭代内**：P2-4（全局并发闸）、P2-5（改 kind 联动清字段）、P2-6（支持 / 明示跨午夜窗）。
3. **技术债排期**：抽掉 3 份 recovery / 2 份 scheduler 的重复；补调度漂移、overlap、并发 claim 的测试；对齐 nullable。

---

# 第二轮复核（Re-check + 遗漏补充）

> 对一审结论逐条回源证伪 + 补读一审未深入的区域 + 交叉比对 `check/` 内另两份 AI 审查（`DeepSeek_review.md` / `codex.md`），所有候选独立核实后再采纳。

## A. 对一审 P1/P2 的校准（有 1 处需降级，1 处被加强）

| 一审条目 | 复核动作 | 结论 |
|---|---|---|
| P1-1 多进程恢复误杀 | 查部署：`Dockerfile`/`Dockerfile.app`/`DEPLOYMENT.md` 的 web 启动均为 `uvicorn server.app.main:app`（**单进程、无 `--workers`**）；worker 进程（`server/worker/executor.py:220`）只跑 `recover_stuck_records`、**不**跑 pipeline/scheme 恢复 | **降级为"潜在地雷"**：当前默认部署单进程 → 不触发。但代码无任何护栏，且假设没写进文档——一旦给 web 加 `--workers N` / gunicorn / `--scale app=N` 就静默损坏 run 状态。结论本身成立，但"必修 before merge"应改为"扩容 web 层前必修" |
| P1-3 前端轮询竞态 | 查 `PipelinesWorkspace.tsx:63`：`<PipelineEditor pipelineId={selectedId}/>` **无 `key`** | **确认且加强**：切换不重挂载，`runStatus`/`pollRef` 跨切换存活，脏写必然留住。仍是最该先修的 P1（正常点点点就触发） |
| P1-2 调度漂移漏跑 | 复核 `current_slot` 采样逻辑 + `pipeline_scheduler_enabled` 默认值 | **确认**：补充说明调度默认**关闭**（`config.py` `pipeline_scheduler_enabled=False`），仅启用 #22 调度功能时活跃；>60s 间隔为系统性漏跑，60s 默认为漂移偶发漏跑 |

## B. 一审遗漏（新增，已逐条回源核实）

**A1 ｜🟠 MEDIUM ｜ AI 生成文章"生而 approved"，审核不变量靠 best-effort 后置翻转维系（弱不变量）**
`Article.review_status` 默认 `"approved"`（`articles/models.py:113-114`），`create_article` 不覆盖（`service.py:159-170`）。模型注释自陈不变量"AI 生成的文章应为 pending（未审）"，但 pipeline 的 `ai_generate` 节点与 scheme 都靠 **run 后** `mark_pending_and_group` 才翻成 pending，而该函数是 best-effort、**任何异常吞掉返回 None**（`articles/service.py:531-535`）。后置翻转一旦失败，这批从未经人工审核的 AI 文章就**永久停留在 `approved`**，后续被人当"已审"直接分发。
**注**：一审子代理曾报"同管道 generate→distribute 可直接发布未审文章"为 CRITICAL——**经我核实不可达**（`_article_ids_for_task` 的 `group_round_robin` 只从已成组的 `ArticleGroupItem` 取文章，而新生成文章在 run 期间未成组，distribute 也只做 group 分发）。故**不是直接发布漏洞**，但"生而 approved + 脆弱后置翻转"是真实治理隐患。
**修复方向**：在生成源头（`ai_generate_node` / `article_writer`）就建成 `review_status="pending"`，别依赖 run 后翻转。

**A2 ｜🟠 MEDIUM ｜ flow_meta 异常逃逸 per-node try，run 永久卡 `running`**
`run_pipeline` 的 `should_skip()`（`executor.py:100`）和 `apply_input_mapping()`（`executor.py:104`）在 **per-node `try` 之外**（try 始于 105），且整个节点循环**无顶层 try**。而 `DraftSave.snapshot` 是裸 `dict`、发布/运行前**零结构校验**（`schemas.py:68`、`snapshot.py:27`）。用户存一个畸形 `flow_meta`（如 `condition` 不是 dict）→ `cond.get(...)` 抛 `AttributeError` → 逃逸节点循环 → 末尾状态写回块（`executor.py:150+`）永不执行 → run 卡 `running`；router `_runner`（`router.py:262`）只 log 不回写。直到下次重启恢复才复位。
**修复方向**：节点循环包顶层 try（异常→标 run failed）；发布前校验 snapshot 结构。

**A3 ｜🟠 MEDIUM（条件性）｜ 0040 `tags` 加为 NULL 无回填，旧行读取 500**
`0040` 把 `tags` 加为 `sa.JSON(), nullable=True`、**无 `server_default`/无回填 UPDATE**（MySQL 的 JSON 列也加不了 server_default）。`PipelineRead.tags` 是 `list[str] = []`（**非 Optional**，`schemas.py:52`）。在 PR#20 部署后、PR#22 部署前创建过的 pipeline 行 `tags=NULL` → `PipelineRead.model_validate(p)`（`router.py:42`）对 `None` 校验 `list[str]` 抛错 → `GET /api/pipelines` 列表/详情 **500**，智能体管理 UI 整页挂。
**修复方向**：0040 加一句 `UPDATE pipelines SET tags='[]' WHERE tags IS NULL`，或把 `PipelineRead.tags` 设为可选并兜底。

**A4 ｜🟡 LOW-MEDIUM ｜「编辑流程」跳错智能体（已确认）**
`AgentManagementWorkspace` 的 `onEditFlow` 形参是 `(id: number)=>void`、调用 `onEditFlow(p.id)`（`AgentManagementWorkspace.tsx:44,118`），但 `App.tsx:117` 接成 `onEditFlow={() => handleNavClick("pipelines")}` —— **把 id 丢了**。点智能体 #5 的"编辑流程"只切到编排页，`PipelinesWorkspace` 默认选中第一个 pipeline → 用户**编辑到错的流程**。

**A5 ｜🟡 LOW-MEDIUM ｜ 被跳过的上游 → 下游静默回退 config（非阻断）**
被 `should_skip` 跳过的节点只写 `node_results[idx]={"skipped":True}`、**不写 `context[idx]`**（`executor.py:101` vs 116）。下游若 `dependsOnIndex` 指向它，`context.get(idx,{})` 得空 → `apply_input_mapping` 返回 `{}` → 节点静默回退 `config`（如 `ai_generate` 用 `cfg.get("question_text")`）。`failed_indices` 阻断只覆盖**失败**上游、不覆盖**跳过**上游。下游会拿过期 config 跑而非被阻断/跳过。

**A6 ｜🟡 LOW ｜ `flow_meta` `contains` 数字误匹配**
`should_skip` 对 `contains` 做 `expected in actual`（字符串子串，`flow_meta.py:33`），且 `actual=str(raw)`。`value="1"` 对 `count=12` → `"1" in "12"` 为真 → 误判命中。数字字段的条件判断会错。

**A7 ｜🟡 LOW ｜ 调度先提交 claim 再建 run，建失败则静默丢槽**
`run_due_pipelines_once` 先 `UPDATE ... last_scheduled_run_at` + `commit`（`scheduler.py:92-94`），再 `create_run`（`:98`）。若 `create_run` 抛（并发活跃 run / pipeline 被删的竞态）→ 槽已认领但无 run，且**到下个 slot 才会再试**。与 P1-2 的"漂移漏跑"是不同成因的同类后果（静默少跑一次）。

**A8 ｜🟡 LOW ｜（需团队拍板）admin 拥有的定时管道跨租户分发**
`distribute_node.py:21-33` 用**管道属主**的 role 调 `create_task`；属主是 admin 时 `create_task` 把 `user_id_filter=None`、关闭账号/分组归属校验（`tasks/service.py:508,567`）。手动跑属"既有 admin 越权"语义；但**调度器**（`scheduler.py:97-99`）以 `p.user_id` 无人值守自动建 run —— admin 拥有的定时管道会按 cron 静默跨租户分发。建议显式决策而非默认放行。

*（另：`_next_version_no` 全表 `max()`、`run_pipeline` 收尾多开一个 session、`publish_draft` 行锁覆盖整段节点重建——均为小性能/锁范围问题，低优先，列此备查。）*

## C. 明确驳回的假阳性（复核中证伪）

- **「generate→distribute 同管道直接发布未审 AI 文章」**（子代理报 CRITICAL）：**不可达**，理由见 A1 注。真实问题是 A1 的弱不变量，严重度 MEDIUM 而非 CRITICAL。
- **「`onRun` 失败后 UI 卡在 running」**（DeepSeek）：**误读求值顺序**。`PipelineEditor.tsx:102-103` 是先 `await startRun()` 再 `setRunStatus("running")`；`startRun` 抛 409 时根本走不到 `setRunStatus`，状态不会被置 running。驳回。
- **迁移多头 / 0038-0040 链断裂**：复核确认链线性、单头、约束/索引末态与模型一致（0039 先建唯一索引再删旧索引规避 MySQL 1553、0039 补 CASCADE）。无问题。
- **`_aggregate_run` 读写竞态**：`ThreadPoolExecutor` 上下文退出已 join 全部 worker 再聚合，无并发读写。无问题。

## D. 复核后的修复优先级（覆盖一审）

1. **正常使用即触发，优先**：P1-3 前端轮询竞态、A4「编辑流程」跳错智能体。
2. **启用对应功能即触发**：P1-2 调度漂移（启调度）、A1 AI 文章生而 approved（启生文）、A2 畸形 flow_meta 卡 running、A3 `tags` NULL→500（跨版本部署）。
3. **扩容/边界才触发**：P1-1 多进程恢复（加 web worker 前必修）、P2-4 并发闸、A8 定时跨租户分发（拍板）。
4. **打磨**：A5 跳过上游回退、A6 contains 数字、A7 丢槽、各小性能项、重复造轮子收敛、补测试。

**总评不变**：骨架合格，但审核门禁、调度可靠性、多进程假设、前端并发这四类是 vibe-coding 的系统性盲区，建议按上表 1/2 档在合并前清掉。
