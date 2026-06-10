"""账号登录态获取与导入导出。

交互式登录走 worker 驱动的命令/状态机：API 端写一行 account_login_sessions
（status=pending）并立即返回 request_id，worker 轮询 process_account_login_session_requests
认领、拉起远程浏览器、推进状态（pending/queued → starting → active →
finish_requested/cancel_requested → finished/cancelled/failed）；API 端的
finish/stop 只改状态并轮询等 worker 落地。所有 Playwright sync API 调用都包在
_run_in_plain_thread 里，避开 FastAPI/AnyIO 的事件循环。
"""

from __future__ import annotations

import io
import json
import logging
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
    """在 FastAPI/AnyIO worker 线程之外运行 Playwright 同步 API 工作。"""
    result: list[Any] = []
    error: list[tuple[type[BaseException], BaseException, Any]] = []

    def _target() -> None:
        # Python 3.10+ 会通过 threading.Thread 把父线程的 contextvars Context
        # 复制到新线程。Python 3.13+ 的 asyncio 把 _running_loop 存成 ContextVar，
        # 因而新线程会继承主事件循环并触发 Playwright 同步 API 的保护逻辑。
        # 在任何 Playwright 调用前先重置它。
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
    """从磁盘上已有的 storage_state.json 登记 / 更新一个账号（不开浏览器）。

    upsert：按 (user_id, platform_id, state_path) 命中则复活并刷新，否则新建，状态直接置 valid。
    use_browser=True 属于交互登录，必须改走 start_login_session。
    """
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
    # 同时匹配新的按用户隔离路径与旧的无用户层路径，避免旧账号被当成新账号重复建
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
    """发起新平台的交互式登录：upsert 账号（置 unknown）并下一条 worker 登录命令，立即返回。

    不等浏览器起来——只回 request_id（=session_id）与 status=pending，后续靠 worker 推进、
    前端轮询 /status。previous_status 透传给 worker，登录被取消时用于回滚账号状态。
    """
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
    """旧布局（无用户层）的登录态目录整体复制到新的按用户隔离目录，做一次性数据迁移。"""
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
    """对已有账号重新登录（如登录态失效）：置 unknown 并下 worker 登录命令，立即返回 pending。"""
    if account.state_path is None:
        raise ClientError("API 接入账号无法进行浏览器登录")
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
    """完成登录：保存登录态并据此刷新账号 valid/expired。

    有 worker 命令行就走 worker 路径（改状态 + 轮询等 finished）；否则按本地路径直接收尾
    （API 与 worker 同进程的开发场景）。
    """
    request = _find_account_login_request(db, account.id, session_id)
    if request is not None:
        return _finish_login_browser_via_worker(db, account, request)

    if account.state_path is None:
        raise ClientError("API 接入账号无法进行浏览器登录")
    platform_code, account_key = account_key_from_state_path(account.state_path)
    result = _finish_login_browser_local(platform_code, account_key, account.state_path, session_id)
    _apply_login_result(account, result)
    db.flush()
    return get_account(db, account.id) or account, result


def stop_account_login_session(db: Session, account: Account, session_id: str) -> None:
    """中止登录会话：有 worker 命令行则请求取消，否则本地直接停掉远程浏览器。"""
    request = _find_account_login_request(db, account.id, session_id)
    if request is not None:
        _cancel_login_browser_via_worker(db, request)
        return
    if account.state_path is None:
        raise ClientError("API 接入账号无法进行浏览器登录")
    _, account_key = account_key_from_state_path(account.state_path)
    _stop_login_browser_local(account_key, session_id)


def _new_login_session_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _touch_login_request(request: AccountLoginSession) -> None:
    request.updated_at = utcnow()


def _find_account_login_request(
    db: Session, account_id: int, session_id: str
) -> AccountLoginSession | None:
    # session_id 可能是登录命令行 id，也可能是底层 browser_session_id：两者都匹配，取最新一条
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
    """返回用于状态轮询的 AccountLoginSession 记录。"""
    return _find_account_login_request(db, account.id, session_id)


