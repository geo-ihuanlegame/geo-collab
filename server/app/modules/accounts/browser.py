"""
Linux 服务端部署的远程浏览器会话管理。

运行链路是 Xvfb -> x11vnc -> websockify -> noVNC，Playwright Chromium 挂到
X display 上。系统部署在 Linux 服务器上，因此本模块不做平台分支。

会话状态会镜像到 DB 的 browser_sessions 表，让 API 进程可以读取 novnc_url，
并跨进程请求关闭会话。进程内字典（_active_sessions 等）只属于当前 worker，
保存实时句柄。
"""

from __future__ import annotations

import logging
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import delete as sa_delete
from sqlalchemy import text as sa_text
from sqlalchemy import update as sa_update

from server.app.core.config import get_settings
from server.app.core.paths import get_data_dir
from server.app.core.time import utcnow

_logger = logging.getLogger(__name__)
PROFILE_LOCK_LEASE_SECONDS = 900


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen
    log_handle: BinaryIO


@dataclass
class RemoteBrowserSession:
    id: str
    account_key: str
    display_number: int
    display: str
    vnc_port: int
    novnc_port: int
    novnc_url: str
    log_dir: Path
    platform_code: str = ""
    profile_key: str | None = None
    processes: list[ManagedProcess] = field(default_factory=list, repr=False)
    playwright: Any | None = field(default=None, repr=False)
    browser_context: Any | None = field(default=None, repr=False)
    page: Any | None = field(default=None, repr=False)
    context_thread_id: int | None = field(default=None, repr=False)
    operation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    started_at: float = field(default_factory=time.monotonic)


_sessions_lock = threading.Lock()
_active_sessions: dict[str, RemoteBrowserSession] = {}
_reserved_displays: set[int] = set()
_reserved_vnc_ports: set[int] = set()
_reserved_novnc_ports: set[int] = set()

_record_to_session: dict[int, str] = {}
_session_keep_alive: set[str] = set()
_account_sessions: dict[str, str] = {}
_account_creation_locks: dict[str, threading.Lock] = {}
_account_creation_locks_lock = threading.Lock()

_idle_cleanup_thread: threading.Thread | None = None
_idle_cleanup_stop = threading.Event()


# ── 数据库辅助函数 ──────────────────────────────────────────────────────────


def _get_db():
    from server.app.db.session import SessionLocal

    return SessionLocal()


def try_acquire_profile_lock(
    profile_key: str,
    *,
    owner_kind: str,
    owner_id: str | int,
    queue_reason: str | None = None,
    lease_seconds: int = PROFILE_LOCK_LEASE_SECONDS,
) -> bool:
    """为一个持久化 Chromium profile 抢跨进程锁。

    抢到返回 True，被别人占用返回 False。幂等可重入：同一 (owner_kind, owner_id)
    再次调用视为续租而非冲突。通过 INSERT ... ON DUPLICATE KEY 让插入不报错，
    再回读行主判断 owner 归属。任何异常都吞掉返回 False（宁可排队等待，也不误判抢到锁）。
    """
    from server.app.modules.accounts.models import BrowserProfileLock

    owner_id = str(owner_id)
    worker_id = _worker_id()
    now = utcnow()
    lease_until = now + timedelta(seconds=lease_seconds)
    db = _get_db()
    try:
        # 先清掉已过期的租约：owner 崩溃后留下的死锁靠这一步被新请求接管
        db.execute(
            sa_delete(BrowserProfileLock).where(
                BrowserProfileLock.profile_key == profile_key,
                BrowserProfileLock.lease_until < now,
            )
        )
        db.execute(
            sa_text(
                """
                INSERT INTO browser_profile_locks
                    (profile_key, owner_kind, owner_id, worker_id, queue_reason, acquired_at, heartbeat_at, lease_until)
                VALUES
                    (:profile_key, :owner_kind, :owner_id, :worker_id, :queue_reason, :now, :now, :lease_until)
                ON DUPLICATE KEY UPDATE profile_key = profile_key
                """
            ),
            {
                "profile_key": profile_key,
                "owner_kind": owner_kind,
                "owner_id": owner_id,
                "worker_id": worker_id,
                "queue_reason": queue_reason,
                "now": now,
                "lease_until": lease_until,
            },
        )
        db.commit()

        # 回读这把锁：owner 是自己才算抢到（INSERT 可能撞上别人的既有行而被 ON DUPLICATE 忽略）
        lock = db.get(BrowserProfileLock, profile_key)
        if lock is None:
            return False
        if lock.owner_kind == owner_kind and lock.owner_id == owner_id:
            lock.worker_id = worker_id
            lock.queue_reason = queue_reason
            lock.heartbeat_at = now
            lock.lease_until = lease_until
            db.commit()
            return True
        return False
    except Exception:
        db.rollback()
        _logger.warning("Failed to acquire browser profile lock for %s", profile_key, exc_info=True)
        return False
    finally:
        db.close()


