# 浏览器发布 headless 化 + 去 VNC(Lever A)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让浏览器发布默认以 headless 跑、彻底不起 Xvfb/x11vnc/websockify 那条 VNC 链;撞到账号登录态失效就标记失败、跳过、继续其余记录,事后经独立 headed 登录流程重登录账号再重试。

**Architecture:** 发布会话抽象增加一个 `with_display` 开关,headless 时产出"displayless 会话"(无 VNC 进程、无 novnc_url),复用现有会话生命周期/超时清理/profile 锁机制;`run_publish` 与 `_finish_record_future` 两处按 `GEO_PUBLISH_BROWSER_HEADLESS` 分叉,headed 分支逐字节保留作回退保险。无 DB 迁移。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / Playwright(sync API)/ pytest(部分用例 `@pytest.mark.mysql` 需 `GEO_TEST_DATABASE_URL`)。

## Global Constraints

- **设计来源**:`docs/superpowers/specs/2026-06-25-headless-publish-lever-a-design.md`,逐条对齐。
- **范围**:仅后端、仅发布路径。**不碰**登录路径(`accounts/auth.py`、`login_broker`)、不碰 noVNC/Xvfb 机器本体、不碰 Lever B(`MAX_CONCURRENT_RECORDS=5`)/ Lever C。
- **无 DB 迁移**:`failed`/`expired` 都是既有状态值;`failure_kind="login_required"` 是既有自由字符串列的新值,无 CHECK 约束。
- **回退保险**:`GEO_PUBLISH_BROWSER_HEADLESS=false` 必须回到现状逐字节一致(headed + VNC + `waiting_user_input` 实时接管)。headed 分支代码不许改行为。
- **运行后端测试**(见 [[run-tests-env]]):conda activate 在工具 shell 里不生效,用 env python 全路径 + `GEO_TEST_DATABASE_URL`(库名含 `test`)。无 DB 的纯单元用例裸跑 `pytest` 即可。
- **worktree cwd 漂移坑**(见 [[gotcha-worktree-bash-cwd-drifts-to-main]]):一律用绝对路径 / `git -C` / `python -m pytest` 指定 worktree,跑完 `pwd` 确认在 `E:\geo\.claude\worktrees\headless-spike`。
- **pydantic 配置吃 `.env`**:`config.py` 的 `model_config` 带 `env_file=".env"`,而 worktree 根的 spike `.env` 有 `GEO_PUBLISH_BROWSER_HEADLESS=true`。OS 环境变量(`monkeypatch.setenv`)优先级高于 `.env`,所以**凡涉及该 setting 的测试都必须显式 `monkeypatch.setenv` + `get_settings.cache_clear()`**,不能依赖默认/ambient。
- **提交**:每个 Task 末尾提交;commit message 结尾加 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。

## File Structure

- `server/app/modules/accounts/browser.py` — 改 `start_remote_browser_session` + `get_or_create_account_session` 增 `with_display`(displayless 会话分支)。
- `server/app/modules/tasks/runner.py` — 改 `run_publish`:headless 起 displayless 会话、Chromium env 去 DISPLAY、`except UserInputRequired` 按 headless 分叉。
- `server/app/modules/tasks/executor.py` — 改 `_finish_record_future` 的 `UserInputRequired` 分支按 headless 分叉 + 新增 `_expire_account_for_record`。
- `server/app/core/config.py` — `publish_browser_headless` 默认翻 `True`。
- `server/tests/test_browser_sessions.py` — 加 displayless 会话单测。
- `server/tests/test_publish_runner.py` — 加 headless `run_publish` 单测。
- `server/tests/test_headless_user_input.py`(新建)— headless `UserInputRequired` → failed + 账号 expired 集成测试。
- `server/tests/test_tasks_state_machine.py` + `server/tests/test_phase4.py` — 把现存 `waiting_user_input` 用例钉成 headed。

---

### Task 1: displayless 浏览器会话(`with_display` 开关)

**Files:**
- Modify: `server/app/modules/accounts/browser.py`(`start_remote_browser_session` ≈ `:625`、`get_or_create_account_session` ≈ `:526`)
- Test: `server/tests/test_browser_sessions.py`

**Interfaces:**
- Produces:
  - `start_remote_browser_session(account_key, platform_code="", profile_key=None, *, with_display: bool = True) -> RemoteBrowserSession`。`with_display=False` 时:不调 `_require_command`(xvfb/x11vnc/websockify)、不调 `_reserve_numbers`、不 `_spawn` 任何进程;返回的会话 `processes == []`、`display == ""`、`vnc_port == 0`、`novnc_port == 0`、`novnc_url == ""`,但仍注册进 `_active_sessions`、镜像 DB、启空闲清理线程。
  - `get_or_create_account_session(platform_code, account_key, profile_key=None, *, with_display: bool = True) -> RemoteBrowserSession`,透传 `with_display`。

