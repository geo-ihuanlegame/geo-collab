# 账号登录态夜间保活设计（Keep-Alive：复用检测按键 + 有界随机错峰）

- 日期：2026-06-25
- 状态：已与需求方逐段确认（节奏模型 / 账号范围 / 进程位置 / 间隔上下界 / 告警渠道 经 AskUserQuestion 锁定）
- 范围：仅后端。新增账号模块子组件 + worker 集成 + 配置项；前端不动、无 UI、无迁移
- 性质：调研可行性 + 抽象通用方案。本文是交付物本身；实现计划另起 `writing-plans`
- 来源：账号模块全量代码调研（`accounts/auth.py` / `browser.py` / `models.py` / `schemas.py`）、三个现有调度器（`ai_generation/sync_scheduler.py` / `pipelines/scheduler.py` / `tasks/taptap_health.py`）、worker（`worker/executor.py`）

## 1. 需求与可行性结论

**需求**：在平台账号模块加一个定时任务，夜间在一个时间窗口内、以**有界随机间隔**把账号登录态**批量逐个刷新**一遍，目的是**维系登录态**（防 cookie 过期）。复用现有"检测按键"。

**可行性结论：可行，且代价很小。** "检测按键"对应的 `check_account(use_browser=True)` 已经做了保活真正需要的三件事：无头载入登录态 → 访问平台首页 → **回写刷新后的 storage_state**。保活只需在 worker 里起一个后台线程，按窗口 + 有界随机节奏，逐个账号复用这条路径即可。已有 `taptap_health.py` 是几乎同构的先例（后台线程定时体检账号），但它是 TapTap 专用、纯 HTTP 探测（不刷 cookie）、固定间隔——本方案把它泛化为**走浏览器检测、覆盖全部浏览器账号、窗口内有界随机错峰**。

**保活生效原理**：`_check_account_in_browser`（`auth.py:1198`）在 `finally` 之前执行 `write_state(abs_state_path, dict(context.storage_state()))`（`auth.py:1240`）——以已登录态访问首页后，平台下发的新 cookie 被回写落盘，从而延长会话寿命。这依赖"平台在登录访问时会刷新会话 cookie"这一前提（头条等主流平台成立）。

## 2. 已锁定的边界（需求方确认）

| 维度 | 决定 | 理由 |
|---|---|---|
| 节奏模型 | **方案 A：自适应有界随机间隔** | 唯一同时满足「随机 + 有上界 + 当晚刷完、不拖到白天」三条；节奏计算可抽纯函数单测 |
| 随机语义 | **夜间窗口内逐个错峰**，间隔 **30s ~ 10min** | 拟人 + 错峰；上界 10min 防"随机太大拖到第二天白天还没轮到" |
| 计时基准 | **从前一个检测完成后开始计时** | gap 从"上一个完成"算起，不含检测耗时；每轮用最新 `now` 重算自适应上界 |
| 账号范围 | **仅当前 `status='valid'` 的浏览器账号** | 只保活健康账号；失效的不反复打、留人工重登 |
| 进程位置 | **worker 进程** | 单实例、本就是浏览器自动化的家、与请求服务隔离、独立 DB 连接池（避开 web 连接池耗尽史） |
| 失效处理 | **标记 expired + 飞书告警**，不自动重登 | 重登需人工扫码/短信，做不到自愈；沿用 `taptap_health` 的"只告警喊人重登" |
| 暴露方式 | **纯环境变量开关，无 UI** | 与 taptap/pipeline/sync 三调度器一致；MVP / YAGNI |

**非目标**：自动重登 / 解验证码 / 扫码（不可能且不安全）；API 型账号（公众号等 `state_path IS NULL`）——无 storage_state，无法走浏览器检测，必然排除；前端 UI / 手动触发端点（现有检测按钮已能手动单刷）；数据库迁移（不加表/列，全程读现有字段）；多 worker 实例（发布 worker 本就单实例）。

## 3. 调研事实基线（现状，带证据）