def heartbeat_profile_lock(
    profile_key: str,
    *,
    owner_kind: str,
    owner_id: str | int,
    lease_seconds: int = PROFILE_LOCK_LEASE_SECONDS,
) -> None:
    """续租 profile 锁：长操作（发布 / 登录）期间周期调用，把 lease_until 往后推，防被当过期回收。"""
    from server.app.modules.accounts.models import BrowserProfileLock

    now = utcnow()
    lease_until = now + timedelta(seconds=lease_seconds)
    db = _get_db()
    try:
        db.execute(
            sa_update(BrowserProfileLock)
            .where(
                BrowserProfileLock.profile_key == profile_key,
                BrowserProfileLock.owner_kind == owner_kind,
                BrowserProfileLock.owner_id == str(owner_id),
            )
            .values(heartbeat_at=now, lease_until=lease_until, worker_id=_worker_id())
        )
        db.commit()
    finally:
        db.close()


def release_profile_lock(profile_key: str, *, owner_kind: str, owner_id: str | int) -> None:
    from server.app.modules.accounts.models import BrowserProfileLock

    db = _get_db()
    try:
        db.execute(
            sa_delete(BrowserProfileLock).where(
                BrowserProfileLock.profile_key == profile_key,
                BrowserProfileLock.owner_kind == owner_kind,
                BrowserProfileLock.owner_id == str(owner_id),
            )
        )
        db.commit()
    finally:
        db.close()


def release_profile_lock_by_owner(*, owner_kind: str, owner_id: str | int) -> None:
    from server.app.modules.accounts.models import BrowserProfileLock

    db = _get_db()
    try:
        db.execute(
            sa_delete(BrowserProfileLock).where(
                BrowserProfileLock.owner_kind == owner_kind,
                BrowserProfileLock.owner_id == str(owner_id),
            )
        )
        db.commit()
    finally:
        db.close()


def _worker_id() -> str | None:
    import os as _os

    return _os.environ.get("GEO_WORKER_ID")


