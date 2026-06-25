# 浏览器发布 headless 化 + 去 VNC(Lever A)设计

- 日期：2026-06-25
- 状态：已与需求方逐段确认(核心接管取舍经 AskUserQuestion 锁定,设计整体「认可」)
- 范围：仅后端,仅**发布路径**。不碰登录路径、不碰 noVNC/Xvfb 机器本体、不碰 Lever B/C
- 性质：spike 已验证可行(2026-06-25 headless 头条全链路真发上线),本文是落地设计;实现计划另起 `writing-plans`
- 来源：发布链路全量代码调研(`tasks/runner.py` / `tasks/executor.py` / `accounts/browser.py` / `accounts/service.py` / `accounts/auth.py` / `tasks/service.py`)

## 1. 需求与可行性结论

**需求**：浏览器发布(头条等走 Playwright 的平台)单次资源占用太大、并发卡瓶颈。当前每次发布都要起 Xvfb → x11vnc → websockify 整条 VNC 链 + headed Chromium,顺利路径下这套人工接管设施全程空转。要求把吞吐提上去、把单次成本降下来。

**可行性结论：可行,且已被 spike 实证。** 2026-06-25 在本机 Docker 全栈跑通了 headless 头条发布(预览 + 真发上线,无验证码/反爬触发,用户截图确认)。spike 唯一没省下的就是 VNC 链——当时它照常起着、只是无头 Chromium 不挂它。本设计把这条链从发布路径彻底摘掉。

**关键简化(来自接管语义调研)**:人工接管(`waiting_user_input`)的真实语义**不是**"在实时浏览器里把这篇手动发完",而是 `resolve_user_input_record`([service.py:257](../../../server/app/modules/tasks/service.py))——人解决账号态阻塞后,系统**杀掉会话、记录打回 pending 从头重发**。也就是说接管会话只是"给人一个可见浏览器去修复持久化 profile"。需求方据此进一步拍板:**发布过程根本不需要实时接管**——账号态失效就标记失败、跳过、继续别的任务,事后通过既有的(独立、headed)账号登录流程重新登录,再重试。

## 2. 已锁定的边界(需求方确认)

| 维度 | 决定 | 理由 |
|---|---|---|
| 发布路径渲染模式 | **默认 headless,完全不起 VNC 链** | spike 已证可行;VNC 链只为接管而存在,而接管被移出发布路径 |
| 撞到 `UserInputRequired` | **标记 `failed` + 跳过 + 继续其余记录**(headless 模式) | 本质是账号态失效;重登录是独立 headed 流程,无需 in-publish 接管 |
| 失败传播 | **一个账号失败不阻塞其它发布;失败持续标记** | 不暂停整任务,执行循环照常推进其余记录 |
| 回退保险 | **保留 headed 模式 = 现状逐字节一致**(含 noVNC 实时接管) | 出问题可一键切回;`GEO_PUBLISH_BROWSER_HEADLESS=false` |
| 账号失效联动 | **`login_required` 时把 `account.status` 置 `expired`** | 账号列表标红待重登录;该账号其余 pending 记录走校验秒失败 |

**非目标(明确排除)**：
- Lever B(放开全局并发上限 `MAX_CONCURRENT_RECORDS=5`)、Lever C(把 `_account_login_loop` 从发布 worker 拆出以横向扩多 worker)——各自独立 spec。
- 删除 noVNC/Xvfb/x11vnc/websockify 机器本体——**登录路径仍在用**(headed + 远程扫码),原样保留。
- 前端改造——`failed` 记录本就带 `error_message` 渲染,无需新 UI;headless 下不再产生 `waiting_user_input` 记录,故"noVNC 接管"按钮自然不出现。
- in-publish 实时接管 / headed 回退重跑 / 账号级 headed 开关——均在头脑风暴中被需求方否决,采用最简的"标记失败 + 事后重登录"。

## 3. 调研事实基线(现状)

