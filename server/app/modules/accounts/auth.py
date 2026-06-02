from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from server.app.core.config import get_settings
from server.app.core.paths import ensure_data_dirs, get_data_dir
from server.app.core.time import utcnow
from server.app.modules.accounts.models import Account, AccountLoginSession
from server.app.modules.accounts.schemas import (
    AccountCheckRequest,
    AccountExportRequest,
    PlatformLoginRequest,
)
from server.app.modules.accounts.service import (
    _get_driver,
    account_key_from_state_path,
    clear_profile_locks,
    get_account,
    get_or_create_platform,
    launch_options,
    normalize_account_key,
    profile_dir_from_state_path,
    profile_key_from_state_path,
    relative_to_data_dir,
    state_dir_for_key,
    state_dir_from_state_path,
    state_path_for_key,
)
from server.app.shared.errors import ClientError

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrowserCheckResult:
    logged_in: bool
    url: str
    title: str


@dataclass(frozen=True)
class AccountBrowserSessionResult:
    account: Account
    platform_code: str
    account_key: str
    session_id: str
    novnc_url: str | None
    status: str | None = None
    queue_reason: str | None = None


LOGIN_STATUS_PENDING = "pending"
LOGIN_STATUS_QUEUED = "queued"
LOGIN_STATUS_STARTING = "starting"
LOGIN_STATUS_ACTIVE = "active"
LOGIN_STATUS_FINISH_REQUESTED = "finish_requested"
LOGIN_STATUS_FINISHING = "finishing"
LOGIN_STATUS_FINISHED = "finished"
LOGIN_STATUS_CANCEL_REQUESTED = "cancel_requested"
LOGIN_STATUS_CANCELLING = "cancelling"
LOGIN_STATUS_CANCELLED = "cancelled"
LOGIN_STATUS_FAILED = "failed"

LOGIN_TERMINAL_STATUSES = {
    LOGIN_STATUS_FINISHED,
    LOGIN_STATUS_CANCELLED,
    LOGIN_STATUS_FAILED,
}

LOGIN_SESSION_START_TIMEOUT_SECONDS = 90.0
LOGIN_SESSION_FINISH_TIMEOUT_SECONDS = 45.0
LOGIN_SESSION_CANCEL_TIMEOUT_SECONDS = 5.0
LOGIN_SESSION_POLL_SECONDS = 0.25


def _run_in_plain_thread(fn: Callable[[], Any]) -> Any:
    """Run Playwright sync API work outside FastAPI/AnyIO worker threads."""
    result: list[Any] = []
    error: list[tuple[type[BaseException], BaseException, Any]] = []

    def _target() -> None:
        # Python 3.10+ copies the parent's contextvars Context into new threads
        # via threading.Thread. On Python 3.13+ asyncio stores _running_loop as
        # a ContextVar, so the spawned thread inherits the main event loop and
        # Playwright's sync-API guard fires. Reset it before any Playwright call.
        try:
            import asyncio.events as _ae

            if hasattr(_ae, "_set_running_loop"):
                _ae._set_running_loop(None)
        except Exception:
            pass
        try:
            result.append(fn())
        except Exception:
            exc_type, exc, tb = sys.exc_info()
            if exc_type is not None and exc is not None:
                error.append((exc_type, exc, tb))

    worker = threading.Thread(target=_target, name="geo-playwright-sync", daemon=False)
    worker.start()
    worker.join()
    if error:
        _, exc, tb = error[0]
        raise exc.with_traceback(tb)
    return result[0] if result else None


def register_account_from_storage_state(
    db: Session,
    user_id: int,
    platform_code: str,
    payload: PlatformLoginRequest,
) -> Account:
    if payload.use_browser:
        raise ClientError("Browser login must use login-session")

    driver = _get_driver(platform_code)
    platform = get_or_create_platform(db, driver.code, driver.name, driver.home_url)
    account_key = normalize_account_key(payload.account_key)
    state_path = state_path_for_key(platform_code, account_key, user_id=user_id)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    _copy_legacy_state_if_needed(platform_code, account_key, state_path)
    if not state_path.exists():
        raise ClientError(f"Storage state not found: {state_path}")

    relative_state_path = relative_to_data_dir(state_path)
    legacy_relative_state_path = relative_to_data_dir(
        state_path_for_key(platform_code, account_key)
    )
    account = db.execute(
        select(Account).where(
            Account.user_id == user_id,
            Account.platform_id == platform.id,
            Account.state_path.in_([relative_state_path, legacy_relative_state_path]),
        )
    ).scalar_one_or_none()
    now = utcnow()
    if account is None:
        account = Account(
            user_id=user_id,
            platform=platform,
            display_name=payload.display_name,
            platform_user_id=None,
            status="valid",
            state_path=relative_state_path,
            note=payload.note,
            last_login_at=now,
            last_checked_at=now,
        )
        db.add(account)
    else:
        account.display_name = payload.display_name
        account.status = "valid"
        account.state_path = relative_state_path
        account.note = payload.note
        account.is_deleted = False
        account.deleted_at = None
        account.last_login_at = now
        account.last_checked_at = now
        account.updated_at = now

    db.flush()
    return get_account(db, account.id) or account


