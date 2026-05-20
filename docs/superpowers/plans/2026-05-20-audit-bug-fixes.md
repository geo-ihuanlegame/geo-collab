# Audit Bug Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 12 high/critical bugs identified in the 2026-05-20 Geo 协作平台 audit report, in severity order.

**Architecture:** Fixes span five backend modules (`task_Executor`, `publish_Runner`, `browser_Session`, `account_Auth`, `main`) and one frontend file (`web/src/api/accounts.ts`). Each task is independent and commit-able on its own. No DB schema changes required.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy, Playwright sync API, threading, React 19 / TypeScript

---

## File Map

| File | Tasks |
|------|-------|
| `server/app/main.py` | T1 |
| `server/app/modules/accounts/browser_Session.py` | T1 |
| `server/app/modules/accounts/account_Auth.py` | T1, T2, T7 |
| `server/app/api/routes/tasks.py` | T1 |
| `server/app/modules/tasks/task_Executor.py` | T2, T3, T4, T5, T6 |
| `server/app/modules/tasks/publish_Runner.py` | T3 |
| `server/app/api/routes/accounts.py` | T7 |
| `web/src/api/accounts.ts` | T7 |

---

## Task 1: Quick-win exception and logging fixes (P0-6, P1-1, P1-3, P1-4, P1-6)

Five independent one-liner or two-liner fixes grouped into a single commit for efficiency.

**Files:**
- Modify: `server/app/main.py:104-109`
- Modify: `server/app/modules/accounts/account_Auth.py:107` (BaseException → Exception)
- Modify: `server/app/modules/accounts/account_Auth.py:682` (remove `from None`)
- Modify: `server/app/api/routes/tasks.py:261`
- Modify: `server/app/modules/accounts/browser_Session.py:657-670`

- [ ] **Step 1: Fix startup recovery silently swallowing exceptions — `main.py`**

Current code at `server/app/main.py:104-109`:
```python
try:
    recover_db = SessionLocal()
    recover_stuck_records(recover_db)
    recover_db.close()
except Exception:
    pass
```

Replace with:
```python
try:
    recover_db = SessionLocal()
    try:
        recover_stuck_records(recover_db)
    finally:
        recover_db.close()
except Exception:
    import logging as _logging
    _logging.getLogger(__name__).exception(
        "Startup recovery failed — stuck records may not have been reset"
    )
```

- [ ] **Step 2: Fix BaseException propagation in `_run_in_plain_thread` — `account_Auth.py:107`**

Current code in `account_Auth.py` inside `_target()`:
```python
    except BaseException:
        exc_type, exc, tb = sys.exc_info()
```

Replace `BaseException` with `Exception`:
```python
    except Exception:
        exc_type, exc, tb = sys.exc_info()
```

This prevents `SystemExit` / `KeyboardInterrupt` from a Playwright child thread being re-raised in the FastAPI worker thread.

- [ ] **Step 3: Remove `from None` that strips Playwright traceback — `account_Auth.py:682`**

Current code:
```python
    raise ClientError(f"Remote login page load failed: {home_url}") from None
```

Replace with:
```python
    raise ClientError(f"Remote login page load failed: {home_url}")
```

- [ ] **Step 4: Add `retry` SSE directive before stream break to stop reconnect storm — `tasks.py:258-263`**

Current code in `_generate()` inner function:
```python
            except GeneratorExit:
                break
            except Exception:
                logging.getLogger(__name__).exception("SSE error for task %s", task_id)
                break
```

Replace with:
```python
            except GeneratorExit:
                break
            except Exception:
                logging.getLogger(__name__).exception("SSE error for task %s", task_id)
                try:
                    yield "retry: 15000\nevent: error\ndata: {}\n\n"
                except Exception:
                    pass
                break
```

The `retry: 15000` SSE directive instructs the browser to wait 15 s before reconnecting, preventing a tight reconnect loop.