- [ ] **Step 1: 写失败测试(displayless 会话不起进程)**

在 `server/tests/test_browser_sessions.py` 末尾追加:

```python
def test_start_remote_browser_session_displayless_skips_processes(monkeypatch, tmp_path: Path):
    """with_display=False：不起任何子进程，会话无 display/novnc，但可注册与停止。"""
    import types

    monkeypatch.setenv("GEO_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    def _boom_popen(*_a, **_k):
        raise AssertionError("Popen must not be called for a displayless session")

    monkeypatch.setattr(browser_session.subprocess, "Popen", _boom_popen)
    monkeypatch.setattr(browser_session, "_write_session_to_db", lambda *a, **k: None)
    monkeypatch.setattr(browser_session, "_delete_session_from_db", lambda *a, **k: None)
    monkeypatch.setattr(browser_session, "_start_idle_cleanup", lambda: None)
    browser_session._reset_globals()
    try:
        session = browser_session.start_remote_browser_session(
            "acct1", platform_code="toutiao", with_display=False
        )
        assert session.processes == []
        assert session.display == ""
        assert session.novnc_url == ""
        assert session.id in {s.id for s in browser_session.active_remote_browser_sessions()}

        browser_session.stop_remote_browser_session(session.id)
        assert browser_session.active_remote_browser_sessions() == []
    finally:
        browser_session._reset_globals()
        get_settings.cache_clear()


def test_get_or_create_account_session_passes_with_display(monkeypatch):
    """get_or_create_account_session 把 with_display 透传给 start_remote_browser_session。"""
    import types

    captured = {}

    def fake_start(account_key, platform_code="", profile_key=None, *, with_display=True):
        captured["with_display"] = with_display
        session = types.SimpleNamespace(id="s1", browser_context=None)
        browser_session._active_sessions["s1"] = session
        return session

    monkeypatch.setattr(browser_session, "start_remote_browser_session", fake_start)
    browser_session._reset_globals()
    try:
        browser_session.get_or_create_account_session("toutiao", "k1", with_display=False)
        assert captured["with_display"] is False
    finally:
        browser_session._reset_globals()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_browser_sessions.py -q -k "displayless or with_display"`
Expected: FAIL —— `start_remote_browser_session() got an unexpected keyword argument 'with_display'`(以及 `get_or_create_account_session` 同因)。

- [ ] **Step 3: 实现 `start_remote_browser_session` 的 displayless 早返回分支**

在 `server/app/modules/accounts/browser.py` 的 `start_remote_browser_session` 签名加 `with_display`,并在 `settings = get_settings()` 之后、`xvfb = _require_command(...)` 之前插入早返回分支。

签名改为:

```python
def start_remote_browser_session(
    account_key: str,
    platform_code: str = "",
    profile_key: str | None = None,
    *,
    with_display: bool = True,
) -> RemoteBrowserSession:
```

在 `settings = get_settings()` 这行之后插入:

```python
    if not with_display:
        # displayless 会话：headless 发布路径,不起 Xvfb/x11vnc/websockify,只持有 Chromium 句柄。
        # 仍注册 + 镜像 DB + 启清理线程,让超时/停止/profile 锁等机制原样复用(进程链为空,清理 no-op)。
        safe_account_key = re.sub(r"[^a-zA-Z0-9_-]+", "-", account_key).strip("-") or "account"
        session_id = uuid.uuid4().hex[:12]
        log_dir = get_data_dir() / "logs" / "browser-sessions" / f"{safe_account_key}-{session_id}"
        log_dir.mkdir(parents=True, exist_ok=True)
        session = RemoteBrowserSession(
            id=session_id,
            platform_code=platform_code,
            account_key=account_key,
            display_number=0,
            display="",
            vnc_port=0,
            novnc_port=0,
            novnc_url="",
            log_dir=log_dir,
            profile_key=profile_key,
        )
        with _sessions_lock:
            _active_sessions[session.id] = session
        _write_session_to_db(session, worker_id)
        _start_idle_cleanup()
        return session
```

(headed 路径以下全部保留不动。)

- [ ] **Step 4: 实现 `get_or_create_account_session` 透传 `with_display`**

在 `get_or_create_account_session` 签名加 `*, with_display: bool = True`,并把它创建会话那行改为透传:

```python
        session = start_remote_browser_session(
            account_key,
            platform_code=platform_code,
            profile_key=profile_key,
            with_display=with_display,
        )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest server/tests/test_browser_sessions.py -q`
Expected: PASS(原有 `test_remote_browser_session_starts_processes_and_cleans_up` + 两个新用例全绿)。