def start_login_session(
    db: Session,
    user_id: int,
    platform_code: str,
    payload: PlatformLoginRequest,
) -> AccountBrowserSessionResult:
    driver = _get_driver(platform_code)
    platform = get_or_create_platform(db, driver.code, driver.name, driver.home_url)
    account_key = normalize_account_key(payload.account_key)
    state_path = state_path_for_key(platform_code, account_key, user_id=user_id)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    relative_state_path = relative_to_data_dir(state_path)
    legacy_relative_state_path = relative_to_data_dir(
        state_path_for_key(platform_code, account_key)
    )
    account = db.execute(
        select(Account).where(
            Account.user_id == user_id,
            Account.platform_id == platform.id,
            Account.state_path.in_([relative_state_path, legacy_relative_state_path]),
        )
    ).scalar_one_or_none()
    now = utcnow()
    if account is None:
        previous_status = None
        account = Account(
            user_id=user_id,
            platform=platform,
            display_name=payload.display_name,
            platform_user_id=None,
            status="unknown",
            state_path=relative_state_path,
            note=payload.note,
            last_checked_at=now,
        )
        db.add(account)
    else:
        previous_status = account.status
        account.display_name = payload.display_name
        account.note = payload.note
        account.status = "unknown"
        account.state_path = relative_state_path
        account.is_deleted = False
        account.deleted_at = None
        account.last_checked_at = now
        account.updated_at = now
    db.flush()

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
        status=LOGIN_STATUS_PENDING,
    )


def _copy_legacy_state_if_needed(
    platform_code: str, account_key: str, user_state_path: Path
) -> None:
    if user_state_path.exists():
        return
    legacy_dir = state_dir_for_key(platform_code, account_key)
    legacy_state = legacy_dir / "storage_state.json"
    if not legacy_state.exists():
        return
    user_dir = user_state_path.parent
    user_dir.mkdir(parents=True, exist_ok=True)
    for child in legacy_dir.iterdir():
        dest = user_dir / child.name
        if child.is_dir():
            if not dest.exists():
                shutil.copytree(child, dest)
        elif child.is_file() and not dest.exists():
            shutil.copy2(child, dest)


def start_account_login_session(
    db: Session, account: Account, payload: AccountCheckRequest
) -> AccountBrowserSessionResult:
    platform_code, account_key = account_key_from_state_path(account.state_path)
    previous_status = account.status
    account.status = "unknown"
    account.last_checked_at = utcnow()
    account.updated_at = account.last_checked_at
    db.flush()

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
        status=LOGIN_STATUS_PENDING,
    )


def finish_account_login_session(
    db: Session, account: Account, session_id: str
) -> tuple[Account, BrowserCheckResult]:
    request = _find_account_login_request(db, account.id, session_id)
    if request is not None:
        return _finish_login_browser_via_worker(db, account, request)

    platform_code, account_key = account_key_from_state_path(account.state_path)
    result = _finish_login_browser_local(platform_code, account_key, account.state_path, session_id)
    _apply_login_result(account, result)
    db.flush()
    return get_account(db, account.id) or account, result


def stop_account_login_session(db: Session, account: Account, session_id: str) -> None:
    request = _find_account_login_request(db, account.id, session_id)
    if request is not None:
        _cancel_login_browser_via_worker(db, request)
        return

    _, account_key = account_key_from_state_path(account.state_path)
    _stop_login_browser_local(account_key, session_id)


def _new_login_session_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _touch_login_request(request: AccountLoginSession) -> None:
    request.updated_at = utcnow()