- [ ] **Step 5: Replace silent `pass` in cleanup thread with warning logs — `browser_Session.py:657-670`**

Current code in `_cleanup_loop()`:
```python
            try:
                _cleanup_stop_requested_sessions()
            except Exception:
                pass
            ...
                try:
                    _cleanup_stale_sessions(idle_timeout())
                except Exception:
                    pass
                try:
                    _cleanup_zombie_sessions()
                except Exception:
                    pass
```

Replace all three `pass` blocks:
```python
            try:
                _cleanup_stop_requested_sessions()
            except Exception:
                _logger.warning("stop-requested session cleanup failed", exc_info=True)
            ...
                try:
                    _cleanup_stale_sessions(idle_timeout())
                except Exception:
                    _logger.warning("stale session cleanup failed", exc_info=True)
                try:
                    _cleanup_zombie_sessions()
                except Exception:
                    _logger.warning("zombie session cleanup failed", exc_info=True)
```

- [ ] **Step 6: Run backend tests to ensure no regressions**

```bash
conda activate geo_xzpt && $env:GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"; pytest server/tests/ -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add server/app/main.py server/app/modules/accounts/account_Auth.py server/app/api/routes/tasks.py server/app/modules/accounts/browser_Session.py
git commit -m "fix: improve exception visibility and SSE reconnect safety"
```

---

## Task 2: Fix `_release_account_lock` TOCTOU race (P0-2)

**File:** `server/app/modules/tasks/task_Executor.py:304-310`

- [ ] **Step 1: Replace `locked()` pre-check with direct try/release**

Current code:
```python
def _release_account_lock(account_id: int) -> None:
    lock = _account_locks.get(account_id)
    if lock is not None and lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass
```

Replace with (remove the TOCTOU `lock.locked()` check):
```python
def _release_account_lock(account_id: int) -> None:
    lock = _account_locks.get(account_id)
    if lock is not None:
        try:
            lock.release()
        except RuntimeError:
            pass  # already released — harmless
```

The `RuntimeError: release unlocked lock` path is now reached only if the lock was already released by another caller, which is harmless. The previous `lock.locked()` + `lock.release()` pair was not atomic.

- [ ] **Step 2: Run tests**

```bash
conda activate geo_xzpt && $env:GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"; pytest server/tests/ -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add server/app/modules/tasks/task_Executor.py
git commit -m "fix: remove TOCTOU race in _release_account_lock"
```

---

## Task 3: Destroy broken browser context after publish failure (P0-5)

When `driver.publish()` raises a non-`UserInputRequired` exception, the current code closes the `page` but leaves the browser `context` attached to the session. The next publish attempt reuses this broken context. Fix: stop the entire session on failure.

**File:** `server/app/modules/tasks/publish_Runner.py:178-201`

- [ ] **Step 1: Add explicit session stop on publish failure**

Current code:
```python
    page = None
    _keep_browser = False
    try:
        page = context.new_page()
        _attach_page_network_diagnostics(page)
        with publish_step("driver publish flow", page=page):
            return driver.publish(
                page=page,
                context=context,
                payload=payload,
                stop_before_publish=stop_before_publish,
            )
    except UserInputRequired as exc:
        _keep_browser = True
        keep_session_alive(session.id)
        exc.session_id = session.id
        exc.novnc_url = session.novnc_url
        raise
    finally:
        if page is not None and not _keep_browser:
            try:
                page.close()
            except Exception:
                pass
```

Replace with:
```python
    page = None
    _keep_browser = False
    try:
        page = context.new_page()
        _attach_page_network_diagnostics(page)
        with publish_step("driver publish flow", page=page):
            return driver.publish(
                page=page,
                context=context,
                payload=payload,
                stop_before_publish=stop_before_publish,
            )
    except UserInputRequired as exc:
        _keep_browser = True
        keep_session_alive(session.id)
        exc.session_id = session.id
        exc.novnc_url = session.novnc_url
        raise
    except Exception:
        # Destroy the session so the broken context is not reused for the
        # next publish attempt on this account.
        stop_remote_browser_session(session.id)
        raise
    finally:
        if page is not None and not _keep_browser:
            try:
                page.close()
            except Exception:
                pass
```

