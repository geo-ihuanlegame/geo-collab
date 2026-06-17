# 资源耗尽 / 连接泄漏 / 并发失控 —— 根因整改实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 根除 2026-06-16 资源审计确认的全部 **[高]** 风险项（10 个），收敛到 5 条根因。直接目标：堵死「重演 #110 连接池耗尽」与「卡死靠重启才恢复」两条最致命的复发路径，并补上 **检测 + 留存 + 闸刹车** 三层防护（注意：不是"自动预测式预警"，主动 backpressure 由并发闸的超时/入队提供，可观测层负责检测与事后留存）。**整改以最小根因修复为主，不引入新框架。**

**Architecture:** 后端 FastAPI + SQLAlchemy/Alembic（MySQL only）；生文 / pipeline 编排跑在 API 进程后台线程（无独立 worker），发布跑在单实例 `server/worker/executor.py`；浏览器自动化 Playwright + Xvfb/x11vnc/websockify/noVNC。连接池现为 `pool_size=20 + max_overflow=40 = 60`、`pool_timeout=10s`（`server/app/db/session.py:35-39`，#110 已落地）。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2 / Alembic / pytest（MySQL，需 `GEO_TEST_DATABASE_URL`）。前端本次基本不涉及。

---

## 背景：审计结论与根因映射

审计原文见对话记录（2026-06-16）。10 个 **[高]** 项与根因 / 阶段映射：

| 高风险项 | 文件:行 | 根因 | 阶段 |
|---|---|---|---|
| #1 排版/配图单连接横跨 120s LLM+下载（崩溃同源） | `articles/ai_format.py:766-895` | A 慢 IO 持连接 | Phase 0 |
| #7 `recover_stuck_records/claims` 只在启动跑、不周期 | `worker/executor.py:237-243` vs `307-312` | C 卡死不自愈 | Phase 0 |
| #10 全仓无池/闸/run 指标 | 全局 | E 零可观测 | Phase 0 |
| #4 三信号量无全局预算、不计 fan-out | `pipelines/executor.py:26`、`scheme_executor.py:40`、`tasks/executor.py:68` | B 并发无治理 | Phase 1 |
| #8 publish 信号量 acquire 无超时 + 排队时间被算进预算 | `tasks/executor.py:641`、watchdog `285-290` | B | Phase 1 |
| #9 pipeline 信号量 acquire 无超时、慢 run 占槽 ~25min | `pipelines/executor.py:244` | B | Phase 1 |
| #5 scheme run 无活跃去重 + 无界 spawn 线程 | `ai_generation/scheme_router.py:237`、`284` | B/幂等 | Phase 2 |
| #6 publish 信号量进程内、web+worker 双进程不封顶 | `tasks/executor.py:68` | B/跨进程 | Phase 2 |
| #2 发布超时释放账号锁时 Playwright 线程可能仍在跑 | `tasks/executor.py:285-317` | D 外部资源回收 | Phase 3 |
| #3 进程 SIGKILL 不退则 display/port 永久泄漏 | `accounts/browser.py:812-836` | D | Phase 3 |

### 核心判断：顺序决定成败

**Task 1（慢 IO 不持连接）是全局最高杠杆，必须第一个做、且单独热修先上线。** 当前 `ai_format` 把一条 DB 连接钉住 120s+，是 4-worker fan-out 下 ~20 条连接被长期占用、逼近池上限 60 的**根本**。一旦改成毫秒级借还，并发数学整个变样——60 的池轻松扛住所有 fan-out，于是 Phase 1 的「三信号量无全局预算」从**会打满池**降级为**只需约束 CPU/内存/成本**。

---

## 评审修订记录（2026-06-16，两份外部评审 + 代码核实）

以下是把初版计划与真实代码核对后的修订（评审意见经逐条验证，采纳为主、精修一处）：