- **检测按键链路**：`POST /api/accounts/{id}/check`（`router.py:453`）→ `check_account(db, account, payload)`（`auth.py:1146`）。`use_browser=True`（`schemas.py:115` 默认 True）时：抢 `owner_kind="account_check"` 的 profile 锁（`auth.py:1164`，与发布/登录互斥、抢不到直接 `ClientError`、不排队）→ `_run_in_plain_thread(_check_account_in_browser)`（`auth.py:1172`）→ 置 `status='valid'|'expired'` + `last_checked_at`（`auth.py:1182`）。
- **无头检测细节**：`_check_account_in_browser`（`auth.py:1198`）用 `with sync_playwright()` + `headless=True`（`auth.py:1211`），`goto(home_url, timeout=30000)`（`auth.py:1216`）+ networkidle 8s + body 3s，`detect_logged_in`（`auth.py:1227`）判定，`write_state(...)` 回写登录态（`auth.py:1240`），`finally: context.close(); browser.close()`（`auth.py:1242`）。**全程不起 Xvfb/x11vnc/websockify/noVNC**——那套只服务交互式远程会话（`browser.py`），无头检测不碰，故 `browser.py` 的泄漏台账/号段回收机制在本路径上根本不触发。
- **`_run_in_plain_thread`**（`auth.py:109`）：在新建非 daemon 线程里跑同步 Playwright，重置 asyncio running-loop 守卫，`worker.join()` **无超时**——本设计要补的唯一缺口。
- **profile 锁**：`try_acquire_profile_lock` / `release_profile_lock`（`browser.py:112` / `:211`），租约 `PROFILE_LOCK_LEASE_SECONDS=900`（`browser.py:39`），过期自动被新请求接管（`browser.py:135`）——故保活即便崩在持锁中，900s 后发布自动恢复。
- **Account 字段**（`models.py:31`）：`state_path`（浏览器账号非空、API 账号 NULL，`models.py:50`）、`status ∈ valid|expired|unknown`（`models.py:45`，有 CHECK 约束）、`last_checked_at`（`models.py:48`，naive UTC）、`is_deleted`（`models.py:69`）、`merged_into`（合并 tombstone，`models.py:73`）、`distribution_enabled`（`models.py:62`）。
- **现有调度器三件套**（同构形状）：纯函数 `run_*_once(session_factory)` + 后台线程 `wait(interval) → run_once` + `start_*`/`stop_*`，由宿主进程按 `GEO_*_ENABLED` 启动。`sync_scheduler.py`（问题池，web）、`pipelines/scheduler.py`（含 `_to_utc_naive` 本地→UTC、`schedule_calc.in_window` 跨午夜窗口）、`taptap_health.py`（账号 cookie 体检，**最接近的先例**：每账号独立 session + `try/except ... continue` 隔离、批量飞书告警）。
- **跨午夜窗口判定**：`schedule_calc.in_window(start, end, now)`（`pipelines/schedule_calc.py:34`）已处理 `start>end` 的跨午夜情形（23:00–03:00）。复用其逻辑（为避免 accounts→pipelines 跨模块依赖，在 keepalive 内内联一份小实现，注释指向此先例）。
- **worker 结构**（`worker/executor.py`）：`main()`（`:335`）`_startup` 后起 `_account_login_loop` daemon 线程（`:354`），主循环按 `_shutdown` 标志退出（`_handle_signal` 置位，`:75`）。`GEO_WORKER_ID` 在 `main()` 注册（`:337`）。

**现状的洞**：完全没有"保活"概念。登录态只在①手动点检测、②发布前隐式校验时被动检查；夜间无人值守时 cookie 静默过期，次日发布才发现失效。

## 4. 组件设计

### 4.1 新模块 `server/app/modules/accounts/keepalive.py`

镜像 `taptap_health.py` 形状。核心是一个**最多处理一个账号、可单测**的 tick 函数，加纯函数节奏算法，加后台线程壳。

**纯函数（无副作用、易测）：**