Note: `stop_remote_browser_session` is already imported at the top of `publish_Runner.py` (line 19).

- [ ] **Step 2: Run tests**

```bash
conda activate geo_xzpt && $env:GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"; pytest server/tests/ -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add server/app/modules/tasks/publish_Runner.py
git commit -m "fix: stop browser session after publish failure to prevent broken context reuse"
```

---

## Task 4: Drain running futures before waiting_user_input early return (P0-3)

When ANY record enters `waiting_user_input`, `_run_pending_records` exits immediately even if other records are still running in the thread pool. Their `Future` results are never read; the records stay stuck in `running` until lease expiry + worker restart.

Fix: only exit when `running` is empty (all in-flight futures have completed).

**File:** `server/app/modules/tasks/task_Executor.py:191-210`

- [ ] **Step 1: Guard early-return exits with `not running` check**

Current code (lines 191-210):
```python
            else:
                if task.stop_before_publish:
                    if any(record.status == "waiting_manual_publish" for record in records):
                        db.commit()
                        return

                if any(record.status == "waiting_user_input" for record in records):
                    db.commit()
                    return

                _start_runnable_records(db, task, executor, running, records)
```

Replace with:
```python
            else:
                _paused_for_user = any(
                    record.status == "waiting_user_input" for record in records
                )
                _paused_for_manual = task.stop_before_publish and any(
                    record.status == "waiting_manual_publish" for record in records
                )

                if _paused_for_user or _paused_for_manual:
                    if not running:
                        # All in-flight futures have completed — safe to exit.
                        db.commit()
                        return
                    # There are still running futures. Fall through to the wait loop
                    # so they complete and their results get written to DB.
                else:
                    _start_runnable_records(db, task, executor, running, records)
```

- [ ] **Step 2: Run tests**

```bash
conda activate geo_xzpt && $env:GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"; pytest server/tests/ -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add server/app/modules/tasks/task_Executor.py
git commit -m "fix: drain running futures before exiting on waiting_user_input to prevent stuck records"
```

---

## Task 5: Force-terminate timed-out Playwright threads by stopping their browser session (P0-1)

`future.cancel()` has no effect on already-running threads. The fix: associate each running record with its browser session immediately after the session is obtained, then stop that session when the record times out. Stopping the session closes the Chromium context, which causes all in-flight Playwright calls to throw `TargetClosedError`, terminating the thread.

**Files:**
- Modify: `server/app/modules/tasks/publish_Runner.py` — pass `record_id` into `run_publish`
- Modify: `server/app/modules/tasks/task_Executor.py` — `build_publish_runner_for_record` passes `record.id`; timeout handler stops session

- [ ] **Step 1: Add `record_id` parameter to `run_publish` and associate session early**

In `server/app/modules/tasks/publish_Runner.py`, change the signature of `run_publish` (line 121) and add session pre-association:

```python
def run_publish(
    *,
    record_id: int | None = None,   # new optional parameter
    article: Article,
    account: Account,
    channel: str = "chromium",
    executable_path: str | None = None,
    stop_before_publish: bool = False,
) -> PublishResult:
    """Generic publish entry point. Looks up driver by account platform, reuses or starts remote session, runs driver.publish."""
    if not article.title or not article.title.strip():
        raise PublishError("标题不能为空")
    if article.cover_asset is None:
        raise PublishError("封面图片是必填项")

    platform_code, account_key = account_key_from_state_path(account.state_path)
    state_path = (get_data_dir() / account.state_path).resolve()
    if not state_path.exists():
        raise PublishError(f"Account storage state not found: {account.state_path}")

    driver = get_driver(platform_code)
    payload = _build_payload(article, account, account_key, platform_code, state_path)

    with publish_step("remote browser session"):
        session = get_or_create_account_session(platform_code, account_key)

    # Associate immediately so the timeout handler can stop this session
    # if the record exceeds its time budget.
    if record_id is not None:
        associate_record_with_session(record_id, session.id)

    # ... rest of function unchanged
```

