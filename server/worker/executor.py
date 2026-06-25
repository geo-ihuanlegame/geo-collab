"""
发布 worker 执行器：轮询数据库中的待处理任务并执行。

运行方式：python -m server.worker.executor

每个 worker 使用唯一的 WORKER_ID（主机名 + PID）注册。
任务通过 worker_id 列上的乐观锁原子抢占。
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from datetime import timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update

import server.app.modules.image_library.models  # noqa: F401  # 确保 StockCategory 注册到映射注册表（Article.stock_category 关系依赖它）

# 导入全部驱动以触发注册。worker 与 Web 应用是不同进程，因此必须自行注册全部驱动
# （含默认 DOM 驱动、页内变体、wechat_mp 等 API 驱动）。注册集中在 drivers.bootstrap，
# 与 main.py 共用同一份，避免「main.py 加了驱动忘了同步 worker」的漂移。
import server.app.modules.tasks.drivers.bootstrap  # noqa: F401
from server.app.core.time import utcnow
from server.app.db.session import SessionLocal
from server.app.modules.accounts import (
    expire_stale_login_sessions,
    process_account_login_session_requests,
    recover_stuck_browser_sessions,
    recover_stuck_login_sessions,
)
from server.app.modules.accounts.models import (
    Account,
    AccountLoginSession,
    BrowserProfileLock,
    BrowserSession,
    RecordBrowserSession,
)
from server.app.modules.accounts.service import profile_key_from_state_path
from server.app.modules.system.models import WorkerHeartbeat
from server.app.modules.tasks import (
    execute_task,
    get_task,
    recover_stuck_records,
    recover_stuck_task_claims,
    reopen_orphaned_terminal_tasks,
)
from server.app.modules.tasks.models import PublishRecord, PublishTask

_logger = logging.getLogger(__name__)

# 当前 worker 进程的唯一标识
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
CLAIM_LEASE_MINUTES = 10
PROFILE_LOCK_HEARTBEAT_SECONDS = 30
PROFILE_LOCK_LEASE_SECONDS = 900
# active 登录会话被遗弃（用户关标签页/刷新/崩溃，没走 finish/cancel）时，其 profile 锁会被
# 心跳无限续租、永久把账号挡在登录之外（#85 死锁的残留路径）。超过这个时长即视为僵死、按取消
# 流程收尾释放锁。阈值远大于真人扫码登录耗时，不会误杀进行中的真实登录。
LOGIN_SESSION_MAX_ACTIVE_SECONDS = 1800
LOGIN_SESSION_STALE_CHECK_SECONDS = 60

# 优雅退出标记
_shutdown = False
_profile_lock_heartbeat_at = 0.0
_profile_lock_heartbeat_lock = threading.Lock()


def _handle_signal(signum, frame) -> None:
    global _shutdown
    _logger.info("Worker %s: shutdown signal received (%s)", WORKER_ID, signum)
    _shutdown = True


def _claim_next_task(db) -> PublishTask | None:
    """通过乐观锁抢占带待处理记录的任务，成功时返回任务，否则返回 None。"""
    from sqlalchemy import exists

    from server.app.modules.tasks.models import PublishRecord

    # 查找至少有一条待处理记录且尚未被 worker 抢占的任务
    candidate_id = db.execute(
        select(PublishTask.id)
        .where(
            PublishTask.status.in_(["pending", "running"]),
            PublishTask.is_deleted == False,  # noqa: E712
            PublishTask.worker_id.is_(None),
            exists(
                select(1).where(
                    PublishRecord.task_id == PublishTask.id,
                    PublishRecord.status == "pending",
                    PublishRecord.is_deleted == False,  # noqa: E712
                )
            ),
        )
        .order_by(PublishTask.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()

    if candidate_id is None:
        return None

    now = utcnow()
    lease_until = now + timedelta(minutes=CLAIM_LEASE_MINUTES)
    rows = db.execute(
        sa_update(PublishTask)
        .where(
            PublishTask.id == candidate_id,
            PublishTask.worker_id.is_(None),
            PublishTask.is_deleted == False,  # noqa: E712
        )
        .values(worker_id=WORKER_ID, worker_lease_until=lease_until, worker_heartbeat_at=now)
    ).rowcount

    if rows == 0:
        return None  # 竞态：其他 worker 已先抢占

    db.commit()
    return get_task(db, candidate_id)


def _release_task_claim(db, task_id: int) -> None:
    db.execute(
        sa_update(PublishTask)
        .where(
            PublishTask.id == task_id,
            PublishTask.worker_id == WORKER_ID,
            PublishTask.is_deleted == False,  # noqa: E712
        )
        .values(worker_id=None, worker_lease_until=None, worker_heartbeat_at=None)
    )
    db.commit()


def _write_worker_heartbeat(db) -> None:
    db.merge(
        WorkerHeartbeat(
            worker_id=WORKER_ID,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            heartbeat_at=utcnow(),
        )
    )
    _heartbeat_active_profile_locks(db)
    db.commit()


def _heartbeat_active_profile_locks(db) -> None:
    global _profile_lock_heartbeat_at
    now_monotonic = time.monotonic()
    with _profile_lock_heartbeat_lock:
        if now_monotonic - _profile_lock_heartbeat_at < PROFILE_LOCK_HEARTBEAT_SECONDS:
            return
        _profile_lock_heartbeat_at = now_monotonic

    now = utcnow()
    lease_until = now + timedelta(seconds=PROFILE_LOCK_LEASE_SECONDS)
    owners: list[tuple[str, str, str]] = []

    login_rows = db.execute(
        select(AccountLoginSession.id, Account.state_path)
        .join(Account, Account.id == AccountLoginSession.account_id)
        .where(
            AccountLoginSession.status == "active",
            AccountLoginSession.worker_id == WORKER_ID,
        )
    ).all()
    owners.extend(
        (profile_key_from_state_path(state_path), "login", str(request_id))
        for request_id, state_path in login_rows
    )

    publish_rows = db.execute(
        select(PublishRecord.id, Account.state_path)
        .join(Account, Account.id == PublishRecord.account_id)
        .join(RecordBrowserSession, RecordBrowserSession.record_id == PublishRecord.id)
        .join(BrowserSession, BrowserSession.id == RecordBrowserSession.session_id)
        .where(
            PublishRecord.status.in_(["waiting_manual_publish", "waiting_user_input"]),
            PublishRecord.is_deleted == False,  # noqa: E712
            BrowserSession.worker_id == WORKER_ID,
        )
    ).all()
    owners.extend(
        (profile_key_from_state_path(state_path), "publish", str(record_id))
        for record_id, state_path in publish_rows
    )

    for profile_key, owner_kind, owner_id in owners:
        db.execute(
            sa_update(BrowserProfileLock)
            .where(
                BrowserProfileLock.profile_key == profile_key,
                BrowserProfileLock.owner_kind == owner_kind,
                BrowserProfileLock.owner_id == owner_id,
            )
            .values(heartbeat_at=now, lease_until=lease_until, worker_id=WORKER_ID)
        )


def _clear_stale_worker_heartbeats(db) -> None:
    """启动清理：删掉其它 worker_id 的心跳行，防 worker_heartbeats 随每次容器重启无限累积。

    WORKER_ID=hostname-pid 每次重启都变，_write_worker_heartbeat 的 merge 退化成 insert、旧行
    永不回收。这里删 worker_id != 当前 WORKER_ID 的全部行（**绝不删自己**）。worker_heartbeats
    无 FK 依赖，唯一读者是系统状态页的 30s 窗口计数，删历史死 worker 的行零影响。自带 commit。
    """
    result = db.execute(sa_delete(WorkerHeartbeat).where(WorkerHeartbeat.worker_id != WORKER_ID))
    rows = result.rowcount  # type: ignore[attr-defined]  # DML 执行返回 CursorResult
    if rows:
        _logger.warning("Cleared %d stale worker heartbeat row(s) on startup", rows)
        db.commit()


def _account_login_loop() -> None:
    """独立于发布任务执行，处理交互式登录命令。"""
    last_heartbeat = 0.0
    last_stale_check = 0.0
    while not _shutdown:
        db = SessionLocal()
        processed = False
        try:
            now = time.monotonic()
            if now - last_heartbeat >= 10:
                _write_worker_heartbeat(db)
                last_heartbeat = now
            if now - last_stale_check >= LOGIN_SESSION_STALE_CHECK_SECONDS:
                last_stale_check = now
                try:
                    expire_stale_login_sessions(
                        db,
                        worker_id=WORKER_ID,
                        max_active_seconds=LOGIN_SESSION_MAX_ACTIVE_SECONDS,
                    )
                except Exception:
                    _logger.exception("Worker %s: stale login session expiry failed", WORKER_ID)
            processed = process_account_login_session_requests(db, WORKER_ID)
        except Exception:
            _logger.exception("Worker %s: error processing account login request", WORKER_ID)
        finally:
            try:
                db.close()
            except Exception:
                pass
        time.sleep(0.1 if processed else 0.5)


def _startup(db) -> None:
    """worker 启动时执行恢复流程。"""
    recover_stuck_records(db)
    recover_stuck_task_claims(db)
    reopen_orphaned_terminal_tasks(db)
    recover_stuck_login_sessions(db)
    # 启动期资源回收：清掉上条命残留的 browser_sessions / worker_heartbeats 孤儿行（容器重启后
    # worker_id 全变，旧行无人回收、永久累积）。纯卫生操作，**失败绝不能拖垮 worker 启动**
    # （restart: unless-stopped 下 boot 崩溃 = 无限重启 = 发布瘫痪），故各自 best-effort 兜异常 +
    # rollback 还原 session，保证后续 _write_worker_heartbeat 仍可用。
    try:
        recover_stuck_browser_sessions(db, worker_id=WORKER_ID)
    except Exception:
        db.rollback()
        _logger.exception("Worker %s: stale browser session cleanup failed", WORKER_ID)
    try:
        _clear_stale_worker_heartbeats(db)
    except Exception:
        db.rollback()
        _logger.exception("Worker %s: stale worker heartbeat cleanup failed", WORKER_ID)
    _write_worker_heartbeat(db)
    _logger.info("Worker %s started", WORKER_ID)


def _check_stuck_tasks(db) -> None:
    from server.app.modules.tasks.service import (
        TERMINAL_TASK_STATUSES,
        aggregate_task_status,
        list_task_records,
    )

    stuck_tasks = (
        db.execute(
            select(PublishTask).where(
                PublishTask.status == "running",
                PublishTask.worker_id.is_(None),
                PublishTask.is_deleted == False,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )

    for t in stuck_tasks:
        records = list_task_records(db, t.id)
        all_terminal = all(r.status in TERMINAL_TASK_STATUSES for r in records) if records else True
        if all_terminal:
            _logger.warning("Recovering stuck task %d (running but all records terminal)", t.id)
            aggregate_task_status(db, t, records)
    if stuck_tasks:
        db.commit()


def _periodic_recovery(db) -> None:
    """主循环周期复位卡死状态（#7）：不再只在 _startup 跑一次。

    三类恢复彼此独立、各包 try/except，单个失败不拖垮主循环：
      - recover_stuck_records：status='running' 且 lease 过期的记录拨回 pending（租约保护，
        不误伤别的进程在跑的记录）；否则进程不重启时这些记录永久占着账号/profile 锁。
      - recover_stuck_task_claims：清空 worker_lease_until 已过期的 worker 认领，让别人重抢。
      - _check_stuck_tasks：记录均已终态但任务仍卡 running 时收口。
    三者均自带 commit / 条件 UPDATE，专为周期调用设计。
    """
    try:
        recover_stuck_records(db)
    except Exception:
        _logger.exception("Worker %s: periodic recover_stuck_records failed", WORKER_ID)
    try:
        recover_stuck_task_claims(db)
    except Exception:
        _logger.exception("Worker %s: periodic recover_stuck_task_claims failed", WORKER_ID)
    try:
        reopen_orphaned_terminal_tasks(db)
    except Exception:
        _logger.exception("Worker %s: periodic reopen_orphaned_terminal_tasks failed", WORKER_ID)
    try:
        _check_stuck_tasks(db)
    except Exception:
        _logger.exception("Worker %s: stuck task recovery check failed", WORKER_ID)


def main() -> None:
    # 注册为 GEO_WORKER_ID，供 browser_sessions.py 标记数据库行
    os.environ["GEO_WORKER_ID"] = WORKER_ID

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # 统一日志配置（级别 / 格式 / 运行上下文 / stdout+滚动文件），与 web 进程一致
    from server.app.core.logging import configure_logging

    configure_logging()

    db = SessionLocal()
    try:
        _startup(db)
    finally:
        db.close()

    _logger.info("Worker %s entering main loop", WORKER_ID)
    login_thread = threading.Thread(
        target=_account_login_loop, daemon=True, name="account-login-worker"
    )
    login_thread.start()

    _recovery_cycle = 0

    while not _shutdown:
        db = SessionLocal()
        task_id: int | None = None
        try:
            _write_worker_heartbeat(db)
            # 周期性恢复：复位过期 lease 的卡死记录/认领 + 收口记录均终态却仍 running 的任务。
            if _recovery_cycle % 60 == 0:
                _periodic_recovery(db)
            _recovery_cycle += 1
            task = _claim_next_task(db)
            if task is None:
                db.close()
                time.sleep(1)
                continue

            task_id = task.id
            _logger.info("Worker %s claimed task %d", WORKER_ID, task_id)
            execute_task(db, task)
            db.commit()
            _logger.info("Worker %s finished task %d", WORKER_ID, task_id)

        except Exception:
            _logger.exception("Worker %s: error executing task %s", WORKER_ID, task_id)
            try:
                db.rollback()
            except Exception:
                pass
            time.sleep(5)
        finally:
            if task_id is not None:
                try:
                    _release_task_claim(db, task_id)
                except Exception:
                    pass
            try:
                db.close()
            except Exception:
                pass

    try:
        from server.app.modules.accounts.login_broker import login_broker

        login_broker.shutdown()
    except Exception:
        _logger.warning("Worker %s: login broker shutdown failed", WORKER_ID, exc_info=True)

    _logger.info("Worker %s exited", WORKER_ID)


if __name__ == "__main__":
    main()