- **发布编排**：`executor.execute_task` → `_run_pending_records`([executor.py:246](../../../server/app/modules/tasks/executor.py)) 主循环 → `_start_runnable_records` 逐级拿锁 claim → 线程池 `_publish_record`([:764](../../../server/app/modules/tasks/executor.py)) → `build_publish_runner_for_record`([:1121](../../../server/app/modules/tasks/executor.py)) 按驱动 `mode` 分叉(API 型走 `runner_api`,浏览器型走 `runner.run_publish`)。
- **浏览器运行器**：`run_publish`([runner.py:287](../../../server/app/modules/tasks/runner.py))。当前**无条件**先 `get_or_create_account_session` → `start_remote_browser_session`(起 Xvfb/x11vnc/websockify 全链),再 `pw.chromium.launch_persistent_context(**options)`,其中 `options = launch_options(channel, executable_path, headless=headless)` 且 `options["env"] = {**os.environ, "DISPLAY": session.display}`([:355-356](../../../server/app/modules/tasks/runner.py))。`headless` 参数已通线(spike 引入),但**会话仍带全套 VNC 进程**。
- **会话生命周期**：`start_remote_browser_session`([browser.py:625](../../../server/app/modules/accounts/browser.py)) 预留 display/vnc/novnc 号段 → `_spawn` 三个进程 → 等就绪 → 注册进 `_active_sessions` + 镜像 DB `browser_sessions` 行 + 启空闲清理线程。`stop_remote_browser_session`([:748](../../../server/app/modules/accounts/browser.py)) 关 Chromium 句柄 + 杀进程链 + 删 DB 行。超时/僵尸/泄漏清理、record→session 映射、profile 锁全部挂在这个会话抽象上。
- **`UserInputRequired` 现有处理**：`run_publish` 的 `except UserInputRequired`([:398-403](../../../server/app/modules/tasks/runner.py)) `keep_session_alive` + 回填 `session_id`/`novnc_url` 再 raise → `_finish_record_future` 的 `except UserInputRequired`([executor.py:815](../../../server/app/modules/tasks/executor.py)) 置 `waiting_user_input` + 关联会话。`waiting_user_input` 在主循环里**会暂停整个任务**(`_paused_for_user` → 无在跑 future 即 return,[executor.py:288-298](../../../server/app/modules/tasks/executor.py))。
- **账号状态值**：`valid` / `expired` / `unknown`。`detect_logged_in` 为假即置 `expired`([auth.py:861](../../../server/app/modules/accounts/auth.py)、[:1182](../../../server/app/modules/accounts/auth.py))。`_validate_record_inputs`([executor.py:670](../../../server/app/modules/tasks/executor.py)) 见 `status != "valid"` 返回"请重新验证账号"。
- **登录路径(保留不动)**：`auth.py:1124` `start_remote_browser_session` + `auth.py:1130` `launch_options(channel, executable_path)`(默认 `headless=False`)= headed + VNC,人远程扫码即走这条,与发布会话完全独立。
- **配置**：`publish_browser_headless: bool = False`([config.py](../../../server/app/core/config.py),spike 引入);`build_publish_runner_for_record` 读 setting 透传给 `run_publish`([executor.py:1172-1186](../../../server/app/modules/tasks/executor.py))。

**现状的洞**：发布路径的 VNC 链是无条件起的,headless 也照起;`UserInputRequired` 一律走"暂停任务等接管"。两者都假定"发布过程要可视接管",而这个假定已被需求方推翻。

## 4. 目标设计

### 4.1 两种模式(按 `GEO_PUBLISH_BROWSER_HEADLESS` 分叉)

| | **headless(新默认)** | **headed(回退保险)** |
|---|---|---|
| Chromium | 无头,不挂 X,不注入 `DISPLAY` | 现状:headed,挂 Xvfb display |
| VNC 链 | **完全不起**(无 Xvfb/x11vnc/websockify) | 现状:全起 |
| 会话对象 | **displayless 会话**(无进程、无 novnc_url) | 现状:完整会话 |
| 撞 `UserInputRequired` | 标 `failed`(`login_required`)+ 置账号 `expired` + 任务继续其余记录 | 现状:`waiting_user_input` + 保活会话 + noVNC 实时接管 |