def _find_account_login_request(
    db: Session, account_id: int, session_id: str
) -> AccountLoginSession | None:
    return db.execute(
        select(AccountLoginSession)
        .where(
            AccountLoginSession.account_id == account_id,
            or_(
                AccountLoginSession.id == session_id,
                AccountLoginSession.browser_session_id == session_id,
            ),
        )
        .order_by(AccountLoginSession.updated_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_login_session_status(
    db: Session, account: Account, session_id: str
) -> AccountLoginSession | None:
    """Return the AccountLoginSession row for status polling."""
    return _find_account_login_request(db, account.id, session_id)


def _wait_for_account_login_request(
    db: Session,
    request_id: str,
    desired_statuses: set[str],
    timeout_seconds: float,
    timeout_message: str,
) -> AccountLoginSession:
    deadline = time.monotonic() + timeout_seconds
    while True:
        db.rollback()
        db.expire_all()
        request = db.get(AccountLoginSession, request_id)
        if request is None:
            raise ClientError(f"Account login session not found: {request_id}")
        if request.status in desired_statuses:
            return request
        if request.status == LOGIN_STATUS_FAILED:
            raise ClientError(request.error_message or "Account login session failed")
        if request.status in LOGIN_TERMINAL_STATUSES:
            raise ClientError(f"Account login session is {request.status}")
        if time.monotonic() >= deadline:
            raise ClientError(timeout_message)
        time.sleep(LOGIN_SESSION_POLL_SECONDS)


def _start_login_browser_via_worker(
    db: Session,
    account_id: int,
    platform_code: str,
    account_key: str,
    channel: str,
    executable_path: str | None,
    previous_status: str | None = None,
) -> str:
    """Create a login-session request row and return the request ID immediately."""
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


def _finish_login_browser_via_worker(
    db: Session,
    account: Account,
    request: AccountLoginSession,
) -> tuple[Account, BrowserCheckResult]:
    if request.account_id != account.id:
        raise ClientError("Remote browser session does not belong to this account")
    if request.status == LOGIN_STATUS_ACTIVE:
        request.status = LOGIN_STATUS_FINISH_REQUESTED
        _touch_login_request(request)
        db.commit()
    elif request.status not in {
        LOGIN_STATUS_FINISH_REQUESTED,
        LOGIN_STATUS_FINISHING,
        LOGIN_STATUS_FINISHED,
    }:
        raise ClientError(f"Account login session is {request.status}")

    request = _wait_for_account_login_request(
        db,
        request.id,
        {LOGIN_STATUS_FINISHED},
        LOGIN_SESSION_FINISH_TIMEOUT_SECONDS,
        "Worker did not finish the account login session in time",
    )

    result = BrowserCheckResult(
        logged_in=bool(request.logged_in),
        url=request.result_url or "",
        title=request.result_title or "",
    )
    db.expire_all()
    return get_account(db, account.id) or account, result


def _cancel_login_browser_via_worker(db: Session, request: AccountLoginSession) -> None:
    if request.status in LOGIN_TERMINAL_STATUSES:
        return
    if request.status in {LOGIN_STATUS_PENDING, LOGIN_STATUS_QUEUED}:
        request.status = LOGIN_STATUS_CANCELLED
        request.queue_reason = None
        _touch_login_request(request)
        db.commit()
        return

    request.status = LOGIN_STATUS_CANCEL_REQUESTED
    _touch_login_request(request)
    db.commit()
    try:
        _wait_for_account_login_request(
            db,
            request.id,
            {LOGIN_STATUS_CANCELLED},
            LOGIN_SESSION_CANCEL_TIMEOUT_SECONDS,
            "Worker did not cancel the account login session in time",
        )
    except ClientError:
        _logger.warning(
            "Account login session cancel is still pending: %s", request.id, exc_info=True
        )


def process_account_login_session_requests(db: Session, worker_id: str) -> bool:
    """Claim and process one account-login browser command for this worker."""
    row = db.execute(
        select(AccountLoginSession.id, AccountLoginSession.status)
        .where(
            or_(
                and_(
                    AccountLoginSession.status.in_([LOGIN_STATUS_PENDING, LOGIN_STATUS_QUEUED]),
                    AccountLoginSession.worker_id.is_(None),
                ),
                and_(
                    AccountLoginSession.worker_id == worker_id,
                    AccountLoginSession.status.in_(
                        [LOGIN_STATUS_FINISH_REQUESTED, LOGIN_STATUS_CANCEL_REQUESTED]
                    ),
                ),
            )
        )
        .order_by(AccountLoginSession.updated_at.asc())
        .limit(1)
    ).first()
    if row is None:
        return False

    request_id, status = row
    if status in {LOGIN_STATUS_PENDING, LOGIN_STATUS_QUEUED}:
        request = db.get(AccountLoginSession, request_id)
        if request is None:
            return False
        if not _try_acquire_login_profile_lock(db, request):
            return True
        next_status = LOGIN_STATUS_STARTING
    else:
        next_status = {
            LOGIN_STATUS_FINISH_REQUESTED: LOGIN_STATUS_FINISHING,
            LOGIN_STATUS_CANCEL_REQUESTED: LOGIN_STATUS_CANCELLING,
        }[status]
    where_clause = [AccountLoginSession.id == request_id, AccountLoginSession.status == status]
    if status in {LOGIN_STATUS_PENDING, LOGIN_STATUS_QUEUED}:
        where_clause.append(AccountLoginSession.worker_id.is_(None))
    else:
        where_clause.append(AccountLoginSession.worker_id == worker_id)

    result = db.execute(
        sa_update(AccountLoginSession)
        .where(*where_clause)
        .values(status=next_status, worker_id=worker_id, queue_reason=None, updated_at=utcnow())
    )
    rows = result.rowcount  # type: ignore[attr-defined]  # DML execute returns CursorResult
    db.commit()
    if rows == 0:
        return False

    request = db.get(AccountLoginSession, request_id)
    if request is None:
        return False
    if next_status == LOGIN_STATUS_STARTING:
        _worker_start_login_session(db, request)
    elif next_status == LOGIN_STATUS_FINISHING:
        _worker_finish_login_session(db, request)
    elif next_status == LOGIN_STATUS_CANCELLING:
        _worker_cancel_login_session(db, request)
    return True


def _try_acquire_login_profile_lock(db: Session, request: AccountLoginSession) -> bool:
    from server.app.modules.accounts.browser import try_acquire_profile_lock

    account = db.get(Account, request.account_id)
    if account is None:
        request.status = LOGIN_STATUS_FAILED
        request.error_message = "Account not found"
        request.queue_reason = None
        _touch_login_request(request)
        db.commit()
        return False

    profile_key = profile_key_from_state_path(account.state_path)
    reason = "账号正在执行发布或登录操作，登录请求已排队"
    if try_acquire_profile_lock(
        profile_key, owner_kind="login", owner_id=request.id, queue_reason=reason
    ):
        return True

    request.status = LOGIN_STATUS_QUEUED
    request.queue_reason = reason
    request.worker_id = None
    _touch_login_request(request)
    db.commit()
    return False


def _worker_start_login_session(db: Session, request: AccountLoginSession) -> None:
    try:
        account = db.get(Account, request.account_id)
        if account is None:
            raise ClientError("Account not found")
        platform_code = request.platform_code
        account_key = request.account_key
        session = _start_login_browser_impl(
            platform_code,
            account_key,
            account.state_path,
            request.channel,
            request.executable_path,
            False,
        )
        request.browser_session_id = session.id
        request.novnc_url = session.novnc_url
        request.status = LOGIN_STATUS_ACTIVE
        request.error_message = None
        request.queue_reason = None
        _touch_login_request(request)
        db.commit()
        _load_login_page_for_session(
            session, platform_code, account_key, _get_driver(platform_code).home_url
        )
        return
    except Exception as exc:
        request.status = LOGIN_STATUS_FAILED
        request.error_message = str(exc)
        request.queue_reason = None
        _release_login_profile_lock(db, request)
        _logger.exception("Failed to start account login session %s", request.id)
        _touch_login_request(request)
        db.commit()


def _worker_finish_login_session(db: Session, request: AccountLoginSession) -> None:
    account = db.get(Account, request.account_id)
    if account is None:
        request.status = LOGIN_STATUS_FAILED
        request.error_message = "Account not found"
        _touch_login_request(request)
        db.commit()
        return

    try:
        if not request.browser_session_id:
            raise ClientError("Remote browser session not found")
        result = _finish_login_browser_impl(
            request.platform_code,
            request.account_key,
            account.state_path,
            request.browser_session_id,
        )
        _apply_login_result(account, result)
        request.logged_in = result.logged_in
        request.result_url = result.url
        request.result_title = result.title
        request.status = LOGIN_STATUS_FINISHED
        request.error_message = None
        request.queue_reason = None
    except Exception as exc:
        request.status = LOGIN_STATUS_FAILED
        request.error_message = str(exc)
        request.queue_reason = None
        _logger.exception("Failed to finish account login session %s", request.id)
    finally:
        _release_login_profile_lock(db, request)
        _touch_login_request(request)
        db.commit()


def _worker_cancel_login_session(db: Session, request: AccountLoginSession) -> None:
    try:
        if request.browser_session_id:
            _stop_login_browser_impl(request.account_key, request.browser_session_id)
        request.status = LOGIN_STATUS_CANCELLED
        request.error_message = None
        request.queue_reason = None
        if request.previous_status is not None:
            account = db.get(Account, request.account_id)
            if account is not None and account.status == "unknown":
                account.status = request.previous_status
                account.updated_at = utcnow()
    except Exception as exc:
        request.status = LOGIN_STATUS_FAILED
        request.error_message = str(exc)
        request.queue_reason = None
        _logger.exception("Failed to cancel account login session %s", request.id)
    finally:
        _release_login_profile_lock(db, request)
        _touch_login_request(request)
        db.commit()


def _release_login_profile_lock(db: Session, request: AccountLoginSession) -> None:
    from server.app.modules.accounts.browser import release_profile_lock

    account = db.get(Account, request.account_id)
    if account is None:
        return
    try:
        release_profile_lock(
            profile_key_from_state_path(account.state_path), owner_kind="login", owner_id=request.id
        )
    except Exception:
        _logger.warning(
            "Failed to release login profile lock for request %s", request.id, exc_info=True
        )


def _apply_login_result(account: Account, result: BrowserCheckResult) -> None:
    now = utcnow()
    account.status = "valid" if result.logged_in else "expired"
    account.last_checked_at = now
    if result.logged_in:
        account.last_login_at = now
    account.updated_at = now


def _finish_login_browser_local(
    platform_code: str,
    account_key: str,
    state_path: str,
    session_id: str,
) -> BrowserCheckResult:
    return _run_in_plain_thread(
        lambda: _finish_login_browser_impl(platform_code, account_key, state_path, session_id)
    )


def _finish_login_browser_impl(
    platform_code: str, account_key: str, state_path: str, session_id: str
) -> BrowserCheckResult:
    from server.app.modules.accounts.browser import get_session, stop_remote_browser_session

    session = get_session(session_id)
    if session is None:
        raise ClientError(f"Remote browser session not found: {session_id}")
    if session.account_key != account_key:
        raise ClientError("Remote browser session does not belong to this account")
    if session.browser_context is None:
        raise ClientError("Remote browser session has no browser context")

    try:
        return _read_and_save_login_state_from_remote_session(
            session,
            platform_code,
            get_data_dir() / state_path,
        )
    finally:
        stop_remote_browser_session(session_id)


def _stop_login_browser_local(account_key: str, session_id: str) -> None:
    _run_in_plain_thread(lambda: _stop_login_browser_impl(account_key, session_id))


def _stop_login_browser_impl(account_key: str, session_id: str) -> None:
    from server.app.modules.accounts.browser import get_session, stop_remote_browser_session

    session = get_session(session_id)
    if session is None:
        return
    if session.account_key != account_key:
        raise ClientError("Remote browser session does not belong to this account")
    stop_remote_browser_session(session_id)


def _start_login_browser(
    platform_code: str, account_key: str, channel: str, executable_path: str | None
):
    state_path = relative_to_data_dir(state_path_for_key(platform_code, account_key))
    return _run_in_plain_thread(
        lambda: _start_login_browser_impl(
            platform_code, account_key, state_path, channel, executable_path
        )
    )


def _start_login_browser_impl(
    platform_code: str,
    account_key: str,
    state_path: str,
    channel: str,
    executable_path: str | None,
    load_login_page: bool = True,
):
    from playwright.sync_api import sync_playwright

    from server.app.modules.accounts.browser import (
        attach_browser_handles,
        keep_session_alive,
        start_remote_browser_session,
        stop_remote_browser_session,
    )

    driver = _get_driver(platform_code)
    ensure_data_dirs()
    state_dir = state_dir_from_state_path(state_path)
    profile_dir = profile_dir_from_state_path(state_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    session = start_remote_browser_session(
        account_key,
        platform_code=platform_code,
        profile_key=profile_key_from_state_path(state_path),
    )
    pw = None
    context = None
    try:
        pw = sync_playwright().start()
        options = launch_options(channel, executable_path)
        options["env"] = {**os.environ, "DISPLAY": session.display}

        clear_profile_locks(profile_dir)
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            **options,
        )
        context.set_default_navigation_timeout(30000)
        page = _primary_page_for_context(context)
        attach_browser_handles(
            session.id, pw, context, page, context_thread_id=threading.get_ident()
        )
        keep_session_alive(session.id)
        if load_login_page:
            _load_login_page_for_session(
                session, platform_code, account_key, driver.home_url, raise_on_error=True
            )
        return session
    except Exception:
        try:
            if context is not None:
                context.close()
        finally:
            if pw is not None:
                pw.stop()
        stop_remote_browser_session(session.id)
        raise


def _primary_page_for_context(context):
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        for extra_page in pages[1:]:
            try:
                extra_page.close()
            except Exception:
                pass
        return page
    return context.new_page()


def _load_login_page(page, platform_code: str, account_key: str, home_url: str) -> None:
    try:
        page.goto(home_url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            _logger.warning(
                "Remote login page did not reach networkidle for %s account %s",
                platform_code,
                account_key,
                exc_info=True,
            )
    except Exception as exc:
        _logger.warning(
            "Remote login page load failed for %s account %s",
            platform_code,
            account_key,
            exc_info=True,
        )
        raise ClientError(f"Remote login page load failed: {home_url}") from exc


def _load_login_page_for_session(
    session,
    platform_code: str,
    account_key: str,
    home_url: str,
    raise_on_error: bool = False,
) -> None:
    operation_lock = getattr(session, "operation_lock", None)
    if operation_lock is None or getattr(session, "page", None) is None:
        return
    with operation_lock:
        page = getattr(session, "page", None)
        if page is None:
            return
        try:
            _load_login_page(page, platform_code, account_key, home_url)
        except Exception:
            _logger.warning(
                "Login page load failed for %s/%s", platform_code, account_key, exc_info=True
            )
            if raise_on_error:
                raise


def _start_login_page_loader(
    session_id: str, platform_code: str, account_key: str, home_url: str
) -> None:
    def _load() -> None:
        from server.app.modules.accounts.browser import get_session

        session = get_session(session_id)
        if session is None or session.page is None:
            return
        with session.operation_lock:
            page = session.page
            if page is None:
                return
            try:
                _load_login_page(page, platform_code, account_key, home_url)
            except Exception:
                _logger.warning(
                    "Async login page load failed for %s/%s",
                    platform_code,
                    account_key,
                    exc_info=True,
                )

    worker = threading.Thread(
        target=_load,
        daemon=True,
        name=f"geo-login-page-loader-{platform_code}-{account_key}",
    )
    worker.start()


def _read_and_save_login_state_from_remote_session(
    session, platform_code: str, state_path: Path
) -> BrowserCheckResult:
    with session.operation_lock:
        result = _read_login_state_from_remote_session(session, platform_code)
        session.browser_context.storage_state(path=str(state_path))
        return result


def _read_login_state_from_remote_session(session, platform_code: str) -> BrowserCheckResult:
    driver = _get_driver(platform_code)
    context = session.browser_context
    page = session.page
    if context is not None:
        pages = list(getattr(context, "pages", []) or [])
        if pages:
            page = pages[-1]
    if page is None:
        raise ClientError("Remote browser session has no page")

    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    url = page.url
    title = ""
    body = ""
    try:
        title = page.title()
        body = page.locator("body").inner_text(timeout=5000)
    except Exception:
        _logger.warning("Remote login state read failed", exc_info=True)

    return BrowserCheckResult(
        logged_in=driver.detect_logged_in(url=url, title=title, body=body),
        url=url,
        title=title,
    )


def check_account(db: Session, account: Account, payload: AccountCheckRequest) -> Account:
    platform_code, _ = account_key_from_state_path(account.state_path)
    driver = _get_driver(platform_code)
    abs_state_path = get_data_dir() / account.state_path

    if payload.use_browser and abs_state_path.exists():
        from server.app.modules.accounts.browser import (
            release_profile_lock,
            try_acquire_profile_lock,
        )

        profile_key = profile_key_from_state_path(account.state_path)
        if not try_acquire_profile_lock(
            profile_key,
            owner_kind="account_check",
            owner_id=account.id,
            queue_reason="账号正在执行发布或登录操作，无法同时检查授权状态",
        ):
            raise ClientError("账号正在执行发布或登录操作，请稍后再检查授权状态")
        try:
            logged_in = _run_in_plain_thread(
                lambda: _check_account_in_browser(driver, abs_state_path, payload)
            )
        finally:
            release_profile_lock(profile_key, owner_kind="account_check", owner_id=account.id)
    else:
        logged_in = abs_state_path.exists()

    now = utcnow()
    account.status = "valid" if logged_in else "expired"
    account.last_checked_at = now
    account.updated_at = now
    db.flush()
    return get_account(db, account.id) or account


def _check_account_in_browser(driver, abs_state_path: Path, payload: AccountCheckRequest) -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_options = launch_options(payload.channel, payload.executable_path)
        viewport = browser_options.pop("viewport", None)
        browser_options["headless"] = True
        browser = pw.chromium.launch(**browser_options)
        context = browser.new_context(storage_state=str(abs_state_path), viewport=viewport)
        page = context.new_page()
        try:
            page.goto(driver.home_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            url = page.url
            title = page.title()
            try:
                body = page.locator("body").inner_text(timeout=3000)
            except Exception:
                body = ""
            logged_in = driver.detect_logged_in(url=url, title=title, body=body)
            context.storage_state(path=str(abs_state_path))
            return logged_in
        finally:
            context.close()
            browser.close()


def relogin_account(db: Session, account: Account, payload: AccountCheckRequest) -> Account:
    platform_code, account_key = account_key_from_state_path(account.state_path)
    request = PlatformLoginRequest(
        display_name=account.display_name,
        account_key=account_key,
        channel=payload.channel,
        executable_path=payload.executable_path,
        use_browser=False,
        note=account.note,
    )
    return register_account_from_storage_state(db, account.user_id, platform_code, request)


def export_accounts_auth_package(db: Session, payload: AccountExportRequest) -> Path:
    ensure_data_dirs()
    accounts = _accounts_for_export(db, payload.account_ids)
    if not accounts:
        raise ClientError("No accounts to export")

    now = utcnow()
    export_path = _new_export_path(now)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "app_version": get_settings().app_version,
        "exported_at": now.isoformat(),
        "excluded_scopes": ["articles", "assets", "publish_tasks", "task_logs", "database"],
        "accounts": [],
    }

    with zipfile.ZipFile(export_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for account in accounts:
            account_dir = f"accounts/{account.platform.code}-{account.id}"
            account_payload = _account_export_payload(account)
            exported_files: list[str] = []
            archive.writestr(
                f"{account_dir}/account.json",
                json.dumps(account_payload, ensure_ascii=False, indent=2),
            )
            exported_files.append(f"{account_dir}/account.json")

            try:
                state_file = _resolve_data_file(account.state_path)
                state_archive_path = f"{account_dir}/storage_state.json"
                archive.write(state_file, state_archive_path)
                exported_files.append(state_archive_path)
            except ClientError:
                _logger.warning(
                    "Skipping storage_state.json for account %s - file not found",
                    account.display_name,
                )

            manifest["accounts"].append({**account_payload, "exported_files": exported_files})

        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return export_path


def _assess_imported_status(state_path: Path) -> str:
    """解析 storage_state.json，评估 cookies 有效性。

    返回：
      "valid"   — cookies 非空，且至少有一个 session cookie 或未来过期的 cookie
      "expired" — cookies 为空，或全部已过期
      "unknown" — 文件无法解析
    """
    import time as _time

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return "unknown"

    cookies = data.get("cookies") or []
    if not cookies:
        return "expired"

    now = _time.time()
    for cookie in cookies:
        expires = cookie.get("expires", -1)
        if expires == -1 or expires > now:  # session cookie 或未过期
            return "valid"

    return "expired"


def import_accounts_auth_package(
    db: Session, user_id: int, zip_bytes: bytes
) -> dict[str, list[str]]:
    ensure_data_dirs()
    imported: list[str] = []
    skipped: list[str] = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except Exception as exc:
            raise ClientError("无效的授权包：无法读取 manifest.json") from exc

        for entry in manifest.get("accounts", []):
            state_path_rel: str = entry.get("state_path", "")
            display_name: str = entry.get("display_name", "未知账号")

            if not state_path_rel:
                skipped.append(f"{display_name}（缺少 state_path）")
                continue

            try:
                platform_code, account_key = account_key_from_state_path(state_path_rel)
            except ClientError:
                skipped.append(f"{display_name}（state_path 格式无效）")
                continue
            dest = state_path_for_key(platform_code, account_key, user_id=user_id)
            new_state_path_rel = relative_to_data_dir(dest)

            existing = db.execute(
                select(Account).where(
                    Account.user_id == user_id, Account.state_path == new_state_path_rel
                )
            ).scalar_one_or_none()
            if existing is not None and not existing.is_deleted:
                skipped.append(display_name)
                continue

            account_dir_in_zip = (
                f"accounts/{entry.get('platform_code', platform_code)}-{entry['id']}"
            )
            archive_state_path = f"{account_dir_in_zip}/storage_state.json"
            if archive_state_path not in archive.namelist():
                skipped.append(f"{display_name}（ZIP 中缺少 storage_state.json）")
                continue

            if not dest.resolve().is_relative_to(get_data_dir().resolve()):
                raise ClientError(f"ZIP entry path escapes data directory: {state_path_rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(archive_state_path))

            platform = get_or_create_platform(
                db,
                platform_code,
                entry.get("platform_name") or platform_code,
                entry.get("platform_base_url"),
            )
            now = utcnow()
            last_login_raw = entry.get("last_login_at")
            try:
                last_login_at = datetime.fromisoformat(last_login_raw) if last_login_raw else None
            except ValueError:
                skipped.append(f"{display_name}（last_login_at 格式无效）")
                continue
            imported_status = _assess_imported_status(dest)
            if existing is None:
                account = Account(
                    user_id=user_id,
                    platform=platform,
                    display_name=display_name,
                    platform_user_id=entry.get("platform_user_id"),
                    status=imported_status,
                    state_path=new_state_path_rel,
                    note=entry.get("note"),
                    last_login_at=last_login_at,
                    last_checked_at=now,
                )
                db.add(account)
            else:
                existing.user_id = user_id
                existing.platform = platform
                existing.display_name = display_name
                existing.platform_user_id = entry.get("platform_user_id")
                existing.status = imported_status
                existing.note = entry.get("note")
                existing.last_login_at = last_login_at
                existing.last_checked_at = now
                existing.is_deleted = False
                existing.deleted_at = None
                existing.updated_at = now
            imported.append(display_name)

    db.flush()
    return {"imported": imported, "skipped": skipped}


def _new_export_path(now) -> Path:
    filename = f"geo-auth-export-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.zip"
    export_dir = get_data_dir() / "exports"
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        probe = export_dir / f".write-probe-{uuid.uuid4().hex}.tmp"
        with probe.open("xb"):
            pass
        probe.unlink(missing_ok=True)
        return export_dir / filename
    except OSError:
        fallback_dir = Path(tempfile.gettempdir()) / "geo-collab-exports"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        return fallback_dir / filename


def _accounts_for_export(db: Session, account_ids: list[int] | None) -> list[Account]:
    from sqlalchemy.orm import selectinload

    stmt = select(Account).options(selectinload(Account.platform))
    if account_ids:
        unique_ids = sorted(set(account_ids))
        stmt = stmt.where(Account.id.in_(unique_ids))
    else:
        unique_ids = []
    accounts = list(db.execute(stmt.order_by(Account.id.asc())).scalars().all())
    if unique_ids:
        found_ids = {account.id for account in accounts}
        missing_ids = [account_id for account_id in unique_ids if account_id not in found_ids]
        if missing_ids:
            raise ClientError(
                f"Accounts not found: {', '.join(str(account_id) for account_id in missing_ids)}"
            )
    return accounts


def _resolve_data_file(relative_path: str) -> Path:
    data_dir = get_data_dir().resolve()
    path = (data_dir / relative_path).resolve()
    if not path.is_relative_to(data_dir) or not path.is_file():
        raise ClientError(f"Account state file not found: {relative_path}")
    return path


def _account_export_payload(account: Account) -> dict[str, Any]:
    return {
        "id": account.id,
        "platform_code": account.platform.code,
        "platform_name": account.platform.name,
        "platform_base_url": account.platform.base_url,
        "display_name": account.display_name,
        "platform_user_id": account.platform_user_id,
        "status": account.status,
        "state_path": account.state_path,
        "last_checked_at": account.last_checked_at.isoformat() if account.last_checked_at else None,
        "last_login_at": account.last_login_at.isoformat() if account.last_login_at else None,
        "note": account.note,
        "created_at": account.created_at.isoformat(),
        "updated_at": account.updated_at.isoformat(),
    }