```
def in_keepalive_window(start: time, end: time, now: datetime) -> bool
    # 内联跨午夜窗口判定（同 pipelines/schedule_calc.in_window）

def window_start_instant(start: time, now_local: datetime) -> datetime
    # 本窗口起点：<= now 的最近一次 start 出现时刻（今天或昨天），返回 UTC-naive
    # 与 Account.last_checked_at(naive UTC) 同基准，用于"本窗口是否已刷"判定

def window_end_instant(end: time, now_local: datetime) -> datetime
    # 本窗口止点：> now 的最近一次 end 出现时刻（今天或明天），返回 UTC-naive
    # 用于算 remaining_window_s = window_end_instant - now（喂给 compute_next_gap）

def compute_next_gap(remaining_window_s: float, remaining_due: int,
                     min_gap: float, max_gap: float, rng: Random) -> float
    cap = remaining_window_s / max(1, remaining_due)
    hi  = min(max_gap, max(min_gap, cap))
    return rng.uniform(min_gap, hi)
    # 账号多→cap 小→上界压缩→当晚刷完；账号少→上界放到 max_gap
    # cap<min_gap 时退化为恒定 min_gap（窗口收尾连刷）

def select_due_account_ids(db, window_start: datetime) -> list[int]
    # 浏览器 valid 账号且本窗口未刷，最旧优先：
    #   state_path IS NOT NULL AND status='valid'
    #   AND is_deleted=0 AND merged_into IS NULL
    #   AND (last_checked_at IS NULL OR last_checked_at < window_start)
    #   ORDER BY last_checked_at ASC（NULL 最前）
```

**带副作用、隔离单账号：**

```
def refresh_one_account(session_factory, account_id, *, check_timeout_s) -> str
    # 独立 session；db.get(Account)；调 check_account(db, account, AccountCheckRequest())
    # 包"超时看门狗"（见 4.3）；记录 valid→expired 翻转→即时飞书告警
    # 任何异常（锁冲突 ClientError / 浏览器错 / 超时）→ 记日志、返回状态码、绝不抛
    # 返回 "refreshed_valid" | "flipped_expired" | "lock_busy" | "timeout" | "error"
```

**tick + 线程壳：**

```
def run_keepalive_once(session_factory, now_local, rng) -> dict
    # 1. 不在窗口 → {processed:False, in_window:False}
    # 2. due = select_due_account_ids(db, window_start_instant(...))
    #    无 due → {processed:False, in_window:True, remaining_due:0}
    # 3. refresh_one_account(due[0])   # 最旧的那个
    # 4. gap = compute_next_gap(window_end_instant - now, len(due)-1, ...)
    #    返回 {processed:True, next_gap_seconds:gap, result:..., remaining_due:len(due)-1}

def start_keepalive(session_factory) -> bool   # GEO_ACCOUNT_KEEPALIVE_ENABLED 关→False
def stop_keepalive() -> None                    # 置 stop event
```

**后台循环**（线程内）：
```
while not _stop.is_set():
    r = run_keepalive_once(session_factory, now_local(tz), rng)
    sleep = r["next_gap_seconds"] if r["processed"] else POLL_SECONDS
    if _stop.wait(sleep): break
```
gap 在 `refresh_one_account` 返回（=检测完成）**之后**计算并 sleep，故"从前一个完成后计时"天然成立；每轮 `now` 实时刷新，检测耗时自动从剩余窗口扣除。

### 4.2 worker 集成（`server/worker/executor.py`）

`main()` 在起 `_account_login_loop` 之后追加：
```python
from server.app.modules.accounts.keepalive import start_keepalive, stop_keepalive
start_keepalive(SessionLocal)
```
关停：在 `_handle_signal` / 主循环退出后调 `stop_keepalive()`（与 `login_broker` 关停同处，`executor.py:401` 附近）。线程为 daemon，循环在账号间隙（`_stop.wait`）promptly 退出。**保活仅 worker 跑**，web 进程不启动（与发布同侧，符合"浏览器自动化只在容器里"）。

### 4.3 超时看门狗（唯一新增硬化）

`_run_in_plain_thread` 的 `worker.join()` 无超时；极端下 Chromium 拆除卡死会冻住整个保活循环、拖累后续账号。`refresh_one_account` 把 `check_account` 调用丢进一个内部线程，`join(timeout=GEO_ACCOUNT_KEEPALIVE_CHECK_TIMEOUT_SECONDS)`：
- 超时 → 记日志 + 飞书告警（"账号 X 检测超时，跳过"）+ 返回 `"timeout"`，**循环继续下一个账号**。
- 被放弃的内部线程是 best-effort（daemon），不阻塞进程退出；正常情况各级 Playwright 超时（goto 30s 等）远早于看门狗触发，看门狗只兜真卡死。

## 5. 配置项（`server/app/core/config.py`，`GEO_` 前缀）