(The rest of `run_publish` is unchanged — only the signature line and the four lines after `session = get_or_create_account_session(...)` change.)

`associate_record_with_session` is already imported at the top via:
```python
from server.app.modules.accounts import (
    attach_browser_handles,
    get_or_create_account_session,
    keep_session_alive,
    stop_remote_browser_session,
)
```

Add `associate_record_with_session` to that import:
```python
from server.app.modules.accounts import (
    associate_record_with_session,
    attach_browser_handles,
    get_or_create_account_session,
    keep_session_alive,
    stop_remote_browser_session,
)
```

- [ ] **Step 2: Pass `record.id` from `build_publish_runner_for_record`**

In `server/app/modules/tasks/task_Executor.py`, function `build_publish_runner_for_record` (line 533):

```python
def build_publish_runner_for_record(record: PublishRecord):
    from server.app.modules.tasks.publish_Runner import run_publish
    settings = get_settings()
    channel = settings.publish_browser_channel
    executable_path = settings.publish_browser_executable_path
    _record_id = record.id   # capture before detach

    def _runner(article, account, *, stop_before_publish=False):
        return run_publish(
            record_id=_record_id,   # new
            article=article,
            account=account,
            channel=channel,
            executable_path=executable_path,
            stop_before_publish=stop_before_publish,
        )

    return _runner
```

- [ ] **Step 3: Stop the browser session when a record times out**

In `task_Executor.py`, the timeout handling block (lines 218-229):

Current code:
```python
            for future in set(done) | set(timed_out):
                running_record = running.pop(future)
                if future in timed_out and not future.done():
                    _mark_record_failed(db, task.id, running_record.record_id, "Timeout: record execution exceeded 300s")
                    future.cancel()
                    try:
                        future.result(timeout=5)
                    except Exception:
                        pass
                    _release_account_lock(running_record.account_id)
                    db.commit()
                    continue
```

Replace with:
```python
            for future in set(done) | set(timed_out):
                running_record = running.pop(future)
                if future in timed_out and not future.done():
                    _mark_record_failed(db, task.id, running_record.record_id, "Timeout: record execution exceeded 300s")
                    # Stop the browser session: closing Chromium context causes the
                    # Playwright thread to receive TargetClosedError and terminate.
                    try:
                        session = get_session_for_record(running_record.record_id)
                        if session is not None:
                            stop_remote_browser_session(session.id)
                    except Exception:
                        _logger.warning(
                            "Failed to stop session for timed-out record %d",
                            running_record.record_id,
                            exc_info=True,
                        )
                    future.cancel()
                    try:
                        future.result(timeout=10)
                    except Exception:
                        pass
                    _release_account_lock(running_record.account_id)
                    db.commit()
                    continue
```

`get_session_for_record` and `stop_remote_browser_session` are already imported at lines 26-31 of `task_Executor.py`.

- [ ] **Step 4: Run tests**

```bash
conda activate geo_xzpt && $env:GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"; pytest server/tests/ -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/tasks/task_Executor.py server/app/modules/tasks/publish_Runner.py
git commit -m "fix: force-terminate timed-out Playwright threads by stopping their browser session"
```

---

## Task 6: Guard `cancel_task` terminal status write with DB-side condition (P1-2)

`cancel_task` (HTTP thread) and `execute_task` (worker thread) both write `task.status` without coordination. If both run concurrently when all records complete, each might see a non-terminal status and independently set the terminal status, causing a double-write.