def _wait_for_account_login_request(
    db: Session,
    request_id: str,
    desired_statuses: set[str],
    timeout_seconds: float,
    timeout_message: str,
) -> AccountLoginSession:
    """轮询登录命令行直到进入 desired_statuses；遇 failed / 其它终态 / 超时则抛 ClientError。

    每轮先 rollback + expire_all，强制下次读重新查库，才能看到 worker 进程刚 commit 的状态变更。
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        # 丢弃本事务快照 + 失效已加载对象，否则读到的是缓存、看不到 worker 的提交
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
    """创建登录会话请求行，并立即返回请求 ID。"""
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
    """为当前 worker 抢占并处理一条账号登录浏览器命令。

    Worker 轮询入口：认领并处理一条登录命令，处理了返回 True，无活儿返回 False。
    认领两类命令：(a) 未被占用的 pending/queued 新登录；(b) 本 worker 名下待 finish/cancel 的命令。
    认领靠条件 UPDATE：只有把 status 从旧值推进到 *ing 且 rowcount==1 才算抢到（跨 worker 去重）。
    新登录在推进前先抢 profile 锁，抢不到则置 queued 让出。
    """
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

    # claim：条件 UPDATE（旧状态 + worker 归属都进 where），rowcount==1 才算抢到、跨 worker 去重
    result = db.execute(
        sa_update(AccountLoginSession)
        .where(*where_clause)
        .values(status=next_status, worker_id=worker_id, queue_reason=None, updated_at=utcnow())
    )
    rows = result.rowcount  # type: ignore[attr-defined]  # DML 执行返回 CursorResult
    db.commit()
    if rows == 0:
        return False  # 被别的 worker 抢先了

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
    """登录前抢账号 profile 锁。抢到返回 True；被发布 / 别的登录占用则置 queued 让出返回 False。

    与发布、check_account 共用同一把 profile 锁，保证同一 Chromium profile 目录串行使用。
    """
    from server.app.modules.accounts.browser import try_acquire_profile_lock

    account = db.get(Account, request.account_id)
    if account is None:
        request.status = LOGIN_STATUS_FAILED
        request.error_message = "Account not found"
        request.queue_reason = None
        _touch_login_request(request)
        db.commit()
        return False
    if account.state_path is None:
        request.status = LOGIN_STATUS_FAILED
        request.error_message = "API 接入账号无法进行浏览器登录"
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
    """Worker：拉起远程浏览器并打开登录页，成功置 active 并记下 browser_session_id/novnc_url。

    失败置 failed 并立刻释放 profile 锁（active 时不释放——浏览器还开着，要留到 finish/cancel）。
    """
    try:
        account = db.get(Account, request.account_id)
        if account is None:
            raise ClientError("Account not found")
        if account.state_path is None:
            raise ClientError("API 接入账号无法进行浏览器登录")
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
        # active 已落库（前端尽快看到可接管），再 best-effort 把登录页 goto 过去
        from server.app.modules.accounts.login_broker import login_broker

        login_broker.load_login_page(session.id, _get_driver(platform_code).home_url)
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
    """Worker：读回登录态存盘、据此刷新账号 valid/expired、置 finished；finally 释放 profile 锁。"""
    account = db.get(Account, request.account_id)
    if account is None:
        request.status = LOGIN_STATUS_FAILED
        request.error_message = "Account not found"
        _touch_login_request(request)
        db.commit()
        return
    if account.state_path is None:
        request.status = LOGIN_STATUS_FAILED
        request.error_message = "API 接入账号无法进行浏览器登录"
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
    """Worker：停掉浏览器、置 cancelled，并把账号状态从 unknown 回滚到登录前的 previous_status。"""
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
    if account is None or account.state_path is None:
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
    return _finish_login_browser_impl(platform_code, account_key, state_path, session_id)


def _finish_login_browser_impl(
    platform_code: str, account_key: str, state_path: str, session_id: str
) -> BrowserCheckResult:
    from server.app.modules.accounts.browser import get_session, stop_remote_browser_session
    from server.app.modules.accounts.login_broker import login_broker

    session = get_session(session_id)
    if session is None:
        raise ClientError(f"Remote browser session not found: {session_id}")
    if session.account_key != account_key:
        raise ClientError("Remote browser session does not belong to this account")
    if not login_broker.owns(session_id):
        raise ClientError("Remote browser session has no browser context")

    driver = _get_driver(platform_code)
    try:
        result = login_broker.read_login_state(
            session_id,
            detect=lambda url, title, body: driver.detect_logged_in(
                url=url, title=title, body=body
            ),
            state_path=get_data_dir() / state_path,
        )
        return BrowserCheckResult(logged_in=result.logged_in, url=result.url, title=result.title)
    finally:
        stop_remote_browser_session(session_id)


def _stop_login_browser_local(account_key: str, session_id: str) -> None:
    _stop_login_browser_impl(account_key, session_id)


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
    return _start_login_browser_impl(
        platform_code, account_key, state_path, channel, executable_path
    )


def _start_login_browser_impl(
    platform_code: str,
    account_key: str,
    state_path: str,
    channel: str,
    executable_path: str | None,
    load_login_page: bool = True,
):
    """起远程会话（Xvfb→x11vnc→websockify），再交给 login_broker 用 async Playwright 打开持久化
    context。浏览器句柄由 broker 在它自己的事件循环上持有并保活，一直开着等用户在 noVNC 里扫码，
    直到 finish/cancel。多个账号并发登录互不干扰（同步 Playwright 单线程那套并发限制不再适用）。
    失败时让 broker 拆掉浏览器并停掉远程会话。
    """
    from server.app.modules.accounts.browser import (
        keep_session_alive,
        start_remote_browser_session,
        stop_remote_browser_session,
    )
    from server.app.modules.accounts.login_broker import login_broker

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
    try:
        options = launch_options(channel, executable_path)
        clear_profile_locks(profile_dir)
        login_broker.launch_login_browser(
            session.id, profile_dir=profile_dir, options=options, display=session.display
        )
        keep_session_alive(session.id)
        if load_login_page:
            login_broker.load_login_page(session.id, driver.home_url)
        return session
    except Exception:
        login_broker.close_if_owned(session.id)
        stop_remote_browser_session(session.id)
        raise


def check_account(db: Session, account: Account, payload: AccountCheckRequest) -> Account:
    """检查账号登录态是否仍有效，刷新 status 为 valid/expired。

    use_browser 时无头开浏览器载入登录态、由 driver.detect_logged_in 判定，并抢同一把 profile 锁
    （和发布 / 登录互斥，抢不到直接 ClientError，不排队）；否则仅按 storage_state 文件是否存在粗判。
    """
    if account.state_path is None:
        raise ClientError("API 接入账号无法检查浏览器登录态")
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
    """从磁盘已有登录态重新登记账号（use_browser=False），不开浏览器、复用现有 state 文件。"""
    if account.state_path is None:
        raise ClientError("API 接入账号无法重新登记")
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
    """把账号元数据 + 各自的 storage_state.json 打包成授权 ZIP，返回落盘路径。

    只导出账号与登录态（manifest 里列了显式排除的 articles/assets/publish_tasks 等）；
    某账号 state 文件缺失只跳过不致命。account_ids 为空表示导出全部。
    """
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

            if account.state_path:
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
            else:
                _logger.warning(
                    "Skipping storage_state.json for account %s - no browser state path",
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
    """导入授权 ZIP：逐账号落 storage_state 文件并 upsert 到当前 user_id 名下。

    已存在（同 state_path 且未软删）的跳过；格式 / 路径异常的逐条 skip 不中断整包。
    写盘前用 is_relative_to 校验目标在 data 目录内，防 ZIP 路径穿越逃逸。
    返回 {"imported": [...], "skipped": [...]}（按 display_name 记账）。
    """
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

            # 防 ZIP 路径穿越：解析后必须仍在 data 目录内，否则可能写到任意路径
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
    """生成唯一导出文件名；data/exports 不可写（探针写失败）时退回系统临时目录。"""
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
    """把相对路径解析为 data 目录下的真实文件，越界或不存在则抛 ClientError（防路径穿越）。"""
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
