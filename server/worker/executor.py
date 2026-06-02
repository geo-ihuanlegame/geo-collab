"""
Worker executor: polls the DB for pending tasks and executes them.

Run as: python -m server.worker.executor

Each worker registers itself with a unique WORKER_ID (hostname + PID).
Tasks are claimed atomically via optimistic locking on the worker_id column.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy import update as sa_update

import server.app.modules.image_library.models  # noqa: F401  # 确保 StockCategory 注册到 mapper registry（Article.stock_category 关系依赖它）
from server.app.core.time import utcnow
from server.app.db.session import SessionLocal
from server.app.modules.accounts import process_account_login_session_requests
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
)
from server.app.modules.tasks.models import PublishRecord, PublishTask

_logger = logging.getLogger(__name__)

# Unique identity for this worker process
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
CLAIM_LEASE_MINUTES = 10
PROFILE_LOCK_HEARTBEAT_SECONDS = 30
PROFILE_LOCK_LEASE_SECONDS = 900

# Graceful shutdown flag
_shutdown = False
_profile_lock_heartbeat_at = 0.0
_profile_lock_heartbeat_lock = threading.Lock()


def _handle_signal(signum, frame) -> None:
    global _shutdown
    _logger.info("Worker %s: shutdown signal received (%s)", WORKER_ID, signum)
    _shutdown = True


def _claim_next_task(db) -> PublishTask | None:
    """Claim a pending task with pending records via optimistic locking. Returns the task or None."""
    from sqlalchemy import exists

    from server.app.modules.tasks.models import PublishRecord

    # Find a task with at least one pending record and no active worker claim
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
        return None  # Race: another worker claimed it first

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


def _account_login_loop() -> None:
    """Process interactive login commands independently from publish task execution."""
    last_heartbeat = 0.0
    while not _shutdown:
        db = SessionLocal()
        processed = False
        try:
            now = time.monotonic()
            if now - last_heartbeat >= 10:
                _write_worker_heartbeat(db)
                last_heartbeat = now
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
    """Run recovery routines on worker startup."""
    recover_stuck_records(db)
    recover_stuck_task_claims(db)
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


def main() -> None:
    # Register as GEO_WORKER_ID so browser_sessions.py can tag DB rows
    os.environ["GEO_WORKER_ID"] = WORKER_ID

    # Import all drivers to trigger registration
    import server.app.modules.tasks.drivers.toutiao  # noqa: F401

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

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
            # Periodic recovery: detect tasks stuck in "running" with all records terminal
            if _recovery_cycle % 60 == 0:
                try:
                    _check_stuck_tasks(db)
                except Exception:
                    _logger.exception("Worker %s: stuck task recovery check failed", WORKER_ID)
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

    _logger.info("Worker %s exited", WORKER_ID)


if __name__ == "__main__":
    main()