Fix: use `sa_update` with a `NOT IN terminal_statuses` WHERE clause so only one writer wins.

**File:** `server/app/modules/tasks/task_Executor.py:551-574`

- [ ] **Step 1: Replace direct ORM assignment with guarded UPDATE for final status**

Current code in `cancel_task` (lines 559-572):
```python
    records = list_task_records(db, task.id)
    task.cancel_requested = True
    _cancel_not_running_records(db, task, records)

    refreshed_records = list_task_records(db, task.id)
    if not any(record.status == "running" for record in refreshed_records):
        task.status = "cancelled"
        task.finished_at = now
        add_log(db, task.id, None, "warn", "Task cancelled")
    else:
        task.status = "running"
        add_log(db, task.id, None, "warn", "Cancellation requested; running record will finish at its next safe point")
    db.flush()
```

Replace with:
```python
    records = list_task_records(db, task.id)
    task.cancel_requested = True
    _cancel_not_running_records(db, task, records)

    refreshed_records = list_task_records(db, task.id)
    if not any(record.status == "running" for record in refreshed_records):
        rows = db.execute(
            sa_update(PublishTask)
            .where(
                PublishTask.id == task.id,
                PublishTask.status.not_in(TERMINAL_TASK_STATUSES),
            )
            .values(status="cancelled", finished_at=now)
        ).rowcount
        if rows > 0:
            task.status = "cancelled"
            task.finished_at = now
            add_log(db, task.id, None, "warn", "Task cancelled")
    else:
        task.status = "running"
        add_log(db, task.id, None, "warn", "Cancellation requested; running record will finish at its next safe point")
    db.flush()
```

- [ ] **Step 2: Run tests**

```bash
conda activate geo_xzpt && $env:GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"; pytest server/tests/ -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add server/app/modules/tasks/task_Executor.py
git commit -m "fix: guard cancel_task terminal status write with DB-side condition to prevent race"
```

---

## Task 7: Non-blocking login session endpoint — split create from wait (P0-4)

**Root cause:** `start_login_session` / `start_account_login_session` poll the DB for up to 90 s inside the HTTP handler thread, exhausting the uvicorn thread pool under concurrent login load.

**Fix:** Split the existing `POST /login-session` into two calls:
1. `POST /login-session` → creates the `AccountLoginSession` DB record and returns immediately with `{session_id, status: "pending"}`. No blocking.
2. New `GET /login-session/{session_id}` → returns current status, plus `novnc_url` once the worker has started the browser. Frontend polls this until `status == "active"`.

Frontend changes: update `startLoginSession` / `startExistingAccountLoginSession` to poll the status endpoint until active, then proceed.

**Files:**
- Modify: `server/app/modules/accounts/account_Auth.py` — `_start_login_browser_via_worker`, `start_login_session`, `start_account_login_session`
- Modify: `server/app/api/routes/accounts.py` — add GET status endpoint
- Modify: `web/src/api/accounts.ts` — update callers

- [ ] **Step 1: Add `get_login_session_status` function to `account_Auth.py`**

Add after the `_find_account_login_request` function (around line 294):

```python
def get_login_session_status(db: Session, account: Account, session_id: str) -> AccountLoginSession | None:
    """Return the raw AccountLoginSession row for status polling."""
    return _find_account_login_request(db, account.id, session_id)
```

- [ ] **Step 2: Make `_start_login_browser_via_worker` return immediately without blocking**

Current signature and body (lines 322-366):
```python
def _start_login_browser_via_worker(
    db: Session,
    account_id: int,
    platform_code: str,
    account_key: str,
    channel: str,
    executable_path: str | None,
    previous_status: str | None = None,
) -> LoginBrowserSessionHandle:
    request = AccountLoginSession(...)
    db.add(request)
    db.commit()

    try:
        request = _wait_for_account_login_request(
            db, request.id, {LOGIN_STATUS_ACTIVE},
            LOGIN_SESSION_START_TIMEOUT_SECONDS,
            "Worker did not start the account login browser in time",
        )
    except ClientError:
        ...
        raise

    if not request.browser_session_id or not request.novnc_url:
        raise ClientError("Worker started login session without browser connection info")
    return LoginBrowserSessionHandle(id=request.browser_session_id, novnc_url=request.novnc_url)
```