1. **Task 1 范围被低估（最大修正）**：`_maybe_insert_images`（[ai_format.py:672-701](../../server/app/modules/articles/ai_format.py#L672-L701)）的 for 循环逐位置交织 DB 读写与网络下载（`_web_fallback_fill_category` 693 内 `download_image`+`store_image_bytes`）。三段式只对 **`web_fallback=False`** 成立；**`web_fallback=True`**（ai_illustrate）必须把下载也剥到内存。→ Task 1 拆为 **1a / 1b / 1c**。
2. **detached ORM**：段1 close 后 `article` 已 detached，段3 不能 `db.refresh(article)`，必须重新 `get_article`。异常路径也要自己的短 session 写 `ai_format_error`/解锁。→ 写进 Task 1a 步骤。
3. **测试不能 flaky**：放弃 `sleep + 全局 checkedout() + "远低于60"`。改**确定性布尔断言**：进入被 patch 的慢 IO 那一刻，run_ai_format 名下无开启的 session。→ Task 1a Step 1。
4. **Task 4 #8 修法错了**：超时是**主线程 watchdog**（[executor.py:285-290](../../server/app/modules/tasks/executor.py#L285-L290) 用 submit 时盖的 `started_monotonic`），`acquire()` 在工作线程（641）。"挪 started_monotonic 一行"无效。→ 正确修法：信号量获取**移到主线程 submit 前**（非阻塞），拿到才 submit+盖戳、拿不到留 pending；释放点迁到现有账号锁释放处。
5. **删 `get_settings.cache_clear()` 是砍在用能力**：[786](../../server/app/modules/articles/ai_format.py#L786) 注释明说它支撑"运维中途改 Key 即时生效"。→ 从 Task 1 摘出，单列 **Task 1c 决策项**，不混进重构 PR。
6. **Task 5 建模 + 接线**：anyio 默认线程池（40）是 Task 1 后真正的稳态长持大头，须显式治理；断言只发 WARNING 且不接告警通道 = 噪音。→ Task 5 改为**钉死 anyio 线程池 + 反推池下限 + 接 Task 3 告警通道**。（精修：主动刹车不另造——它就是 Task 4 闸超时 + Task 7 入队的 backpressure。）
7. **Task 3 只有瞬时快照**：事故后进程一重启就无数据。→ 加**周期采样落盘**；Goal 措辞下调为"检测+留存"。
8. **纪律靠文档+一次性 grep 拦不住**：同源反模式已发作两次。→ 新增 **Task G 机制护栏**（运行期长持断言）。
9. **Task 7 无 worker 会静默卡 pending**：→ 配 WorkerHeartbeat 陈旧检测 + 告警。
10. **Phase 3 只手测一次会腐化**：→ 拆纯逻辑单测（台账记账）+ 容器冒烟进 CI。
11. **缺端到端负载复现**：单测证明不了 #110 那类并发场景。→ 新增 **Task ACC 负载签收脚本**，作为 Phase 0 的硬门禁。
12. **PR 粒度过粗**：→ Task 1a 单独热修先行，不再整个 Phase 0 捆一个 PR。

## 评审修订记录（第二轮，2026-06-16，已逐条代码核实）

13. **Task 5 anyio 建模再修正（最强）**：核实 SSE 端点是 `sync def`（[router.py:360](../../server/app/modules/tasks/router.py#L360)）+ sync 生成器，**每条流占 1 个 anyio 线程整段、却几乎不持 DB 连接**（每轮 `_SL2()` 查完即关 392-426）。故：(a) anyio 线程数 ≠ 持连接数；(b) 生文/pipeline 不走 anyio（自建池）；(c) **钉小 anyio 池会饿死 SSE/同步端点**。→ Task 5 改为：**不缩 anyio**；`anyio(默认40)+publish(5)+余量 ≤ 60` 是保守上界、默认即满足、无需钉小；若失败则**扩池/降 publish**而非缩 anyio；最终数值以 Task 3/ACC 实测稳态来源为准。
14. **Task 4 Step 5 闸/锁释放漏口（最实在落地缺陷）**：核实 [_start_runnable_records:365-421](../../server/app/modules/tasks/executor.py#L365-L421) submit 前已拿账号锁(365)+profile 锁(376)，且已有 try/except 释放两锁(418-420)。新加 `try_acquire(gate)` 必须：**放在账号锁之前**，失败 `return`（未拿任何锁）；成功后用 **`gate_transferred` 标志 + try/finally**——submit 成功才置转移、否则 finally 释放闸。否则槽位泄漏→发布全停。
15. **Task ACC 名实**：opt-in load 脚本只能**一次性基线签收**「单进程借还纪律使峰值 触顶→≪60」，非持续门禁、不证明跨进程（#110 多进程放大归 Task 6/7）。→ 措辞下调；持续防回归 = Task 1a 确定性单测 + Task G。
16. **Task 1c 多进程盲点**：`get_settings()` 是**每进程** lru_cache。`--workers N` 下 refresh 端点只刷接到请求的那个进程。→ 决策 A 加前提「确认 web 单进程」，否则用配置表带版本号；现状每次 cache_clear 反而每进程都拿最新。
17. **Task 8 回归不对等**：与 Task 9 对齐——锁所有权不变式由 Step 1 纯逻辑 mock 单测**进 CI 持续防回归**（强化断言），容器真卡死路径只能一次性实测。
18. **Task 5 断言注释别复述**：scheme 侧"×4 瞬时借连接"已核实；pipeline 节点 session 时长**未核实**前不写进基准（[[verify-dont-parrot-docs]]）。→ 加一步实测 pipeline 节点 checkout 时长。
19. **Task 1a 工作量**：「热修」指优先级先行，**本质仍是并发重构**，评审成本不低，需仔细 review。

---

## 执行顺序（修订版）

| 波次 | 内容 | 封堵 | 阻塞级别 |
|---|---|---|---|
| **Wave 0** | Task 1a 单独热修（web_fallback=False 三段式）+ Task ACC 负载签收 | #1 部分 | 立即、独立 PR、最先合 |
| Wave 1 | Task 1b（web_fallback=True 下载剥离）∥ Task 2（周期自愈）∥ Task 3（可观测+落盘） | #1 余 #7 #10 | Task 1b 与 2/3 文件不相交，可并行 |
| Wave 1.5 | Task G 机制护栏（接 Task 3 之后）、Task 1c 决策 | 防复发 | 排期 |
| Wave 2 | Task 4（闸+超时，含 publish 主线程 acquire）→ Task 5（连接预算断言，不缩 anyio） | #4 #8 #9 | 调大并发/缩池前必做；依赖 Wave 1 |
| Wave 3 | Task 6（scheme 幂等）∥ Task 7（web 入队+无worker告警）∥ Task 9（display/port 回收） | #5 #6 #3 | 排期，互不相交 |
| Wave 4 | Task 8（超时锁安全，接 Task 4 后，同改 executor.py） | #2 | 容器内验收 |

**通用约定**
- 后端测试：`set GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test` 后用 **env python 全路径** 跑 `python -m pytest <path> -q`（conda activate 在工具 shell 里不生效）。多 agent 并行时各用独立库名（`geo_test_w1/w2/...`，都含 "test"）。带 `build_test_app`/`@pytest.mark.mysql` 的需 DB，结束 `finally: test_app.cleanup()`。
- Phase 3 / Task 8-9 涉及 Xvfb/x11vnc/进程信号，**Windows 本地无此环境，纯逻辑部分本地跑、进程信号路径只能 Docker 内验**。
- service 层抛命名异常（`ClientError`/`ConflictError`/`ValidationError`），不抛裸 `ValueError`。
- 每个任务结束 commit；message 结尾加 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- 在默认分支上先开整改分支再提交。**Task 1a 一个独立 PR**；其余按波次/任务出 PR，粒度宁细勿粗。

---

# Phase 0 — 止血（#1 #7 #10）

## Task ACC: 负载复现签收脚本（Wave 0 一次性基线签收，先写）

**目的**：以数据签收「**单进程内** ai_format 借还纪律使峰值 checkedout 触顶→≪60」（参见 [[verify-dont-parrot-docs]]）。**范围限定单进程**——#110 的多进程/多人放大由 Task 6/7 的跨进程封顶覆盖，本脚本不涉。**这是一次性基线签收，非持续门禁**；持续防回归靠 Task 1a 确定性单测 + Task G 运行期断言。

**Files:**
- Add: `server/tests/load/test_pool_under_concurrency.py`（标 `@pytest.mark.mysql` + 自定义 `@pytest.mark.load`，opt-in 不进默认 CI gate）

- [x] **Step 1（已完成）:** `checkout`/`checkin` 事件监听器统计**峰值并发持连接**（事件驱动，非 sleep 采样）。落在 `server/tests/load/test_pool_under_concurrency.py`；`load` 标记 + `GEO_RUN_LOAD_TESTS=1` opt-in（conftest 已注册，默认不跑）。
- [x] **Step 2（已完成，含一处刻意偏离）:** 起 M=12 路并发 `run_ai_format`（mock LLM 可控耗时 0.5s、`threading.Barrier` 对齐保证重叠）。**N 路 SSE 循环刻意略去**——SSE 已在 #110 修成每轮短借（占 anyio 线程、~0 持连接，见评审第 13 条），加进来不改变 peak-checkout 指标、只添噪音；run_ai_format-only 更干净地隔离 Task 1a 的效果。
- [x] **Step 3（改造前/后均已实测）:** 脚本重设计为 **before/after 同跑**——`web_fallback=True` 路由到保留的 `_run_ai_format_single_session`（=改造前行为），`web_fallback=False` 走三段式；用 LLM 内 `Barrier(action=...)` 在「M 路全部停在 LLM 内」那一刻无竞态采样 `checkedout()`。指标为「LLM 期间被占用的连接数」（与池绝对容量无关）。结果：**before=12（=M）→ after=0**。
- [x] **Step 4（已完成）:** 改前/改后数字（before=12 / after=0）见上，附 Task 1a PR。

## Task 1a: `web_fallback=False` 路径连接持有纪律（**热修，最先合**，封堵 #1 主体）

**根因**：`run_ai_format`（[ai_format.py:766](../../server/app/modules/articles/ai_format.py#L766)）单 session 持有到 895，跨 `_call_litellm_completion`（825，timeout 默认 120s）。`web_fallback=False` 时 `_maybe_insert_images` 仅快 DB、唯一慢 IO 是 LLM —— 三段式干净成立。覆盖 scheme 配图 + 手动排版两条最常跑路径。

> ⚠️ 「热修」指**优先级先行**，本质仍是并发重构（拆段 + detached re-get + 异常短 session + 确定性 instrument），评审成本不低，需仔细 review，勿因"热修"二字轻视（评审第 19 条）。

**修法（三段，连接只在两端短暂出现）：**
- 段1（短借）：开 session → 第一道锁检查（`_article_lock_matches` 773）→ 读 `content_json`/可用分类 → 拼 `system_prompt` → `db.close()`。
- 段2（无连接）：`_call_litellm_completion`（825）+ 解析 JSON。`web_fallback=False` 下 `_maybe_insert_images` 的 DB 都是快查，可放段3。
- 段3（短借）：重开 session → **重新 `get_article(db, article_id)`（不是 refresh detached 对象）** → 第二道锁检查 → `_maybe_insert_images`（快 DB）→ 写回 + commit → close。
- **异常路径**：段2 抛错时，用一个 `with 短session` 的 helper 写 `ai_format_error` + `_unlock_ai_format`（[711](../../server/app/modules/articles/ai_format.py#L711) 已收 db 参数），不依赖外层长 session 的 finally。

**Files:**
- Modify: `server/app/modules/articles/ai_format.py`（`run_ai_format` 拆段；仅走 `web_fallback=False` 分支，`web_fallback=True` 暂保持旧行为并在代码里标 TODO 指向 Task 1b）
- Test: `server/tests/test_ai_format_connection_lifecycle.py`（新建）

- [x] **Step 1（确定性失败测试，已完成）**：`test_ai_format_connection_lifecycle.py`——patch `_call_litellm_completion`，在入口抓 `engine.pool.checkedout()` 断言 == 0（比"数开启 session"更直接的布尔判定，无 sleep）。已确认在旧代码上 **RED 为正确原因**（`held 1 connection during LLM, assert 1 == 0`）。第二例：LLM 抛错后断言 `ai_format_error` 已写 + 已解锁。
- [x] **Step 2（已完成）:** 三段式重构 `web_fallback=False`（`_ai_format_prepare` / 段2 LLM / `_ai_format_write_back`），保留两道锁检查与 `images_inserted` 语义；`web_fallback=True` 原样保留在 `_run_ai_format_single_session`（Task 1b 再处理）。
- [x] **Step 3（已完成）:** 段3 改 `get_article` re-get（不 refresh detached）；异常路径走 `_ai_format_finalize_error` 短 session 落错+解锁。
- [x] **Step 4（已完成）:** `test_ai_format_connection_lifecycle.py` 绿 + 回归 `test_ai_format` / `test_scheme_autoformat` / `test_ai_illustrate_node` / `test_scheme_runs` / `test_image_search_prompts` / `test_ai_writer_credentials` 全绿；ruff/format/mypy 均通过。
- [x] **Step 5（已完成）:** ACC 实测 **before(单 session)=12 → after(三段式)=0** 连接 held during LLM。

## Task 1b: `web_fallback=True` 路径下载剥离（封堵 #1 余下，ai_illustrate）

**根因**：`web_fallback=True` 时 `_maybe_insert_images` 在 for 循环里交织 DB 写（`get_or_create_companion_category` 680）、DB 读、网络下载（`_web_fallback_fill_category` 693）。把下载塞进"无连接段"不成立——必须重构数据流。

**修法**：两遍式——先一遍（短 session 或无连接）决策每个位置需要哪些"现有图 id / 需联网补图的栏目"；再无连接地把需要的图**下载到内存**；最后短 session 内 `store_image_bytes` 落库 + 选图 + 插入。`get_or_create_companion_category` 的写收进最后的短 session。

**Files:**
- Modify: `server/app/modules/articles/ai_format.py`（`_maybe_insert_images` + `_web_fallback_fill_category` 改造：下载与落库分离）
- Test: 扩 `test_ai_format_connection_lifecycle.py`，对 `web_fallback=True` 同样断言下载期间无开启 session

- [x] **Step 1（已完成）:** `test_ai_format_connection_lifecycle.py` 扩 `web_fallback=True` 用例，探针抓下载期 `checkedout()`；旧单 session 路径上 RED 为正确原因（`held 1 connection during download, assert 1 == 0`）。
- [x] **Step 2（已完成）:** 五段式重构——SEG1 prepare（短 session，解析 image_search 模板）→ SEG2 LLM（无连接）→ SEG3 `_web_fallback_decide`（短 session，定每位置「现有图 id / 需联网补图栏目」+ `get_or_create_companion_category` 写）→ SEG4 `_web_fallback_download`（无连接，下载入内存 FIFO 队列）→ SEG5 写回（短 session，`_maybe_insert_images(prefetched_downloads=...)` 从内存 `store_image_bytes`）。`_run_ai_format_single_session` 删除。
- [x] **Step 3（已完成）:** `web_fallback=False` 仍走 Task 1a 三段式、行为不变（回归 75 passed）。`_maybe_insert_images` 仅加可选 `prefetched_downloads` kwarg，`False` 路径传 `None` 保持同步 `_web_fallback_fill_category`。
- [x] **Step 4（已完成）:** 连接生命周期 4 + ai_illustrate/scheme/image_search 等回归全绿；ruff/format/mypy 通过。**附带**：Task ACC 负载脚本 `test_pool_under_concurrency.py` 因旧单 session 路径删除，转为「两路 fallback LLM 期间均 0 连接」稳态护栏（集成时一并改，opt-in 实测通过）。

## Task 1c（决策项，非编码）: `get_settings.cache_clear()` 去留

**背景**：[ai_format.py:786](../../server/app/modules/articles/ai_format.py#L786) + [baidu.py:211](../../server/app/shared/baidu.py#L211) 每次调用清全局 lru_cache，是"运维中途改 Key 即时生效"的在用通路；但也是性能拖累、破坏 `get_settings()` 的 lru_cache 契约。

- [ ] **前提核实（评审第 16 条）**：`get_settings()` 是**每进程** lru_cache。先确认 web 部署进程数——若 `uvicorn/gunicorn --workers N`（N>1），refresh 端点只刷接到请求的那个进程、其余仍用旧 Key（比现状更隐蔽地失效）；现状每次 `cache_clear()` 反而每进程都拿最新。
- [ ] **决策 A（仅当 web 单进程）**：加显式 `POST /api/system/refresh-settings`（admin）刷新配置后删两处 `cache_clear()`。**单独 PR**，不与 Task 1a/1b 混。
- [ ] **决策 A'（多进程部署）**：改用配置表带版本号 / 进程间信号，别用单进程 refresh 端点替换。
- [ ] **决策 B**：确认无人依赖中途改 Key（问运维），确认后直接删并记录到 CLAUDE.md。
- [ ] 未决前 **不动** 这两行（避免引入行为回归）。

## Task 2: worker 主循环周期复位卡死记录/认领（封堵 #7）

**根因**：`recover_stuck_records`（[service.py:358](../../server/app/modules/tasks/service.py#L358)）/`recover_stuck_task_claims`（[service.py:392](../../server/app/modules/tasks/service.py#L392)）**本就为周期设计、租约保护、自带 commit**，但主循环周期分支（[worker/executor.py:307-312](../../server/worker/executor.py#L307-L312)）只调 `_check_stuck_tasks`，二者仅在 `_startup`（237-243）跑一次。结果进程不重启时卡死记录不自愈、占着账号/profile 锁。CLAUDE.md 称"周期重跑"与代码不符。

**Files:**
- Modify: `server/worker/executor.py`（`_recovery_cycle % 60 == 0` 分支加两个 recover，包 try/except 日志）
- Modify: `CLAUDE.md`（修正「Task Execution」节）
- Test: `server/tests/test_worker_periodic_recovery.py`（新建）

- [x] **Step 1（已完成）:** `test_worker_periodic_recovery.py` 造过期 lease 的 running 记录 + 死认领 task，RED 为正确原因（`_periodic_recovery` 不存在 → 周期未跑 recover）。
- [x] **Step 2（已完成）:** 周期分支抽成 `_periodic_recovery(db)`，`% 60 == 0` 时跑 `recover_stuck_records` + `recover_stuck_task_claims` + 原有 `_check_stuck_tasks`，各包 try/except；两个 recover 不改签名/逻辑（仍租约保护、自带 commit）。
- [x] **Step 3（已完成）:** CLAUDE.md「Task Execution」节订正——明确周期复位经 `_periodic_recovery`、不只启动跑一次。
- [x] **Step 4（已完成）:** 2 passed，含护栏用例：未过期 lease 的在跑记录/认领不被误动；回归 worker/task 相关测试绿。

## Task 3: 可观测底座 —— 快照 + 周期采样落盘 + 阈值 WARN（封堵 #10）

**根因**：全仓无 `engine.pool.status()`、无闸占用、无活跃 run/过期 lease 数暴露；裸 `Semaphore` 占用读不出；事故后无留存。

**Files:**
- Add: `server/app/shared/resource_metrics.py`（采集：池状态 + 闸占用[Wave 2 接 ObservableGate] + 活跃 run 数 + 过期 lease 数）
- Modify: `server/app/modules/system/system_router.py`（`GET /api/system/db-pool`，`require_admin`）
- Modify: `server/app/main.py` 或后台线程（周期采样**落盘/打点** + checkedout/上限 >80% 升 WARNING；提供统一告警 hook 供 Task 5 接）
- Test: `server/tests/test_resource_metrics_api.py`（新建）

- [x] **Step 1（已完成）:** `test_resource_metrics_api.py`——占 3 连接后请求端点断言 `checked_out` 随占用上升 + `max`/`size` 在；operator 客户端 403。RED 为正确原因（端点未实现 → 404）。
- [x] **Step 2（已完成）:** `shared/resource_metrics.py:collect_resource_metrics()`——池 `size/checked_out/overflow/checked_in/max`（`max=pool_size+max_overflow`，读 `_max_overflow` 而非可能为负的 `overflow()`）；`gates: {}` + `gates_placeholder` 占位（Wave 2 接 `ObservableGate`）；`active_publish_records` / `expired_leases`（无 db 时为 `null`）。线程安全、依赖轻。
- [x] **Step 3（已完成）:** `GET /api/system/db-pool`（`require_admin`）；`start_resource_sampler(SessionLocal)` 守护线程周期采样 → 结构化日志落盘 + `checked_out/max > GEO_RESOURCE_METRICS_WARN_RATIO`（默认 0.8）经统一 `emit_resource_alert` 告警 hook（`set_alert_hook` 供 Task 5 换通道）；开关 `GEO_RESOURCE_METRICS_SAMPLING_ENABLED`（默认开、可关）+ `..._SAMPLE_INTERVAL_SECONDS`（60）。启动安全（try/except、不阻塞，create_app 0.8s 起）。
- [x] **Step 4（已完成）:** `tasks/executor.py` 发布池 `thread_name_prefix="publish"`、`scheme_executor.py` `="scheme-run"`。**偏离**：`pipelines/executor.py` 无 `ThreadPoolExecutor`（用 `threading.Semaphore` + router 起 `Thread`），未捏造执行器；scheduler 线程本已命名。
- [x] **Step 5（已完成）:** 端点测试 2 passed；集成后与 Task 1a/1b/2 同跑全绿。

## Task G: 机制护栏 —— 运行期长持连接断言（防 A 类复发）

**根因**：纪律靠"CLAUDE.md 一行 + 一次性 grep"拦不住（#110/#1 同源已两发）。

**修法**：SQLAlchemy `checkout` 事件记 checkout 时刻 + 轻量上下文；`checkin`（或周期巡检）时若某连接持有 > 阈值（如 30s）记 WARNING + 调用点线索。运行期捕获任何路径新引入的长持，不靠人自觉。开关 + 阈值走 settings，默认开、可关。

**Files:**
- Add: `server/app/shared/connection_watchdog.py`（事件监听 + 阈值 WARN）
- Modify: `server/app/db/session.py`（注册监听）
- Test: `server/tests/test_connection_watchdog.py`（新建，纯逻辑/事件 mock）

- [x] **Step 1（已完成）:** `test_connection_watchdog.py`（纯逻辑/事件 mock，无 DB）——假 connection_record + 注入 clock/alert：持有 31s>30s 触发告警且含时长/阈值/线程线索；短借 5s 不触发；未配对 checkin 安全。RED 为正确原因（模块未实现 → ModuleNotFoundError）。
- [x] **Step 2（已完成）:** `shared/connection_watchdog.py:ConnectionWatchdog`（checkout 记借出时刻+线程名于 `connection_record.info`，checkin 算持有时长超阈值经 `emit_resource_alert` 告警）+ `register_connection_watchdog(engine)`（幂等、env 开关）；`session.py` import 后注册到全局 engine。开关/阈值走 `GEO_CONNECTION_WATCHDOG_ENABLED`/`..._THRESHOLD_SECONDS`（默认开、30s），与池参数同走 os.environ（不进 Settings，符合该文件既有约定）。
- [x] **Step 3（已完成）:** 3 passed；create_app 冒烟（resource_metrics/system_status）通过——session.py 新 import 无循环、不破坏启动；正常短借不误报（checkin 时仅一次 dict pop+减法，无显著开销）；ruff/format/mypy 通过。

---

# Phase 1 — 并发治理（#4 #8 #9）

## Task 4: 统一可观测、带超时的并发闸（含 publish acquire 重构）

**根因**：三个裸 `threading.Semaphore`（pipeline=3、scheme=2、publish=5）：占用读不出、`acquire` 无超时、不计 run 内 ×4 fan-out。

**Files:**
- Add: `server/app/shared/concurrency.py`（`ObservableGate`：`acquire(timeout)->bool`、`try_acquire()->bool`、`release()`、`in_use`、`waiting`）
- Modify: `pipelines/executor.py:244`、`scheme_executor.py:192`、`tasks/executor.py`（acquire 重构，见下）
- Modify: `server/app/shared/resource_metrics.py`（接 `ObservableGate.in_use/waiting`）
- Test: `server/tests/test_observable_gate.py`（纯逻辑）+ publish 退避路径测试

- [x] **Step 1（已完成）:** `test_observable_gate.py`（纯逻辑、无 DB）——容量限制、`try_acquire` 非阻塞、`acquire(timeout)` 超时返回 False 且不增计数、`waiting` 反映阻塞线程数、release 后可再取、over-release 抛 ValueError 且不污染计数。RED 为正确原因（模块未实现）。
- [x] **Step 2（已完成）:** `shared/concurrency.py:ObservableGate`（`BoundedSemaphore` + 受锁计数 `in_use`/`waiting` + `snapshot()`）。5 passed，ruff/format/mypy 通过。
- [x] **Step 3（pipeline #9，已完成）**：`pipelines/executor.py` `_RUN_SEMAPHORE`→`_RUN_GATE`（ObservableGate），`run_pipeline` 改 `acquire(timeout=_run_acquire_timeout())`：超时→`_mark_run_failed`（写「等待并发槽位超时」）+ return；`finally` 释放，绝不泄漏。超时秒数走新 setting `pipeline_run_acquire_timeout_seconds`（默认 1800）。
- [x] **Step 4（scheme，已完成）**：`scheme_executor.py` 同上对称改造（`scheme_run_acquire_timeout_seconds` 默认 1800）。新 `test_run_gate_timeout.py` 确定性验证两路超时→failed 且不泄漏闸槽；既有 `test_pipeline_concurrency.py`/`test_scheme_run_concurrency.py` 契约测试改用 `_RUN_GATE`/`try_acquire` 续守 cap+release 不漏（23 passed）。
- [x] **Step 5（publish #8，已完成 —— 闸/锁释放无漏口）**：见下，已落地：`_global_publish_gate`（ObservableGate）主线程 `try_acquire()`（满了 return）+ `gate_transferred` 标志 + try/finally 兜底；记录退场处 `_retire_running_slot` 释放闸+账号锁（over-release 吞 ValueError 告警）；`_publish_record` 不再碰闸。顺带把 resource_metrics 的 gates 占位换成真实快照（`concurrency.register_gate` 注册 pipeline/scheme/publish 三闸）。新 `test_publish_gate_acquire.py`（worker 不碰闸 / 满闸不 submit / 账号锁占用零泄漏 / submit 成功移交）+ `test_concurrent_publish.py` 改用 in_use 断言。
- [ ] ~~原 Step 5 计划文本~~ 把 `_global_publish_sem` 改 `ObservableGate`，获取移到主线程 [_start_runnable_records](../../server/app/modules/tasks/executor.py#L329)。**顺序：`try_acquire(gate)` 放在 `_try_acquire_account_lock`（[365](../../server/app/modules/tasks/executor.py#L365)）之前**——失败直接 `return`（未拿任何锁、无需释放；全局槽满本轮不再填）。拿到槽后用 **`gate_transferred=False` 标志 + try/finally** 包住「拿账号锁→拿 profile 锁→claim→detach→submit」：**仅 submit 成功并登记 RunningRecord 后置 `gate_transferred=True`**（所有权移交 running 生命周期，由 [315/321/325](../../server/app/modules/tasks/executor.py#L315) 释放）；任何 submit 前的 continue/异常分支由 `finally: if not gate_transferred: gate.release()` 兜底。**worker 线程 `_publish_record` 不再 acquire/release**。排队时间不进 watchdog 预算（#8 根除）、跨任务封顶仍在、槽位无泄漏。
- [x] **Step 6（已完成）:** 17 passed（gate+concurrency+resource_metrics+observable_gate）；回归 tasks/state_machine/publish_validation/phase4/driver/pipeline/scheme/run_gate_timeout 58 passed；ruff check / format / mypy 通过。满闸 record 留 pending（退回 pending 不破坏账号锁/租约）已由 `test_full_gate_blocks_submission` 覆盖。

## Task 5: anyio 线程池治理 + 预算断言接告警（封堵 #4）

**根因**：三闸无全局上限；且 Task 1 后真正的稳态长持大头是 **anyio 同步端点线程池（默认 40）**，不治理它则任何池断言都失真。只发 WARNING、不接告警通道 = 噪音。

**修法（评审第二轮修正——不缩 anyio、先实测）：**
1. **先实测**：用 Task 3/ACC 数据回答「Task 1 后 web 进程里到底谁稳态长持连接」。已知：SSE 占 anyio 线程但**几乎不持连接**（[router.py:360](../../server/app/modules/tasks/router.py#L360) sync 生成器、每轮查完即关）；生文/pipeline 走自建 ThreadPoolExecutor + 自建 session、不走 anyio。
2. **断言（保守上界，默认即满足）**：`anyio_pool_size + publish_max + 余量 ≤ pool_size + max_overflow`。每个 anyio 线程经 `get_db` 至多持 1 连接，故 anyio 大小是 web 进程并发持连接数的**上界**；默认 `40 + 5 + 10 = 55 ≤ 60` 已通过——**无需钉小 anyio**。
3. **失败时杠杆是扩池 / 降 publish_max，绝不缩 anyio**（缩 anyio 会饿死 SSE/同步端点，评审第 13 条）。
4. **断言注释别复述**：scheme 侧「×4 瞬时借连接」已核实；**pipeline 节点 session 时长须先实测**再写入基准（[[verify-dont-parrot-docs]]，评审第 18 条）。
5. 越界 → 走 Task 3 告警 hook（不只孤立 WARNING）。

**Files:**
- Modify: `server/app/main.py:create_app()`（启动期断言 + 接告警 hook；**默认不改 anyio 池**）
- Modify: `server/app/core/config.py`（仅暴露"池下限/余量"配置，**不**暴露缩 anyio 的旋钮）
- Test: `server/tests/test_concurrency_budget_assertion.py`（纯逻辑）

- [~] **Step 1（部分）**：未做满负载实测；预算建模基于既有事实——anyio 默认 40（每线程经 get_db 至多持 1 连接）、池容量 20+40=60（session.py，#110 调大后）、publish_max=5；pipeline/scheme 后台 run 走自建 ThreadPoolExecutor+自建 session（不占 anyio）、各自闸封顶 3+2、scheme ×4 实测瞬时借还、**pipeline 节点 checkout 时长仍未满负载实测**——这些零散/瞬时借用归入 safety_margin（默认 10）吸收，断言定位为保守护栏而非精确建模（代码注释已如实标注，未 parrot「anyio=唯一上界」）。真要精确化需后续做满负载实测，留待需要时。
- [x] **Step 2（失败测试，已完成）**：`test_concurrency_budget_assertion.py` —— 越界（把 `_collect_pool` 压到极小容量）触发告警 hook（断言 emit 一次 + 文案含 budget）；另含纯函数 within/over/边界、容量不可用不误报、_collect_pool 抛错不崩。
- [x] **Step 3（已完成）**：纯函数 `compute_connection_budget`（算式明细 + within_budget）+ `check_connection_budget`（算式明细打 INFO 日志，越界文案建议扩池 GEO_DB_POOL_SIZE/MAX_OVERFLOW 或降 publish，明确「绝不缩 anyio」），均落在 `resource_metrics.py`（与 emit_resource_alert 同处；非 main.py，便于纯逻辑单测）。新增配置 `connection_budget_safety_margin`（GEO_CONNECTION_BUDGET_SAFETY_MARGIN，默认 10），**未暴露缩 anyio 的旋钮**。
- [x] **Step 4（已完成）**：越界走 `emit_resource_alert`（Task 3 同一告警通道），`create_app()` 启动期 try/except 调 `check_connection_budget()`（内部已吞异常，外层双保险）。
- [x] **Step 5（已完成）**：7 passed（纯逻辑，无 DB）；`test_resource_metrics_api.py`（经 build_test_app→create_app）2 passed 确认启动期检查不破坏建 app；ruff check / format / mypy（132 files）通过。

---

# Phase 2 — 幂等去重 + 跨进程封顶（#5 #6）

## Task 6: scheme run 活跃去重 + 有界线程（封堵 #5）

**根因**：pipeline 有"活跃 run 抛 ConflictError"，scheme 没有（[scheme_router.py:237](../../server/app/modules/ai_generation/scheme_router.py#L237)）；[284](../../server/app/modules/ai_generation/scheme_router.py#L284) 裸 `Thread` 无界 spawn。

**Files:**
- Modify: `scheme_service.py`/`scheme_router.py`（建 run 前查同 scheme 有无 pending/running，复用 [scheme_executor.py:245](../../server/app/modules/ai_generation/scheme_executor.py#L245) 判定，有则 409 ConflictError）
- Modify: `scheme_router.py:284`（裸 Thread → 有界线程池/队列）
- Test: `server/tests/test_scheme_run_idempotency.py`（新建）

- [x] **Step 1（失败测试，已完成）**：`test_scheme_run_idempotency.py` —— create_run 二次抛 ConflictError、端到端连点第二次 409+只留 1 条 run、`_DISPATCH_POOL` 有界、`submit_scheme_run` 经池执行；另含「历史 done run 不挡新 run」守卫。RED 4 失败 + 1 守卫通过。
- [x] **Step 2（已完成）:** 活跃去重落在 `scheme_executor.create_run`（镜像 pipeline）——`with_for_update()` 锁 scheme 行 + 查 `status ∈ ACTIVE_RUN_STATUSES{pending,running}`，有则抛 `ConflictError`（全局处理器 → 409）。新增模块常量 `ACTIVE_RUN_STATUSES`，`recover_stuck_scheme_runs` 同源复用。
- [x] **Step 3（已完成）:** 裸 `threading.Thread` → 有界 `_DISPATCH_POOL = ThreadPoolExecutor(max_workers=scheme_max_concurrent_runs*2, prefix="scheme-dispatch")` + `submit_scheme_run()`；router 改调它（去掉 `import threading`）。活跃去重挡同 scheme 重复、有界池挡跨 scheme 无限 spawn，双层。
- [x] **Step 4（已完成）:** 新测试 5 passed；回归 `test_scheme_runs`/`test_scheme_run_concurrency`/`test_generation_schemes`/`test_scheme_recovery`/`test_scheme_autoformat` + 本文件共 26 passed（正常路径未受去重影响）；ruff/format/mypy 通过。
  > 注：pipeline 的派发线程（[pipelines/router.py:401](../../server/app/modules/pipelines/router.py#L401)）同为裸 `threading.Thread`，但有 `_RUN_GATE`(cap 3) 封顶执行 + create_run 活跃去重挡同 pipeline 重复；本任务范围限 scheme（#5），未一并改 pipeline 派发。

## Task 7: 发布并发跨进程封顶 + 无 worker 告警（封堵 #6）

**根因**：`_global_publish_sem`（[tasks/executor.py:68](../../server/app/modules/tasks/executor.py#L68)）进程内；web 进程经 `bg_session_factory` 也能跑浏览器发布 → 双进程各 ×5。

**修法（推荐）**：web 进程 `POST /api/tasks/{id}/execute` 只置 pending/入队，由单实例 worker 抢占。**配套**：检测无活跃 worker（WorkerHeartbeat 陈旧）时入队即告警 + 端点回包提示，避免静默卡 pending。

**Files:**
- Modify: `server/app/modules/tasks/router.py`（execute 在 web 不起浏览器，仅置 pending；查 WorkerHeartbeat 新鲜度，陈旧则告警）
- Test: `server/tests/test_publish_web_no_browser.py`（新建）

- [x] **Step 1（已完成）:** 核实 `create_app()` 只给 scheme_router / pipelines_router 注入 `bg_session_factory`，**未注入 tasks.router**——即生产 web 进程本就不内联发布。唯一依赖 web 内联发布的调用方是测试（`utils.py:198`）。本任务把"生产不内联"从隐式（靠 bg_session_factory 恰好为 None）变显式（独立开关），防未来误注入。
- [x] **Step 2（失败测试，已完成）**：`test_publish_web_no_browser.py` —— 关内联开关后 execute 只入队（`execute_task` 调用计数=0）、记录留 pending；无新鲜 WorkerHeartbeat → 告警 hook 触发 + 回包 `worker_online=False`；有新鲜 worker → 不告警 + `True`；陈旧心跳(>30s)仍告警；显式开关开 → 仍内联执行。RED 4 失败（开关缺失）+ 1 守卫。
- [x] **Step 3（已完成）:** 新增显式开关 `inline_execute_enabled`（默认 False）+ `_inline_execute_active()`（= 开关 ∧ bg_session_factory），execute 与 `_start_background_execute` 同走它；生产路径只入队 + 释放陈旧认领，并查 `_has_fresh_worker`（复用 system_router 的 30s 心跳判定），无 worker 走 `emit_resource_alert` + 回包 `worker_online`（`_ExecuteResponse` 新增字段）。
- [x] **Step 4（已完成）:** 显式开关：`build_test_app` 置 `inline_execute_enabled=True`，存量发布测试照旧内联跑（5 处 `== {"queued": True}` 因新增 `worker_online` 字段放宽为 `["queued"] is True`）。验证：新测试 5 passed；clean DB 下 `test_tasks_state_machine` 13 / `test_publish_validation` 5 / `test_publish_web_no_browser` 5 全绿；ruff/format/mypy 通过。
  > 注：本会话本地反复跑后 MySQL 出现 schema 复用 + 后台线程并发 DDL 竞态（`Duplicate testadmin` / `concurrent DDL` / `ai_models doesn't exist`），与 Task 7 逻辑无关——全部失败均为该基础设施竞态、**零**断言失败，且 clean 分支同样偶发（见 [[run-tests-env]]）；CI（全新 mysql 服务、单进程）为准。

---

# Phase 3 — 发布外部资源回收与锁所有权（#2 #3，含常驻回归）

## Task 8: 超时确认线程终止再释放账号锁（封堵 #2，接 Task 4 后）

**根因**：[tasks/executor.py:285-317](../../server/app/modules/tasks/executor.py#L285-L317) 超时分支 `future.result(timeout=10)` 若仍 TimeoutExpired（线程卡 IO 未响应 context 关闭）被吞，而账号/profile 锁已释放（315）→ 下一条同账号记录对同一 persistent profile 再开 Chromium，目录并发写损坏。

**Files:**
- Modify: `server/app/modules/tasks/executor.py`（超时分支：线程未确认终止则不释放账号/profile 锁，标"僵尸待清"+ 告警，交下轮恢复）
- Test: `server/tests/test_publish_timeout_lock_safety.py`（mock 卡死 future，纯逻辑）

- [x] **Step 1（失败测试 = 常驻 CI 回归，评审第 17 条）**：`server/tests/test_publish_timeout_lock_safety.py` 用真实 RUNNING `Future`（`set_running_or_notify_cancel`）模拟卡死/已终止两态，断言线程存活时账号锁 + profile 锁 + 全局闸槽**均未**释放、记录标"僵尸待清"、走告警；线程终止时全部归还。纯逻辑无 DB，进 CI 作锁所有权不变式护栏。RED 已观测（`AttributeError: _handle_timed_out_record` 缺失）。
- [x] **Step 2:** 改超时分支。`_stop_record_session` 拆成 `_close_record_browser`（关会话+清映射、**不放 profile 锁**）+ `_release_record_profile_lock`，常规收尾＝两者合并（11 个旧调用点行为不变）。新增 `_handle_timed_out_record`（标 failed→关 context→等线程终止：终止才放 profile 锁 + 退场归还闸槽/账号锁；仍存活则一律不放 + `_mark_record_zombie` 回填 queue_reason + `emit_resource_alert`，交下轮恢复）。执行循环超时分支改为单调 helper。
- [x] **Step 3:** 跑测试绿（新 2 例 + 回归：state_machine 13、publish/worker 集群 26 全绿；ruff/format/mypy 通过；隐藏 .env 全量 768 用例 0 收集错误）。**容器内真实卡死实测＝不可重复路径，仅作补充、留待容器冒烟（与 Task 9 对齐），未在本地复现。**

## Task 9: display/port 回收对账（封堵 #3）+ 常驻回归

**根因**：[browser.py:812-836](../../server/app/modules/accounts/browser.py#L812-L836) SIGKILL 后进程仍不退只 `logger.error`，号段（base..base+1000）泄漏满后 `start_remote_browser_session` 抛错，全站发布瘫痪。

**Files:**
- Modify: `server/app/modules/accounts/browser.py`（被杀失败 PID + display/port 记入持久结构，后台对账重试 reap，成功归还号段；周期扫 `/tmp/.X11-unix/` 与端口对账）
- Test: `server/tests/test_browser_port_reaper.py`（**台账记账纯逻辑单测**，可本地跑）
- Add: 容器冒烟脚本（CI 内可跑的 Xvfb 泄漏场景）

- [x] **Step 1（失败测试，纯逻辑）**：`server/tests/test_browser_port_reaper.py` 用假进程（poll/terminate/kill/wait 可控）覆盖「SIGTERM 死 / SIGKILL 死 / 怎么都不死」三态，断言泄漏号段被 `_reserve_numbers` 视为占用、对账（注入 is_alive）确认死透才回收、仍存活则重试强杀留账。无 DB、无真子进程。RED 已观测（`AttributeError: _register_leaked_session` 缺失）。
- [x] **Step 2:** 实现回收对账。新增 `LeakedSession` 台账 `_leaked_sessions` + `_register_leaked_session`（杀不死即记号段 + `emit_resource_alert`）；`_stop_session_processes` 改为返回 survivors，两个调用点（启动失败 / 正常 stop）据此入账；`_reserve_numbers` 把台账号段并入「占用」绝不复用；`reconcile_leaked_sessions(is_alive=)` 重试强杀、全死则出账 + 关句柄 + 清 X11 socket 回收号段，接进 idle cleanup 30s 周期；`_reset_globals` 清台账。
- [ ] **Step 3（常驻回归）**：容器冒烟脚本进 CI（真实 Xvfb/x11vnc 泄漏路径）—— **待补**。纯逻辑台账单测已作 CI 护栏；容器冒烟需 Linux 镜像，留待容器侧补。
- [x] **Step 4:** 跑测试绿（新 5 例 + 回归：browser_sessions / login_session_cancel / recover_login_sessions 全绿；ruff/format/mypy 通过；隐藏 .env 全量 773 用例 0 收集错误）。

---

## 验收总结（修订版）

- **Wave 0 签收 = Task ACC 的改前/改后峰值数字**，证明**单进程内**借还纪律使峰值从触顶→≪60（一次性基线签收，非持续门禁）。#110 的多进程放大由 Task 6/7 覆盖；持续防回归靠 Task 1a 确定性单测 + Task G。
- Phase 0 全完成：连接持有纪律落地（含 web_fallback 两路）+ 卡死可自愈 + 可观测有留存 + 运行期长持护栏。
- Phase 1 完成即可安全调并发/池容量（含 anyio 池治理），不再线上才暴露。
- Phase 2/3 收口重复运行放大、跨进程超发、浏览器/端口泄漏；Phase 3 两项带常驻回归（纯逻辑单测 + 容器冒烟），防半年后腐化。
- 每波次/任务出 PR，**Task 1a 独立先行**；CI（后端 ruff/mypy/pytest）硬门禁须绿。