回退模式与今天**逐字节一致**,作安全网;新默认是 headless。两种模式在 `run_publish` 与 `_finish_record_future` 两处按 `headless` 分叉,headed 分支不改一行行为。

### 4.2 组件改动(共 4 处)

**改动 1 — `start_remote_browser_session` 增 `with_display: bool = True`**(browser.py)

为 `False` 时:跳过 `_reserve_numbers()` 与三个 `_spawn`(Xvfb/x11vnc/websockify)及其就绪等待;产出一个 **displayless 会话**——`display=""`、`vnc_port=0`/`novnc_port=0`(或 sentinel)、`novnc_url=""`、`processes=[]`。其余照旧:注册进 `_active_sessions`、镜像 DB `browser_sessions` 行、启空闲清理线程。

**会话对象必须保留**(不是裸起 Chromium):超时处理 `_handle_timed_out_record` → `_close_record_browser` → `get_session_for_record` → `stop_remote_browser_session` 依赖 record→session 映射来关闭卡死的 Chromium;profile 锁、僵尸/泄漏清理、stop_requested 跨进程指令也都挂在会话上。displayless 会话让这些机制**原样复用**,只是 `_stop_session_processes` 面对空 `processes` 列表 no-op。

`get_or_create_account_session` 增 `with_display` 透传(或等价开关),由 `run_publish` 按 `headless` 传 `with_display=not headless`。

**改动 2 — `run_publish` 按 headless 起会话 + 起 Chromium**(runner.py)

- 起会话:`get_or_create_account_session(..., with_display=not headless)`。
- 起 Chromium:headless 时**不注入 `DISPLAY`**(`options` 不设 `env["DISPLAY"]`,或 env 不含 DISPLAY);headed 时维持 `options["env"] = {**os.environ, "DISPLAY": session.display}`。
- `launch_options(channel, executable_path, headless=headless)` 已就位(spike),无需再改。
- `except UserInputRequired` 分支也按 `headless` 分叉:
  - **headed**:维持现状——`keep_session_alive` + 回填 `session_id`/`novnc_url` 再 raise(留活会话供 noVNC 接管)。
  - **headless**:**不** `keep_session_alive`(无接管),直接 raise;`finally` 照常拆掉 displayless 会话(只关 Chromium 句柄,无 VNC 进程,成本极低)。会话由此在抛回 executor 前即销毁,`_finish_record_future` 只需标记记录、无会话可留。

**改动 3 — `_finish_record_future` 的 `UserInputRequired` 分支按 headless 分叉**(executor.py)

读 `get_settings().publish_browser_headless`(与 `build_publish_runner_for_record` 同一真理源)判模式:

- **headless**：标记记录 `failed`,`failure_kind="login_required"`,message 形如"账号登录态失效,请重新登录该账号后重试"(若 `exc.screenshot` 存在则随 `_store_failure_screenshot` 存证,与其它 failed 分支一致)。**不进 `waiting_user_input`** → 主循环不触发 `_paused_for_user` → 任务继续推进该账号外的其余 pending 记录。会话已在 `run_publish` 的 `finally` 销毁(见改动 2),此处 `_stop_record_session` 幂等收尾(找不到会话即 no-op)。
- **headed**：维持现状(`waiting_user_input` + 关联会话 + 截图),逐字节不变。

`failure_kind` 取**新值 `"login_required"`**(与既有 `"commit_uncertain"` 并列;该列为自由字符串、无 CHECK 约束,新增值**不需要迁移**),供前端/运维识别"这类失败 = 去重登录账号";`retry_record` 既有逻辑对 `failed` 记录可直接重试(重登录账号 → status 回 `valid` → 重试记录通过校验)。