Replace with (keep everything up to and including `db.commit()`, drop the blocking wait):
```python
def _start_login_browser_via_worker(
    db: Session,
    account_id: int,
    platform_code: str,
    account_key: str,
    channel: str,
    executable_path: str | None,
    previous_status: str | None = None,
) -> str:
    """Create a login-session request row and return the request ID immediately.

    The caller must poll get_login_session_status() until status == LOGIN_STATUS_ACTIVE
    before the novnc_url is available.
    """
    request = AccountLoginSession(
        id=_new_login_session_request_id(),
        account_id=account_id,
        platform_code=platform_code,
        account_key=account_key,
        channel=channel,
        executable_path=executable_path,
        status=LOGIN_STATUS_PENDING,
        previous_status=previous_status,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(request)
    db.commit()
    return request.id
```

- [ ] **Step 3: Update `start_login_session` and `start_account_login_session` return types**

`AccountBrowserSessionResult` currently requires `novnc_url`. Update it to make `novnc_url` optional and add `login_session_request_id`:

In `account_Auth.py`, the `AccountBrowserSessionResult` dataclass (lines 51-57):
```python
@dataclass(frozen=True)
class AccountBrowserSessionResult:
    account: Account
    platform_code: str
    account_key: str
    session_id: str           # this is the login-session REQUEST id now
    novnc_url: str | None     # None until worker starts the browser
```

Update `start_login_session` (lines 208-223):
```python
    request_id = _start_login_browser_via_worker(
        db,
        account.id,
        platform_code,
        account_key,
        payload.channel,
        payload.executable_path,
        previous_status=previous_status,
    )
    return AccountBrowserSessionResult(
        account=get_account(db, account.id) or account,
        platform_code=platform_code,
        account_key=account_key,
        session_id=request_id,
        novnc_url=None,
    )
```

Update `start_account_login_session` (lines 234-249) the same way:
```python
    request_id = _start_login_browser_via_worker(
        db,
        account.id,
        platform_code,
        account_key,
        payload.channel,
        payload.executable_path,
        previous_status=previous_status,
    )
    return AccountBrowserSessionResult(
        account=get_account(db, account.id) or account,
        platform_code=platform_code,
        account_key=account_key,
        session_id=request_id,
        novnc_url=None,
    )
```

- [ ] **Step 4: Add GET status endpoint in `accounts.py`**

Add the following imports and route to `server/app/api/routes/accounts.py`. Add the import at the top with the other `modules.accounts` imports:

```python
from server.app.modules.accounts import (
    ...
    get_login_session_status,  # new
    ...
)
```

Add the route after the `stop_existing_account_login_session_endpoint`:
```python
@router.get("/{account_id:int}/login-session/{session_id}/status")
def get_login_session_status_endpoint(
    account_id: int,
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Poll this endpoint after POST /login-session until status == 'active'."""
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    request = get_login_session_status(db, account, session_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Login session not found")
    return {
        "status": request.status,
        "novnc_url": request.novnc_url,
        "error_message": request.error_message,
        "browser_session_id": request.browser_session_id,
    }
```

Also add `get_login_session_status` to the `modules.accounts` `__init__.py` exports if not already present. Check `server/app/modules/accounts/__init__.py` — if `get_login_session_status` isn't exported there, add it.

- [ ] **Step 5: Update `AccountBrowserSessionRead` schema to allow null `novnc_url`**