- [ ] **Step 6: 提交**

```bash
git -C "E:/geo/.claude/worktrees/headless-spike" add server/app/modules/accounts/browser.py server/tests/test_browser_sessions.py
git -C "E:/geo/.claude/worktrees/headless-spike" commit -m "feat(publish): browser 会话支持 with_display=False(displayless,无 VNC 链)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `run_publish` headless 接线(displayless 会话 + 去 DISPLAY + 接管分叉)

**Files:**
- Modify: `server/app/modules/tasks/runner.py`(`run_publish` ≈ `:287`;会话获取 `:328`/`:343`、Chromium env `:355-356`、`except UserInputRequired` `:398-403`)
- Test: `server/tests/test_publish_runner.py`

**Interfaces:**
- Consumes(Task 1):`get_or_create_account_session(..., with_display=...)`。
- Produces(`run_publish` 行为):
  - `headless=True` → 以 `with_display=False` 获取会话;Chromium `options["env"]` **不含 `DISPLAY`**。
  - `headless=True` 且驱动抛 `UserInputRequired` → **不** `keep_session_alive`,`finally` 调 `stop_remote_browser_session(session.id)` 拆会话,异常照常上抛。
  - `headless=False`(默认)→ 现状逐字节不变(`with_display=True`、env 含 `DISPLAY`、接管保活会话)。

- [ ] **Step 1: 写失败测试(headless:displayless + 无 DISPLAY)**

在 `server/tests/test_publish_runner.py` 末尾追加:

```python
def test_run_publish_headless_displayless_and_no_display(monkeypatch, tmp_path):
    """headless=True：会话以 with_display=False 获取，Chromium env 不含 DISPLAY。"""
    from server.app.modules.tasks import runner as publish_runner
    from server.app.modules.tasks.drivers.base import PublishResult

    state_rel = "browser_states/testplat/k1/storage_state.json"
    state_file = tmp_path / state_rel
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}")

    stub_payload = _make_stub_payload(tmp_path)
    stub_session = types.SimpleNamespace(id="sess1", display="", novnc_url="", browser_context=None)
    captured = {"with_display": None, "launch_kw": None}

    def fake_get_or_create(platform_code, account_key, profile_key=None, *, with_display=True):
        captured["with_display"] = with_display
        return stub_session

    page = types.SimpleNamespace(on=lambda *a, **k: None)
    context = types.SimpleNamespace(
        set_default_navigation_timeout=lambda ms: None,
        new_page=lambda: page,
        close=lambda: None,
    )

    def fake_launch(user_data_dir, **kw):
        captured["launch_kw"] = kw
        return context

    chromium = types.SimpleNamespace(launch_persistent_context=fake_launch)
    pw = types.SimpleNamespace(chromium=chromium, stop=lambda: None)
    pw_cm = types.SimpleNamespace(start=lambda: pw)

    monkeypatch.setattr(publish_runner, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(publish_runner, "account_key_from_state_path", lambda sp: ("testplat", "k1"))
    monkeypatch.setattr(publish_runner, "_build_payload", lambda *a, **k: stub_payload)
    monkeypatch.setattr(publish_runner, "profile_key_from_state_path", lambda sp: "browser_states/testplat/k1")
    monkeypatch.setattr(publish_runner, "profile_dir_from_state_path", lambda sp: tmp_path / "profile")
    monkeypatch.setattr(publish_runner, "get_or_create_account_session", fake_get_or_create)
    monkeypatch.setattr(publish_runner, "stop_remote_browser_session", lambda sid: None)
    monkeypatch.setattr(publish_runner, "clear_profile_locks", lambda d: None)
    monkeypatch.setattr(publish_runner, "sync_playwright", lambda: pw_cm)
    monkeypatch.setattr(publish_runner, "launch_options", lambda channel, executable_path, **kwargs: {})
    monkeypatch.setattr(publish_runner, "attach_browser_handles", lambda *a, **k: None)

    class _Driver:
        code = "testplat"; name = "x"; home_url = "h"; publish_url = "p"

        def detect_logged_in(self, *, url, title, body):
            return True

        def publish(self, *, page, context, payload, stop_before_publish, commit_guard=None, retry_policy=None):
            return PublishResult(url="u", title=payload.title, message="ok")

    monkeypatch.setattr(publish_runner, "resolve_driver", lambda pc: _Driver())

    publish_runner.run_publish(
        article=_make_stub_article(tmp_path), account=_make_stub_account(), headless=True
    )

    assert captured["with_display"] is False
    assert "DISPLAY" not in captured["launch_kw"].get("env", {})


def test_run_publish_headless_user_input_does_not_keep_session(monkeypatch, tmp_path):
    """headless 撞 UserInputRequired：不保活会话，finally 拆会话。"""
    from server.app.modules.tasks import runner as publish_runner

    state_rel = "browser_states/testplat/k1/storage_state.json"
    state_file = tmp_path / state_rel
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}")

    stub_payload = _make_stub_payload(tmp_path)
    stub_session = types.SimpleNamespace(id="sess1", display="", novnc_url="", browser_context=None)

    page = types.SimpleNamespace(on=lambda *a, **k: None)
    context = types.SimpleNamespace(
        set_default_navigation_timeout=lambda ms: None,
        new_page=lambda: page,
        close=lambda: None,
    )
    chromium = types.SimpleNamespace(launch_persistent_context=lambda user_data_dir, **kw: context)
    pw = types.SimpleNamespace(chromium=chromium, stop=lambda: None)
    pw_cm = types.SimpleNamespace(start=lambda: pw)

    monkeypatch.setattr(publish_runner, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(publish_runner, "account_key_from_state_path", lambda sp: ("testplat", "k1"))
    monkeypatch.setattr(publish_runner, "_build_payload", lambda *a, **k: stub_payload)
    monkeypatch.setattr(publish_runner, "profile_key_from_state_path", lambda sp: "browser_states/testplat/k1")
    monkeypatch.setattr(publish_runner, "profile_dir_from_state_path", lambda sp: tmp_path / "profile")
    monkeypatch.setattr(publish_runner, "get_or_create_account_session",
                        lambda platform_code, account_key, profile_key=None, *, with_display=True: stub_session)
    monkeypatch.setattr(publish_runner, "clear_profile_locks", lambda d: None)
    monkeypatch.setattr(publish_runner, "sync_playwright", lambda: pw_cm)
    monkeypatch.setattr(publish_runner, "launch_options", lambda channel, executable_path, **kwargs: {})
    monkeypatch.setattr(publish_runner, "attach_browser_handles", lambda *a, **k: None)

    kept, stopped = [], []
    monkeypatch.setattr(publish_runner, "keep_session_alive", lambda sid: kept.append(sid))
    monkeypatch.setattr(publish_runner, "stop_remote_browser_session", lambda sid: stopped.append(sid))

    class _Driver:
        code = "testplat"; name = "x"; home_url = "h"; publish_url = "p"

        def detect_logged_in(self, *, url, title, body):
            return True

        def publish(self, *, page, context, payload, stop_before_publish, commit_guard=None, retry_policy=None):
            raise ToutiaoUserInputRequired("needs login")

    monkeypatch.setattr(publish_runner, "resolve_driver", lambda pc: _Driver())

    with pytest.raises(ToutiaoUserInputRequired):
        publish_runner.run_publish(
            article=_make_stub_article(tmp_path), account=_make_stub_account(), headless=True
        )

    assert kept == []
    assert stopped == [stub_session.id]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_publish_runner.py -q -k headless`
Expected: FAIL —— 第一个用例 `captured["with_display"]` 仍为 `True`(当前 `run_publish` 写死 `with_display` 未传)或 env 仍含 `DISPLAY`;第二个用例 `kept == [stub_session.id]`(当前无条件保活)。

- [ ] **Step 3: 实现 `run_publish` 三处改动**

在 `server/app/modules/tasks/runner.py`:

(a) 两处 `get_or_create_account_session` 调用都加 `with_display=not headless`。第一处(`:328` 附近):

```python
        session = get_or_create_account_session(
            platform_code, account_key, profile_key=profile_key, with_display=not headless
        )
```

第二处(线程切换后重新获取,`:343` 附近)同样补 `with_display=not headless`:

```python
            session = get_or_create_account_session(
                platform_code, account_key, profile_key=profile_key, with_display=not headless
            )
```

(b) Chromium env 去 DISPLAY(`:355-356`),把:

```python
                options = launch_options(channel, executable_path, headless=headless)
                options["env"] = {**os.environ, "DISPLAY": session.display}
```

改为:

```python
                options = launch_options(channel, executable_path, headless=headless)
                env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
                if not headless:
                    env["DISPLAY"] = session.display
                options["env"] = env
```

(c) `except UserInputRequired`(`:398-403`)按 headless 分叉,把:

```python
    except UserInputRequired as exc:
        _keep_browser = True
        keep_session_alive(session.id)
        exc.session_id = session.id
        exc.novnc_url = session.novnc_url
        raise
```

改为:

```python
    except UserInputRequired as exc:
        if not headless:
            _keep_browser = True
            keep_session_alive(session.id)
            exc.session_id = session.id
            exc.novnc_url = session.novnc_url
        raise
```

- [ ] **Step 4: 跑测试确认通过(含 headed 回归)**

Run: `python -m pytest server/tests/test_publish_runner.py -q`
Expected: PASS —— 两个新 headless 用例 + 原有 headed 用例(`test_run_publish_routes_by_platform_code`、`test_run_publish_keeps_session_on_user_input_required`、`test_run_publish_stops_session_after_auto_publish`、`test_run_publish_keeps_session_for_manual_publish`)全绿。

- [ ] **Step 5: 提交**

```bash
git -C "E:/geo/.claude/worktrees/headless-spike" add server/app/modules/tasks/runner.py server/tests/test_publish_runner.py
git -C "E:/geo/.claude/worktrees/headless-spike" commit -m "feat(publish): run_publish headless 走 displayless 会话 + Chromium 去 DISPLAY + 接管分叉

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: executor headless 接管分叉(标 failed + 置账号 expired + 不暂停)

**Files:**
- Modify: `server/app/modules/tasks/executor.py`(`_finish_record_future` 的 `except UserInputRequired` ≈ `:815-843`;新增 helper `_expire_account_for_record`)
- Create: `server/tests/test_headless_user_input.py`
- Modify(钉 headed):`server/tests/test_tasks_state_machine.py`、`server/tests/test_phase4.py`

**Interfaces:**
- Consumes:`get_settings().publish_browser_headless`(与 `build_publish_runner_for_record` 同一真理源)、`Account`(已 import,`:51`)、`select`/`sa_update`(已 import)。
- Produces(`_finish_record_future` 行为):
  - headless 模式 + `UserInputRequired` → 记录 `failed`、`failure_kind="login_required"`;若 `error_type == "login_required"` 则该账号 `status="expired"`;**不** `waiting_user_input`(任务不暂停)。
  - headed 模式 → 现状(`waiting_user_input` + 保活会话)。
  - 新增 `_expire_account_for_record(db: Session, record_id: int) -> None`。

- [ ] **Step 1: 写失败测试(headless → failed + 账号 expired;captcha 不连坐;headed 仍 waiting)**

新建 `server/tests/test_headless_user_input.py`:

```python
"""headless 发布撞 UserInputRequired 的处置：标 failed + 置账号 expired，不进 waiting_user_input。"""

from concurrent.futures import Future

import pytest

from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _seed_record(db, *, status="running", username="op_headless"):
    from server.app.modules.accounts.models import Account
    from server.app.modules.articles.models import Article
    from server.app.modules.system.models import Platform, User
    from server.app.modules.tasks.models import PublishRecord, PublishTask

    user = User(username=username, role="operator", is_active=True, must_change_password=False)
    user.set_password("pw-123456")
    db.add(user)
    db.flush()
    platform = Platform(code="toutiao", name="头条号", base_url="https://mp.toutiao.com", enabled=True)
    db.add(platform)
    db.flush()
    account = Account(
        user_id=user.id,
        platform_id=platform.id,
        display_name="acc",
        platform_user_id=None,
        status="valid",
        state_path="browser_states/toutiao/acc/storage_state.json",
    )
    db.add(account)
    db.flush()
    article = Article(user_id=user.id, title="t", status="ready")
    db.add(article)
    db.flush()
    task = PublishTask(
        user_id=user.id, name="task", task_type="single",
        platform_id=platform.id, article_id=article.id,
    )
    db.add(task)
    db.flush()
    record = PublishRecord(
        task_id=task.id, article_id=article.id, platform_id=platform.id,
        account_id=account.id, status=status,
    )
    db.add(record)
    db.flush()
    return task, record, account.id


def test_headless_login_required_marks_failed_and_expires_account(monkeypatch):
    from server.app.core.config import get_settings
    from server.app.modules.accounts.models import Account
    from server.app.modules.tasks import executor as ex
    from server.app.modules.tasks.drivers.toutiao import ToutiaoUserInputRequired
    from server.app.modules.tasks.executor import _finish_record_future
    from server.app.modules.tasks.models import PublishRecord

    monkeypatch.setattr(ex, "_stop_record_session", lambda _rid: None)
    monkeypatch.setenv("GEO_PUBLISH_BROWSER_HEADLESS", "true")
    get_settings.cache_clear()

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, rec, account_id = _seed_record(db, status="running")
            db.commit()
            rid = rec.id

            fut: Future = Future()
            fut.set_exception(ToutiaoUserInputRequired("登录态失效", error_type="login_required"))
            _finish_record_future(db, task, rid, fut)
            db.commit()

            refreshed = db.get(PublishRecord, rid)
            assert refreshed.status == "failed"
            assert refreshed.failure_kind == "login_required"
            assert db.get(Account, account_id).status == "expired"
    finally:
        test_app.cleanup()
        get_settings.cache_clear()


def test_headless_captcha_marks_failed_but_keeps_account_status(monkeypatch):
    """captcha 可能是瞬时挑战：标 failed，但不连坐账号 status。"""
    from server.app.core.config import get_settings
    from server.app.modules.accounts.models import Account
    from server.app.modules.tasks import executor as ex
    from server.app.modules.tasks.drivers.toutiao import ToutiaoUserInputRequired
    from server.app.modules.tasks.executor import _finish_record_future
    from server.app.modules.tasks.models import PublishRecord

    monkeypatch.setattr(ex, "_stop_record_session", lambda _rid: None)
    monkeypatch.setenv("GEO_PUBLISH_BROWSER_HEADLESS", "true")
    get_settings.cache_clear()

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, rec, account_id = _seed_record(db, status="running")
            db.commit()
            rid = rec.id

            fut: Future = Future()
            fut.set_exception(ToutiaoUserInputRequired("需要验证码", error_type="captcha_required"))
            _finish_record_future(db, task, rid, fut)
            db.commit()

            assert db.get(PublishRecord, rid).status == "failed"
            assert db.get(Account, account_id).status == "valid"
    finally:
        test_app.cleanup()
        get_settings.cache_clear()


def test_headed_user_input_still_waits(monkeypatch):
    """回退保险：headed 模式下 UserInputRequired 仍进 waiting_user_input。"""
    from server.app.core.config import get_settings
    from server.app.modules.tasks import executor as ex
    from server.app.modules.tasks.drivers.toutiao import ToutiaoUserInputRequired
    from server.app.modules.tasks.executor import _finish_record_future
    from server.app.modules.tasks.models import PublishRecord

    monkeypatch.setattr(ex, "_stop_record_session", lambda _rid: None)
    monkeypatch.setenv("GEO_PUBLISH_BROWSER_HEADLESS", "false")
    get_settings.cache_clear()

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, rec, _account_id = _seed_record(db, status="running")
            db.commit()
            rid = rec.id

            fut: Future = Future()
            fut.set_exception(ToutiaoUserInputRequired("需要扫码", error_type="qr_scan_required"))
            _finish_record_future(db, task, rid, fut)
            db.commit()

            assert db.get(PublishRecord, rid).status == "waiting_user_input"
    finally:
        test_app.cleanup()
        get_settings.cache_clear()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_headless_user_input.py -q`(需 `GEO_TEST_DATABASE_URL`)
Expected: FAIL —— headless 两个用例期望 `failed`,但当前代码无 headless 分叉、一律落 `waiting_user_input`(且无 `_expire_account_for_record`)。`test_headed_user_input_still_waits` 应已 PASS(现状即 waiting)。

- [ ] **Step 3: 实现 `_finish_record_future` 的 headless 分叉 + `_expire_account_for_record`**

在 `server/app/modules/tasks/executor.py` 把 `except UserInputRequired as exc:` 整块(`:815-843`)替换为:

```python
    except UserInputRequired as exc:
        try:
            _add_publish_diagnostics(
                db, task.id, record_id, _diagnostics_from_exception(exc), task.user_id
            )
            screenshot_asset_id = _store_failure_screenshot(
                db, task.id, record_id, exc.screenshot, task.user_id
            )
            error_type = getattr(exc, "error_type", "login_required")
            if get_settings().publish_browser_headless:
                # headless 无实时接管：标失败 + 置账号失效 + 不暂停任务(执行循环继续其余记录)。
                _mark_record_failed(
                    db,
                    task.id,
                    record_id,
                    f"[账号登录态失效，请重新登录账号后重试] {exc}\n{traceback.format_exc()}",
                    screenshot_asset_id=screenshot_asset_id,
                    failure_kind="login_required",
                )
                if error_type == "login_required":
                    _expire_account_for_record(db, record_id)
                _stop_record_session(record_id)
                _logger.info(
                    "Record %d failed (headless, login state invalid; type=%s)", record_id, error_type
                )
            else:
                type_label = {
                    "login_required": "需要登录",
                    "captcha_required": "需要验证码",
                    "qr_scan_required": "需要扫码",
                }.get(error_type, "需要人工操作")
                _mark_record_waiting_user_input(
                    db,
                    task.id,
                    record_id,
                    f"[{type_label}] {exc}\n{traceback.format_exc()}",
                    screenshot_asset_id=screenshot_asset_id,
                )
                if exc.session_id:
                    associate_record_with_session(record_id, exc.session_id)
                _logger.info("Record %d waiting user input (type=%s)", record_id, error_type)
        except Exception as _inner:
            _logger.error(
                "Record %d: error handling UserInputRequired: %s", record_id, _inner, exc_info=True
            )
            _mark_record_failed(db, task.id, record_id, f"Error handling user input: {_inner}")
```

并在 `_mark_record_waiting_user_input` 定义之后(`:1072` 附近)新增 helper:

```python
def _expire_account_for_record(db: Session, record_id: int) -> None:
    """headless 发布撞登录失效:把该记录对应账号置 expired。

    触发账号列表标红待重登录,且该账号其余 pending 记录在 _validate_record_inputs 处
    (status != "valid")快速失败,天然实现"一个账号失效、其余账号照常发"。
    """
    account_id = db.execute(
        select(PublishRecord.account_id).where(PublishRecord.id == record_id)
    ).scalar_one_or_none()
    if account_id is None:
        return
    db.execute(sa_update(Account).where(Account.id == account_id).values(status="expired"))
```

- [ ] **Step 4: 跑新测试确认通过**

Run: `python -m pytest server/tests/test_headless_user_input.py -q`
Expected: PASS(三个用例全绿)。

- [ ] **Step 5: 钉现存 headed 用例(防被默认翻转/spike .env 带挂)**

这些用例验证 **headed 接管语义**,必须显式跑在 headed 下。

在 `server/tests/test_phase4.py` 的 `test_error_type_propagates_in_finish_record_future`(`:98`)里,`test_app = build_test_app(monkeypatch)` 之前插入两行:

```python
        from server.app.core.config import get_settings

        monkeypatch.setenv("GEO_PUBLISH_BROWSER_HEADLESS", "false")
        get_settings.cache_clear()
```

在 `server/tests/test_tasks_state_machine.py` 里,两个走 executor 跑出 `waiting_user_input` 的用例 —— `test_user_input_required_pauses_record`(`:144`)与 `test_resolve_user_input_requeues_and_continues`(`:202`)—— 各自在函数体首行 `test_app = build_test_app(monkeypatch)` 之前插入同样三行:

```python
    from server.app.core.config import get_settings

    monkeypatch.setenv("GEO_PUBLISH_BROWSER_HEADLESS", "false")
    get_settings.cache_clear()
```

- [ ] **Step 6: 跑被钉的现存用例确认仍绿**

Run: `python -m pytest server/tests/test_phase4.py server/tests/test_tasks_state_machine.py -q`
Expected: PASS(显式 headed → 仍走 `waiting_user_input`,行为不变)。

- [ ] **Step 7: 提交**

```bash
git -C "E:/geo/.claude/worktrees/headless-spike" add server/app/modules/tasks/executor.py server/tests/test_headless_user_input.py server/tests/test_phase4.py server/tests/test_tasks_state_machine.py
git -C "E:/geo/.claude/worktrees/headless-spike" commit -m "feat(publish): headless 撞登录失效标 failed + 置账号 expired + 不暂停任务

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 翻默认 `publish_browser_headless=True` + 全量回归

**Files:**
- Modify: `server/app/core/config.py:80`
- Test: 全量后端测试套件

**Interfaces:**
- Produces:`publish_browser_headless` 默认 `True`(headless 成为新默认,headed 仅在显式 `GEO_PUBLISH_BROWSER_HEADLESS=false` 时启用)。

说明:不为"默认值=True"单写断言用例——`config.py` 吃 `.env`、worktree 根 spike `.env` 已设该值,默认值单测对 `.env` 脆弱且低价值。真正证明翻转的是全量套件绿 + Task 5 容器实测。

- [ ] **Step 1: 翻默认值**

`server/app/core/config.py:80`,把:

```python
    publish_browser_headless: bool = False  # GEO_PUBLISH_BROWSER_HEADLESS
```

改为:

```python
    publish_browser_headless: bool = True  # GEO_PUBLISH_BROWSER_HEADLESS
```

并同步更新该行上方注释里"默认 False=现状(headed…)"的措辞为"默认 True=headless(发布路径不起 VNC 链);设 false 回退 headed+VNC+实时接管"。

- [ ] **Step 2: 跑全量后端套件,抓"假设 headed 默认"的漏网用例**

Run: `python -m pytest server/tests -q`(需 `GEO_TEST_DATABASE_URL`)
Expected: PASS。**若有用例因默认翻 headless 而挂**(典型:走 executor 跑出 `waiting_user_input` 却没显式钉 headed,或断言发布会话起了 Xvfb),按 Task 3 Step 5 同法给该用例补 `monkeypatch.setenv("GEO_PUBLISH_BROWSER_HEADLESS", "false") + get_settings.cache_clear()`(它测的是 headed 语义),或据其语义改为 headless 期望。逐个修到全绿。

- [ ] **Step 3: lint / format / typecheck 门禁**

Run:
```bash
python -m ruff check server/ && python -m ruff format --check server/ && python -m mypy server/app
```
Expected: 全过(CI 硬门禁,见 [[project_ci_and_hardening]])。

- [ ] **Step 4: 提交**

```bash
git -C "E:/geo/.claude/worktrees/headless-spike" add server/app/core/config.py server/tests
git -C "E:/geo/.claude/worktrees/headless-spike" commit -m "feat(publish): GEO_PUBLISH_BROWSER_HEADLESS 默认翻 True(headless 成新默认)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 容器实测门禁(spike 栈零 Xvfb 真发)

**Files:** 无代码改动。验证 spec 第 6 节的唯一实证缺口:无头 Chromium 在容器里**完全不起 Xvfb、不设 DISPLAY** 时能否正常发布。

**前提**:`geo-spike` Docker 栈在跑、账号 1(头条)登录态仍有效(早前 spike 已登录)。若已拆栈或登录过期,需先重起栈并重新登录账号 1(走既有账号登录流程,headed+noVNC)。

- [ ] **Step 1: 用 Lever A 新代码重建并起 spike 栈**

Run:
```bash
docker compose --project-directory "E:/geo/.claude/worktrees/headless-spike" \
  -f docker-compose.yml -f docker-compose.spike.yml -p geo-spike up -d --build
```
Expected: app + worker + mysql + minio + nginx + dailyhot 容器 healthy。

- [ ] **Step 2: 造一条真发任务(stop_before_publish=False)**

Run:
```bash
docker exec -e PYTHONPATH=/app geo-spike-app-1 python /tmp/spike_real_publish.py
```
(若 `/tmp/spike_real_publish.py` 不在容器内,先 `docker cp "E:/geo/.claude/worktrees/headless-spike/spike_real_publish.py" geo-spike-app-1:/tmp/`。)
Expected: 打印 `task_id = <N> status = pending`。

- [ ] **Step 3: 发布进行中,确认 worker 容器内零 VNC 进程**

Run(发布约 30~60s 窗口内执行):
```bash
docker exec geo-spike-worker-1 sh -lc "ps -e -o comm= | grep -Ei 'xvfb|x11vnc|websockify' || echo NO_VNC_PROCESSES"
```
Expected: 输出 `NO_VNC_PROCESSES`(发布路径不再起这三个进程)。

- [ ] **Step 4: 确认记录落 succeeded**

Run:
```bash
docker exec geo-spike-worker-1 sh -lc "tail -n 80 /data/logs/*.log 2>/dev/null | grep -Ei 'succeeded|Record .* succeeded' | tail -n 5"
```
或经 MCP/接口查该 task 的 record 状态。
Expected: 记录 `status=succeeded`,无验证码/反爬/UserInputRequired;headless 全自动跑完。

- [ ] **Step 5: 记录验证结果到 memory + spec,提交**

更新 `C:\Users\Administrator\.claude\projects\e--geo\memory\project_publish_perf_optimization.md`:追加一行"Lever A 容器实测通过(2026-06-25,零 Xvfb 真发 succeeded)"。在 spec 第 6 节末尾标注"✅ 已实测"。

```bash
git -C "E:/geo/.claude/worktrees/headless-spike" add docs/superpowers/specs/2026-06-25-headless-publish-lever-a-design.md
git -C "E:/geo/.claude/worktrees/headless-spike" commit -m "docs(publish): Lever A 容器零 Xvfb 真发实测通过

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾说明(非任务)

- spike 脚手架(`Dockerfile`/`Dockerfile.nginx`/`services/dailyhot-api/Dockerfile` 的 daocloud 镜像站改、`.env`、`docker-compose.spike.yml`、`spike_*.py`、`cookies.txt`、`login.json`)**不进生产 PR**——它们只服务本机 spike 栈。开 PR 时只带 4 个源文件改动 + 测试 + docs。
- Lever B(放开 `MAX_CONCURRENT_RECORDS=5`)、Lever C(拆 `_account_login_loop` 多 worker)各自独立 spec/plan,不在本计划。

## Self-Review

**Spec 覆盖**:① headless 不起 VNC 链 → Task 1(displayless 会话)+ Task 2(run_publish 接线);② 撞失效标 failed+跳过+继续 → Task 3;③ 账号置 expired → Task 3;④ headed 回退逐字节一致 → Task 2/Task 3 的 headed 分支 + Task 3 Step 5 钉现存用例;⑤ 默认翻 True → Task 4;⑥ 容器零 X 实测 → Task 5。无遗漏。

**占位符扫描**:无 TBD/TODO;所有步骤含完整代码/命令/预期。

**类型一致性**:`with_display: bool` 在 Task 1 定义、Task 2 消费签名一致;`_expire_account_for_record(db, record_id)`、`failure_kind="login_required"`、`account.status="expired"` 在 Task 3 内自洽;`get_or_create_account_session(..., *, with_display=True)` 的 keyword-only 签名在 Task 1 实现与 Task 2 桩/调用一致。