| 配置 | 默认 | 含义 |
|---|---|---|
| `ACCOUNT_KEEPALIVE_ENABLED` | `False` | 总开关（本地/测试默认关，不起浏览器） |
| `ACCOUNT_KEEPALIVE_WINDOW_START` | `"23:00"` | 窗口起（HH:MM，`GEO_SCHEDULER_TZ` 时区） |
| `ACCOUNT_KEEPALIVE_WINDOW_END` | `"03:00"` | 窗口止（跨午夜） |
| `ACCOUNT_KEEPALIVE_MIN_GAP_SECONDS` | `30` | 账号间最小随机间隔 |
| `ACCOUNT_KEEPALIVE_MAX_GAP_SECONDS` | `600` | 账号间最大随机间隔（10min） |
| `ACCOUNT_KEEPALIVE_POLL_SECONDS` | `120` | 不在窗口/无待刷时的轮询步长 |
| `ACCOUNT_KEEPALIVE_CHECK_TIMEOUT_SECONDS` | `120` | 单账号检测墙钟超时（看门狗） |

复用 `GEO_SCHEDULER_TZ`（默认 `Asia/Shanghai`）作为窗口时区。窗口字符串 `"HH:MM"` 在启动时解析为 `dt.time`；解析失败记日志并禁用保活（不致命）。

## 6. 失效检测、并发与隔离（不影响其他账号）

- **不进扫码/验证码**：`check_account` 是只读检测，从不驱动登录流程；登录态掉了只会 `detect_logged_in=False` → 置 `expired`，不可能卡在二维码上。
- **失效即出范围**：翻 `expired` 后不再满足 `status='valid'`，下次自动跳过；每个新失效账号即时发一条飞书告警（失效后即出范围，无重复告警风险）。
- **锁冲突安静跳过**：账号正被发布/登录占 profile 锁 → `check_account` 抛 `ClientError` → `refresh_one_account` 捕获返回 `"lock_busy"`，本窗口跳过、下一晚再试，不报错、不阻塞。
- **单账号隔离**：每账号独立 session + try/except（照搬 `taptap_health` 的 `:135` 模式），任何失败只影响自己，循环继续。
- **DB 连接池**：worker 独立池；串行一次一账号、每账号 session 用完即关；最多占 1 连接，无池压力。
- **关停**：daemon 线程 + `_stop` event，SIGTERM 后在账号间隙退出；在途检测让其自然结束（受看门狗上界约束）。

## 7. 测试（`server/tests/test_account_keepalive.py`，纯函数为主、不需真浏览器）

- `compute_next_gap`：seeded `Random` 验证 ∈ `[min, hi]`；账号多→`cap` 压上界；`cap<min_gap`→恒定 `min_gap`；`remaining_due=0` 不除零。
- `in_keepalive_window` / `window_start_instant` / `window_end_instant`：窗口内/外、跨午夜（now=01:30 属昨夜 23:00 窗口、止点为当日 03:00）、本地→UTC-naive 换算正确。
- `select_due_account_ids`（需 MySQL，`@pytest.mark.mysql`）：只选 valid+浏览器账号；排除 API（`state_path NULL`）/`is_deleted`/`merged_into`/非 valid；本窗口已刷（`last_checked_at >= window_start`）跳过；最旧优先排序。
- `run_keepalive_once`：monkeypatch `check_account`，验证 ① 不在窗口直接返回；② valid→expired 翻转触发飞书告警（patch `feishu.send_text`）；③ 锁冲突 `ClientError` 返回 `lock_busy` 不抛；④ 看门狗超时返回 `timeout` 且循环可继续；⑤ 单账号异常隔离不影响计数。
- `start_keepalive`：`ENABLED=False` 返回 False、不起线程。

## 8. 风险与权衡

- **保活有效性依赖平台行为**：若某平台访问首页不刷新 cookie，则保活退化为"仅检测"（仍有价值：提前发现失效并告警）。无法在设计层根治，属平台特性。
- **看门狗放弃的线程残留**：极端卡死时被放弃的内部线程作为 daemon 残留到进程退出；正常运行（容器长驻）下数量极少，可接受。若高频出现说明 Chromium 环境异常，应另查根因。
- **窗口太短 + 账号太多**：`compute_next_gap` 会把间隔压到 `min_gap=30s` 连刷；仍刷不完则"不强求"，最旧优先保证最危险账号优先、不饿死，剩余下一晚补。
- **手动检测与保活共用 `last_checked_at`**：窗口内手动点检测的账号会被保活自动跳过（视为已刷），是期望行为、不重复劳动。