**改动 4 — `publish_browser_headless` 默认翻 `True`**(config.py)

headless 成为新默认。flag 保留作回退(`false` → headed + VNC + 实时接管,现状)。

### 4.3 账号失效联动(D2,已确认做)

headless 分支处理 `UserInputRequired` 且 `error_type == "login_required"`(非 `captcha_required`)时,在主线程 `_finish_record_future`(持有 db)内把该账号 `account.status = "expired"`。效果:

- 账号列表 UI 直接标红"待重新登录"(复用既有 `expired` 渲染)。
- 该账号其余 pending 记录在 `_start_runnable_records` → `_validate_record_inputs` 处 `status != "valid"` **秒失败**("请重新验证账号"),天然实现"一个账号失败、其余账号照常发"。

仅对 `login_required` 置位(真登出);`captcha_required` 等可能是瞬时挑战,不连坐账号状态。在主线程写,不碰发布线程的 db 隔离。

## 5. 资源账与吞吐含义

- **单次发布进程数**：今天 = Xvfb + x11vnc + websockify + headed Chromium(4 进程 + 真实帧缓冲);之后 = 无头 Chromium 一个进程、零 X、零 VNC。
- **吞吐**：本 spec 不动 `MAX_CONCURRENT_RECORDS=5`(那是 Lever B),但每个并发槽位的内存/CPU 成本大幅下降,使"放开 5"在 Lever B 里变得安全可行。Lever A 是 B 的前置。

## 6. 待验证的唯一未知

无头 Chromium 在容器里**完全不起 Xvfb、不设 `DISPLAY`** 时能否正常发布(spike 当时 VNC 链仍在,没测"零 X"路径)。信心很高——Playwright 默认就是无头无 X 运行——但实现时**必须在 spike 栈里实测一次**(停掉 Xvfb、确认 headless 发布全链路绿)再宣告完成。这是本设计落地前唯一的实证缺口。

**✅ 已实测(2026-06-25,Lever A 代码):** 在 `geo-spike` 栈用新代码重建 app+worker(`GEO_PUBLISH_BROWSER_HEADLESS=true`),建一条 `stop_before_publish=True` 的头条发布(record 3 / article 2)。结果:记录干净到达 `waiting_manual_publish`(headless 一路跑到头条预览页,`error_message=(none)`),worker 容器内 `ps` 断言 **`NO_VNC_PROCESSES`——Xvfb/x11vnc/websockify 全部缺席**,仅 8 个 `chrome-headless` 进程。零-Xvfb headless 发布坐实,缺口封闭。

## 7. 测试策略

- **displayless 会话**:`start_remote_browser_session(with_display=False)` 不调 `_reserve_numbers`/`_spawn`、`processes==[]`、`novnc_url==""`;`stop_remote_browser_session` 对空进程链 no-op 且正常关 Chromium 句柄 + 删 DB 行。
- **`run_publish` headless 路径**:不起 VNC 链(断言未走 Xvfb 分支 / `with_display=False`)、Chromium options 不含 `DISPLAY`。
- **headless `UserInputRequired` → 失败不暂停**:记录置 `failed`(非 `waiting_user_input`)、`account.status` 置 `expired`、任务继续跑其余记录、聚合状态正确。
- **headed 回退路径回归**:`UserInputRequired` 仍 `waiting_user_input` + 保活会话(现状行为不破)。
- **既有发布测试全绿**:`test_publish_runner.py` 等(spike 已把 mock 改 `**kwargs` 兼容 headless 形参)。
- **容器实测(手动门禁)**:spike 栈零 Xvfb 跑通一次真发(覆盖第 6 节未知)。

## 8. 回滚

`GEO_PUBLISH_BROWSER_HEADLESS=false` 即回到现状(headed + VNC + 实时接管),无需回滚代码、无需迁移(本设计**不含 DB 迁移**——`failed`/`expired` 都是既有状态值,`failure_kind` 是既有列)。