def _write_session_to_db(session: RemoteBrowserSession, worker_id: str | None) -> None:
    # 把会话镜像进 browser_sessions 表，让 API 进程能读到 novnc_url、发停止请求。
    # 写失败只记日志、不抛——本地句柄仍可用，只是丢了跨进程可见性。
    try:
        from server.app.core.time import utcnow
        from server.app.modules.accounts.models import BrowserSession

        db = _get_db()
        try:
            now = utcnow()
            db.merge(
                BrowserSession(
                    id=session.id,
                    platform_code=session.platform_code,
                    account_key=session.account_key,
                    profile_key=session.profile_key,
                    display=session.display,
                    novnc_url=session.novnc_url,
                    started_at=now,
                    last_activity_at=now,
                    worker_id=worker_id,
                    keep_alive=False,
                    stop_requested=False,
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        _logger.error(
            "Could not write session %s to DB — cross-process visibility lost",
            session.id,
            exc_info=True,
        )


def _delete_session_from_db(session_id: str) -> None:
    try:
        from sqlalchemy import delete as sa_delete

        from server.app.modules.accounts.models import BrowserSession, RecordBrowserSession

        db = _get_db()
        try:
            db.execute(
                sa_delete(RecordBrowserSession).where(RecordBrowserSession.session_id == session_id)
            )
            db.execute(sa_delete(BrowserSession).where(BrowserSession.id == session_id))
            db.commit()
        finally:
            db.close()
    except Exception:
        _logger.debug("Could not delete session %s from DB", session_id, exc_info=True)


def _set_stop_requested_db(session_id: str) -> None:
    try:
        from sqlalchemy import update as sa_update

        from server.app.modules.accounts.models import BrowserSession

        db = _get_db()
        try:
            db.execute(
                sa_update(BrowserSession)
                .where(BrowserSession.id == session_id)
                .values(stop_requested=True)
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        _logger.debug("Could not set stop_requested for session %s", session_id, exc_info=True)


def _update_keep_alive_db(session_id: str, keep_alive: bool) -> None:
    try:
        from sqlalchemy import update as sa_update

        from server.app.core.time import utcnow
        from server.app.modules.accounts.models import BrowserSession

        db = _get_db()
        try:
            db.execute(
                sa_update(BrowserSession)
                .where(BrowserSession.id == session_id)
                .values(keep_alive=keep_alive, last_activity_at=utcnow())
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        _logger.debug("Could not update keep_alive for session %s", session_id, exc_info=True)


def _write_record_session_to_db(record_id: int, session_id: str) -> None:
    try:
        from server.app.modules.accounts.models import RecordBrowserSession

        db = _get_db()
        try:
            db.merge(RecordBrowserSession(record_id=record_id, session_id=session_id))
            db.commit()
        finally:
            db.close()
    except Exception:
        _logger.debug(
            "Could not write record→session mapping %d→%s to DB",
            record_id,
            session_id,
            exc_info=True,
        )


def _delete_record_session_from_db(record_id: int) -> None:
    try:
        from sqlalchemy import delete as sa_delete

        from server.app.modules.accounts.models import RecordBrowserSession

        db = _get_db()
        try:
            db.execute(
                sa_delete(RecordBrowserSession).where(RecordBrowserSession.record_id == record_id)
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        _logger.debug(
            "Could not delete record session mapping for record %d", record_id, exc_info=True
        )


def _query_stop_requested_session_ids() -> list[str]:
    try:
        from sqlalchemy import select

        from server.app.modules.accounts.models import BrowserSession

        db = _get_db()
        try:
            rows = (
                db.execute(select(BrowserSession.id).where(BrowserSession.stop_requested == True))  # noqa: E712
                .scalars()
                .all()
            )
            return list(rows)
        finally:
            db.close()
    except Exception:
        _logger.debug("Could not query stop_requested sessions", exc_info=True)
        return []


# ── 公共 API ────────────────────────────────────────────────────────────────


def associate_record_with_session(record_id: int, session_id: str) -> None:
    """把发布记录关联到远程浏览器会话。"""
    with _sessions_lock:
        _record_to_session[record_id] = session_id
    _write_record_session_to_db(record_id, session_id)


def get_session_for_record(record_id: int) -> Any | None:
    """返回指定发布记录对应的浏览器会话对象；不存在则返回 None。

    这里会查询 DB 以支持跨进程访问。返回 BrowserSession ORM 行（带 .id 和
    .novnc_url），如果当前进程有本地 RemoteBrowserSession，则返回本地对象。
    """
    with _sessions_lock:
        session_id = _record_to_session.get(record_id)
        if session_id is not None:
            local = _active_sessions.get(session_id)
            if local is not None:
                return local

    try:
        from sqlalchemy import select

        from server.app.modules.accounts.models import BrowserSession, RecordBrowserSession

        db = _get_db()
        try:
            rbs = db.execute(
                select(RecordBrowserSession).where(RecordBrowserSession.record_id == record_id)
            ).scalar_one_or_none()
            if rbs is None:
                return None
            bs = db.get(BrowserSession, rbs.session_id)
            if bs is None:
                return None
            from sqlalchemy.orm import make_transient

            # 脱离 session 返回：调用方在 db.close() 之后还要读 .novnc_url，不能让它变 detached
            db.expunge(bs)
            make_transient(bs)
            return bs
        finally:
            db.close()
    except Exception:
        _logger.debug("Could not query session for record %d from DB", record_id, exc_info=True)
        return None


def get_session(session_id: str) -> RemoteBrowserSession | None:
    with _sessions_lock:
        return _active_sessions.get(session_id)


def attach_browser_handles(
    session_id: str,
    playwright: Any | None,
    context: Any | None,
    page: Any | None = None,
    context_thread_id: int | None = None,
) -> None:
    with _sessions_lock:
        session = _active_sessions.get(session_id)
        if session is None:
            raise RuntimeError(f"Remote browser session not found: {session_id}")
        session.playwright = playwright
        session.browser_context = context
        session.page = page
        session.context_thread_id = context_thread_id


def disassociate_record(record_id: int) -> None:
    with _sessions_lock:
        _record_to_session.pop(record_id, None)
    _delete_record_session_from_db(record_id)


def _context_alive(context: object) -> bool:
    try:
        _ = context.pages  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def _account_session_key(
    platform_code: str, account_key: str, profile_key: str | None = None
) -> str:
    return profile_key or f"{platform_code}:{account_key}"


def get_or_create_account_session(
    platform_code: str,
    account_key: str,
    profile_key: str | None = None,
) -> RemoteBrowserSession:
    """返回账号的持久化会话；需要时创建新会话。

    复用条件：会话存在于当前进程、未处于 keep_alive 状态，且 Chromium context
    仍然存活。

    按账号加创建锁，避免两个并发调用都看到无会话，并各自为同一账号拉起一个
    浏览器实例。
    """
    cache_key = _account_session_key(platform_code, account_key, profile_key)

    def _try_reuse() -> RemoteBrowserSession | None:
        with _sessions_lock:
            session_id = _account_sessions.get(cache_key)
            if session_id is None:
                return None
            session = _active_sessions.get(session_id)
            if (
                session is not None
                and session_id not in _session_keep_alive
                and (session.browser_context is None or _context_alive(session.browser_context))
            ):
                session.started_at = time.monotonic()
                return session
            _account_sessions.pop(cache_key, None)
            return None

    if (existing := _try_reuse()) is not None:
        return existing

    with _account_creation_locks_lock:
        if cache_key not in _account_creation_locks:
            _account_creation_locks[cache_key] = threading.Lock()
        account_lock = _account_creation_locks[cache_key]

    with account_lock:
        # 双重检查：拿到 per-account 锁后再 _try_reuse 一次，可能已被先到的并发者建好了
        if (existing := _try_reuse()) is not None:
            return existing

        session = start_remote_browser_session(
            account_key, platform_code=platform_code, profile_key=profile_key
        )
        with _sessions_lock:
            _account_sessions[cache_key] = session.id
        return session


def keep_session_alive(session_id: str) -> None:
    with _sessions_lock:
        _session_keep_alive.add(session_id)
    _update_keep_alive_db(session_id, keep_alive=True)


def active_remote_browser_sessions() -> list[RemoteBrowserSession]:
    with _sessions_lock:
        return list(_active_sessions.values())


def remote_browser_runtime_status() -> dict[str, object]:
    settings = get_settings()
    required = {
        "xvfb": _resolve_command(settings.publish_xvfb_path),
        "x11vnc": _resolve_command(settings.publish_x11vnc_path),
        "websockify": _resolve_command(settings.publish_websockify_path),
    }
    novnc_web_dir = settings.publish_novnc_web_dir
    novnc_web_ready = True
    if novnc_web_dir:
        novnc_web_ready = Path(novnc_web_dir).exists()
    return {
        "enabled": True,
        "ready": all(required.values()) and novnc_web_ready,
        "active_sessions": len(active_remote_browser_sessions()),
        "tools": {name: bool(path) for name, path in required.items()},
        "novnc_web_ready": novnc_web_ready,
    }


@contextmanager
def managed_remote_browser_session(account_key: str) -> Iterator[RemoteBrowserSession | None]:
    """上下文管理器：进入时启动远程浏览器会话，退出时停止。

    如果调用过 keep_session_alive()，例如 waiting_user_input 场景，则不会停止。
    """
    session = start_remote_browser_session(account_key)
    try:
        yield session
    finally:
        with _sessions_lock:
            keep = session.id not in _session_keep_alive
        if keep:
            stop_remote_browser_session(session.id)


def start_remote_browser_session(
    account_key: str,
    platform_code: str = "",
    profile_key: str | None = None,
) -> RemoteBrowserSession:
    """拉起一条 Xvfb → x11vnc → websockify 进程链，返回带 novnc_url 的会话句柄。

    每步拉起进程后都等对应 X display / 端口就绪才继续。任一步失败则杀掉已起进程、
    归还预留的 display/端口号再抛出（避免泄漏可复用的号段）。成功后注册进 _active_sessions
    并镜像到 DB，按需启动空闲清理线程。
    """
    import os as _os

    worker_id = _os.environ.get("GEO_WORKER_ID")

    settings = get_settings()

    xvfb = _require_command(settings.publish_xvfb_path, "Xvfb")
    x11vnc = _require_command(settings.publish_x11vnc_path, "x11vnc")
    websockify = _require_command(settings.publish_websockify_path, "websockify")
    if settings.publish_novnc_web_dir and not Path(settings.publish_novnc_web_dir).exists():
        raise RuntimeError(f"noVNC web dir not found: {settings.publish_novnc_web_dir}")

    display_number, vnc_port, novnc_port = _reserve_numbers()
    safe_account_key = re.sub(r"[^a-zA-Z0-9_-]+", "-", account_key).strip("-") or "account"
    session_id = uuid.uuid4().hex[:12]
    log_dir = get_data_dir() / "logs" / "browser-sessions" / f"{safe_account_key}-{session_id}"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        _release_reserved_numbers(display_number, vnc_port, novnc_port)
        raise

    session = RemoteBrowserSession(
        id=session_id,
        platform_code=platform_code,
        account_key=account_key,
        display_number=display_number,
        display=f":{display_number}",
        vnc_port=vnc_port,
        novnc_port=novnc_port,
        novnc_url=_novnc_url(settings.publish_remote_browser_host, novnc_port),
        log_dir=log_dir,
        profile_key=profile_key,
    )

    try:
        session.processes.append(
            _spawn(
                "xvfb",
                [
                    xvfb,
                    session.display,
                    "-screen",
                    "0",
                    "1440x900x24",
                    "-ac",
                    "+extension",
                    "GLX",
                    "+render",
                    "-noreset",
                ],
                log_dir,
            )
        )
        _wait_for_x_display(
            session.display_number, settings.publish_remote_browser_start_timeout_seconds
        )

        session.processes.append(
            _spawn(
                "x11vnc",
                [
                    x11vnc,
                    "-display",
                    session.display,
                    "-localhost",
                    "-forever",
                    "-shared",
                    "-nopw",
                    "-rfbport",
                    str(session.vnc_port),
                ],
                log_dir,
            )
        )
        _wait_for_port(
            "127.0.0.1", session.vnc_port, settings.publish_remote_browser_start_timeout_seconds
        )

        websockify_command = [websockify]
        if settings.publish_novnc_web_dir:
            websockify_command.append(f"--web={settings.publish_novnc_web_dir}")
        websockify_command.extend(
            [
                f"{settings.publish_remote_browser_host}:{session.novnc_port}",
                f"127.0.0.1:{session.vnc_port}",
            ]
        )
        session.processes.append(_spawn("websockify", websockify_command, log_dir))
        _wait_for_port(
            settings.publish_remote_browser_host,
            session.novnc_port,
            settings.publish_remote_browser_start_timeout_seconds,
        )

        with _sessions_lock:
            _active_sessions[session.id] = session
            _reserved_displays.discard(session.display_number)
            _reserved_vnc_ports.discard(session.vnc_port)
            _reserved_novnc_ports.discard(session.novnc_port)

        _write_session_to_db(session, worker_id)
        _start_idle_cleanup()
        return session
    except Exception:
        _stop_session_processes(session)
        _release_reserved_numbers(display_number, vnc_port, novnc_port)
        raise


def stop_remote_browser_session(session_id: str) -> None:
    """停止浏览器会话。

    若会话属于当前进程则立即杀掉；否则在 DB 中置 stop_requested=True，由所属
    worker 清理。
    """
    with _sessions_lock:
        local_session = _active_sessions.pop(session_id, None)
        _session_keep_alive.discard(session_id)
        stale_records = [rid for rid, sid in _record_to_session.items() if sid == session_id]
        for rid in stale_records:
            _record_to_session.pop(rid, None)
        stale_account_keys = [k for k, v in _account_sessions.items() if v == session_id]
        for k in stale_account_keys:
            _account_sessions.pop(k, None)

    if local_session is not None:
        _close_browser_handles(local_session)
        _stop_session_processes(local_session)
        _delete_session_from_db(session_id)
    else:
        _set_stop_requested_db(session_id)


# ── 端口 / display 分配 ────────────────────────────────────────────────────


def _reserve_numbers() -> tuple[int, int, int]:
    """在锁内挑一组互不冲突的 (display, vnc_port, novnc_port) 并登记到 _reserved_*。

    _reserved_* 集合占着启动中（尚未进 _active_sessions）的号，防并发启动撞号；
    会话起好后由调用方从 _reserved_* 移除，失败则走 _release_reserved_numbers 归还。
    """
    settings = get_settings()
    with _sessions_lock:
        used_displays = {s.display_number for s in _active_sessions.values()} | _reserved_displays
        used_vnc_ports = {s.vnc_port for s in _active_sessions.values()} | _reserved_vnc_ports
        used_novnc_ports = {s.novnc_port for s in _active_sessions.values()} | _reserved_novnc_ports

        display_number = _find_display_number(
            settings.publish_remote_browser_display_base, used_displays
        )
        vnc_port = _find_free_port(
            "127.0.0.1", settings.publish_remote_browser_vnc_base_port, used_vnc_ports
        )
        novnc_port = _find_free_port(
            settings.publish_remote_browser_host,
            settings.publish_remote_browser_novnc_base_port,
            used_novnc_ports,
        )
        _reserved_displays.add(display_number)
        _reserved_vnc_ports.add(vnc_port)
        _reserved_novnc_ports.add(novnc_port)
        return display_number, vnc_port, novnc_port


def _release_reserved_numbers(display_number: int, vnc_port: int, novnc_port: int) -> None:
    with _sessions_lock:
        _reserved_displays.discard(display_number)
        _reserved_vnc_ports.discard(vnc_port)
        _reserved_novnc_ports.discard(novnc_port)


def _find_display_number(base: int, used: set[int]) -> int:
    for display_number in range(base, base + 1000):
        if display_number in used:
            continue
        socket_path = Path(f"/tmp/.X11-unix/X{display_number}")
        if socket_path.exists():
            continue
        return display_number
    raise ValueError("No free X display number available")


def _find_free_port(host: str, base: int, used: set[int]) -> int:
    for port in range(base, base + 1000):
        if port in used:
            continue
        if _port_available(host, port):
            return port
    raise ValueError(f"No free TCP port available from {base}")


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


# ── 进程管理 ────────────────────────────────────────────────────────────────


def _spawn(name: str, command: list[str], log_dir: Path) -> ManagedProcess:
    log_handle = (log_dir / f"{name}.log").open("ab")
    try:
        process = subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT)
    except Exception:
        log_handle.close()
        raise
    return ManagedProcess(name=name, process=process, log_handle=log_handle)


def _stop_session_processes(session: RemoteBrowserSession) -> None:
    for managed in reversed(session.processes):
        process = managed.process
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    time.sleep(0.1)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    _logger.error(
                        "Process %s (PID %d) failed to terminate after SIGKILL — "
                        "display/port may be leaked",
                        managed.name,
                        process.pid,
                    )
        finally:
            try:
                managed.log_handle.close()
            except Exception:
                pass


def _close_browser_handles(session: RemoteBrowserSession) -> None:
    with session.operation_lock:
        if session.browser_context is not None:
            try:
                session.browser_context.close()
            except Exception:
                pass
        if session.playwright is not None:
            try:
                session.playwright.stop()
            except Exception:
                pass
        session.browser_context = None
        session.playwright = None
        session.page = None


# ── X11 / TCP 就绪检查 ─────────────────────────────────────────────────────


def _wait_for_x_display(display_number: int, timeout_seconds: float) -> None:
    socket_path = Path(f"/tmp/.X11-unix/X{display_number}")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        time.sleep(0.1)
    raise ValueError(f"Xvfb display did not become ready: :{display_number}")


def _wait_for_port(host: str, port: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                sock.connect((host, port))
                return
            except OSError:
                time.sleep(0.1)
    raise ValueError(f"Port did not become ready: {host}:{port}")


def _require_command(command: str, label: str) -> str:
    resolved = _resolve_command(command)
    if not resolved:
        raise ValueError(f"{label} command not found: {command}")
    return resolved


def _resolve_command(command: str | None) -> str | None:
    if not command:
        return None
    path = Path(command)
    if path.is_absolute():
        return str(path) if path.exists() else None
    return shutil.which(command)


# ── 空闲 / 停止请求清理线程 ────────────────────────────────────────────────


def _start_idle_cleanup() -> None:
    """惰性启动后台清理线程（已在跑则直接返回）。

    每 2s 处理一轮 stop_requested（跨进程停止指令），每约 30s 再扫一次空闲超时 / 僵尸会话。
    """
    global _idle_cleanup_thread
    if _idle_cleanup_thread is not None and _idle_cleanup_thread.is_alive():
        return

    def _cleanup_loop():
        def idle_timeout():
            return get_settings().publish_remote_browser_idle_timeout_seconds

        idle_tick = 0
        while not _idle_cleanup_stop.is_set():
            _idle_cleanup_stop.wait(2)
            if _idle_cleanup_stop.is_set():
                break
            try:
                _cleanup_stop_requested_sessions()
            except Exception:
                _logger.warning("stop-requested session cleanup failed", exc_info=True)
            idle_tick += 1
            if idle_tick >= 15:  # 每 30 秒
                idle_tick = 0
                try:
                    _cleanup_stale_sessions(idle_timeout())
                except Exception:
                    _logger.warning("stale session cleanup failed", exc_info=True)
                try:
                    _cleanup_zombie_sessions()
                except Exception:
                    _logger.warning("zombie session cleanup failed", exc_info=True)

    _idle_cleanup_thread = threading.Thread(
        target=_cleanup_loop, daemon=True, name="session-idle-cleanup"
    )
    _idle_cleanup_thread.start()


def _cleanup_stop_requested_sessions() -> None:
    """杀掉 DB 中已标记 stop_requested 的会话。"""
    stop_ids = _query_stop_requested_session_ids()
    for session_id in stop_ids:
        with _sessions_lock:
            session = _active_sessions.get(session_id)
        if session is not None:
            _logger.info("Stopping session %s (stop_requested via DB)", session_id)
            stop_remote_browser_session(session_id)


def _cleanup_stale_sessions(idle_timeout: int) -> None:
    """回收 keep_alive 但已超过空闲超时的会话（如人工接管 waiting_user_input 后无人完成）。"""
    now = time.monotonic()
    stale_ids: list[str] = []
    with _sessions_lock:
        for session_id in list(_session_keep_alive):
            session = _active_sessions.get(session_id)
            if session is None:
                _session_keep_alive.discard(session_id)
                continue
            if now - session.started_at > idle_timeout:
                stale_ids.append(session_id)
                _session_keep_alive.discard(session_id)

    for session_id in stale_ids:
        try:
            stop_remote_browser_session(session_id)
        except Exception:
            _logger.warning("Failed to stop stale session %s", session_id, exc_info=True)
        with _sessions_lock:
            stale_records = [rid for rid, sid in _record_to_session.items() if sid == session_id]
            for rid in stale_records:
                _record_to_session.pop(rid, None)


def _cleanup_zombie_sessions() -> None:
    """回收任一底层进程（Xvfb/x11vnc/websockify）已退出的会话——进程链断了，会话已不可用。"""
    zombie_ids: list[str] = []
    with _sessions_lock:
        for session_id, session in list(_active_sessions.items()):
            for mp in session.processes:
                if mp.process.poll() is not None:
                    zombie_ids.append(session_id)
                    break

    for session_id in zombie_ids:
        _logger.warning("Zombie session detected (process exited): %s", session_id)
        try:
            stop_remote_browser_session(session_id)
        except Exception:
            _logger.warning("Failed to stop zombie session %s", session_id, exc_info=True)
        with _sessions_lock:
            stale_records = [rid for rid, sid in _record_to_session.items() if sid == session_id]
            for rid in stale_records:
                _record_to_session.pop(rid, None)


def _stop_idle_cleanup() -> None:
    global _idle_cleanup_thread
    _idle_cleanup_stop.set()
    if _idle_cleanup_thread is not None:
        _idle_cleanup_thread.join(timeout=3)
        _idle_cleanup_thread = None


def _novnc_url(host: str, novnc_port: int) -> str:
    # 经 80 端口反代访问：真实 websockify 端口编进 ws path，前端连 port=80 由反代转发到 novnc_port
    return f"http://{host}/novnc/vnc.html?host={host}&port=80&path=novnc/ws/{novnc_port}"


def _reset_globals() -> None:
    """重置全部模块级状态（测试清理用）。"""
    global _active_sessions, _record_to_session, _session_keep_alive
    global _reserved_displays, _reserved_vnc_ports, _reserved_novnc_ports
    with _sessions_lock:
        _active_sessions.clear()
        _record_to_session.clear()
        _session_keep_alive.clear()
        _reserved_displays.clear()
        _reserved_vnc_ports.clear()
        _reserved_novnc_ports.clear()
        _account_sessions.clear()