Find the schema (likely in `server/app/schemas/account.py`). Change:
```python
class AccountBrowserSessionRead(BaseModel):
    account: AccountRead
    platform_code: str
    account_key: str
    session_id: str
    novnc_url: str
```
to:
```python
class AccountBrowserSessionRead(BaseModel):
    account: AccountRead
    platform_code: str
    account_key: str
    session_id: str
    novnc_url: str | None = None
```

- [ ] **Step 6: Update frontend `web/src/api/accounts.ts` — make login session calls non-blocking**

Find the `startLoginSession` and `startExistingAccountLoginSession` helpers (currently they POST and get back a fully-ready `AccountBrowserSession`). Wrap them to poll status until `active`:

```typescript
// In web/src/api/accounts.ts — add a helper that polls until active
async function pollLoginSessionUntilActive(
  accountId: number,
  sessionId: string,
  timeoutMs = 90_000,
): Promise<AccountBrowserSession> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const status = await api<{
      status: string;
      novnc_url: string | null;
      error_message: string | null;
      browser_session_id: string | null;
    }>(`/api/accounts/${accountId}/login-session/${sessionId}/status`);

    if (status.status === "active") {
      return {
        session_id: status.browser_session_id ?? sessionId,
        novnc_url: status.novnc_url ?? "",
      } as unknown as AccountBrowserSession;
    }
    if (status.status === "failed") {
      throw new Error(status.error_message || "Login session failed to start");
    }
    // pending / starting — keep polling
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error("Login session did not become active within 90s");
}
```

Then wherever `startLoginSession` or `startExistingAccountLoginSession` is called and the caller needs `novnc_url` immediately, chain it with `pollLoginSessionUntilActive`.

Locate the call sites in `web/src/features/` (likely `accounts/` feature folder) and update them to: call the POST endpoint → get back `{session_id, novnc_url: null}` → start polling in background → show "Starting browser…" UI state → open noVNC once `novnc_url` available.

- [ ] **Step 7: Also export `get_login_session_status` from accounts module `__init__.py`**

Check `server/app/modules/accounts/__init__.py`. If `get_login_session_status` is not in the import list, add it alongside `start_login_session`, `finish_account_login_session`, etc.

- [ ] **Step 8: Run tests**

```bash
conda activate geo_xzpt && $env:GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"; pytest server/tests/ -q
```

Expected: all tests pass. If any tests call `start_login_session` or check `novnc_url` on the immediate response, update them to expect `None` and to poll separately.

- [ ] **Step 9: Commit**

```bash
git add server/app/modules/accounts/account_Auth.py server/app/schemas/account.py server/app/api/routes/accounts.py server/app/modules/accounts/__init__.py web/src/api/accounts.ts
git commit -m "fix: make login-session endpoint non-blocking; frontend polls status for novnc_url"
```

---

## Self-Review Checklist

| # | Requirement | Task |
|---|-------------|------|
| P0-6 | Startup recovery logs instead of swallowing | T1 Step 1 |
| P1-1 | BaseException → Exception in plain-thread wrapper | T1 Step 2 |
| P1-3 | `from None` removed so Playwright traceback preserved | T1 Step 3 |
| P1-4 | SSE retry directive stops reconnect storm | T1 Step 4 |
| P1-6 | Cleanup thread logs warnings instead of `pass` | T1 Step 5 |
| P0-2 | `_release_account_lock` TOCTOU removed | T2 |
| P0-5 | Context stopped on non-UserInputRequired exception | T3 |
| P0-3 | Running futures drained before waiting_user_input exit | T4 |
| P0-1 | Timed-out record stops its browser session to kill thread | T5 |
| P1-2 | cancel_task uses DB-side guard against double terminal write | T6 |
| P0-4 | Login session endpoint non-blocking; 90s poll moved to frontend | T7 |
| P1-5 | (Not tasked — DB connection pool settings `pool_size=5, max_overflow=10, pool_pre_ping=True` are already correctly set in `session.py`. The fragmentation concern is acceptable with current pool config.) | — |
