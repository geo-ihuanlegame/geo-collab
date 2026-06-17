"""
任务执行引擎：把一个 PublishTask 的 pending 记录并发跑成发布。

并发分层（见 CLAUDE.md「Task Execution」）：
  per-task 锁（_task_locks，同一任务同时只能一个执行循环）
    → 全局发布闸 _global_publish_gate（ObservableGate(MAX_CONCURRENT_RECORDS)，跨任务封顶并发发布数；
      Task 4 Step 5/#8：主线程 submit 前 try_acquire、记录退场处释放，发布线程不再持闸）
      → 每账号串行锁（_account_locks，同账号同时只发一条）
        → 浏览器 profile 锁（accounts.try_acquire_profile_lock，跨进程，发布 vs 登录互斥）。

DB session 非线程安全：实际发布在 ThreadPoolExecutor 线程里跑（_publish_record，纯浏览器自动化、
不碰 db），所有 DB 读写都回到执行循环所在线程做。记录状态推进一律走条件 UPDATE
（status='running' 才改），rowcount 为乐观锁，防止外部已改状态时误覆盖。
本文件函数自带 db.commit()，与多数只 flush 的 service 函数不同。
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from server.app.core.config import get_settings
from server.app.core.time import utcnow
from server.app.modules.accounts.browser import (
    associate_record_with_session,
    disassociate_record,
    get_session_for_record,
    release_profile_lock_by_owner,
    stop_remote_browser_session,
    try_acquire_profile_lock,
)
from server.app.modules.accounts.models import Account
from server.app.modules.articles.models import Article, ArticleBodyAsset
from server.app.modules.articles.parser import has_publishable_body
from server.app.modules.tasks.drivers.base import PublishError, PublishResult, UserInputRequired
from server.app.modules.tasks.models import PublishRecord, PublishTask
from server.app.modules.tasks.service import (
    PAUSED_RECORD_STATUSES,
    TERMINAL_TASK_STATUSES,
    add_log,
    aggregate_task_status,
    get_task,
    list_task_records,
)
from server.app.shared.concurrency import ObservableGate, register_gate
from server.app.shared.diagnostics import PublishDiagnosticEvent, capture_publish_diagnostics
from server.app.shared.errors import ConflictError
from server.app.shared.resource_metrics import emit_resource_alert

MAX_CONCURRENT_RECORDS = 5
WORKER_LEASE_EXTENSION_SECONDS = 600
# 超时记录关 context 后，等发布线程确认终止的上限；超时仍存活＝卡死，保留账号/profile 锁（#2）
_THREAD_TERMINATION_TIMEOUT = 10.0
# 僵尸记录标记（回填到 failed 行的 queue_reason，不改 status、无需迁移）
_ZOMBIE_QUEUE_REASON = "僵尸待清：发布线程超时未终止，账号/profile 锁保留待下轮恢复回收"

_task_locks: dict[int, threading.Lock] = {}
_account_locks: dict[int, threading.Lock] = {}
_account_locks_lock = threading.Lock()
# 全局发布并发闸（跨任务封顶）。Task 4 Step 5/#8：裸 Semaphore → ObservableGate，且获取点从
# 发布线程移到主线程 submit 前（见 _start_runnable_records）——排队不再计入记录执行预算、可观测占用。
_global_publish_gate = register_gate(ObservableGate(MAX_CONCURRENT_RECORDS, name="publish"))
_task_cancel: dict[int, threading.Event] = {}

_logger = logging.getLogger(__name__)


def _profile_key_from_state_path(state_path: str) -> str:
    # profile 锁的 key = state 文件所在目录（同 profile 共享一把锁）；超长则用 sha256 压到 DB 列宽内
    key = os.path.dirname(state_path).replace("\\", "/")
    if len(key) <= 240:
        return key
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunningRecord:
    record_id: int
    account_id: int
    started_monotonic: float


@dataclass(frozen=True)
class RecordPublishOutcome:
    result: PublishResult
    diagnostics: list[PublishDiagnosticEvent]


def _max_concurrent_records() -> int:
    return max(1, min(int(get_settings().publish_max_concurrent_records), MAX_CONCURRENT_RECORDS))


def execute_task(db: Session, task: PublishTask) -> PublishTask:
    """执行一个任务：把 pending 记录跑成发布，阻塞到本批次记录全部收口或暂停后返回。

    进程内 per-task 锁串行化（同任务并发执行抛 ConflictError）。pending→running 用条件 UPDATE
    抢占（rowcount==0 说明被别的执行者/worker 抢走，按其状态收尾），非 pending 则只续 worker 心跳。
    """
    lock = _task_locks.setdefault(task.id, threading.Lock())
    locked = lock.acquire(blocking=False)
    if not locked:
        raise ConflictError(f"Task {task.id} is already being executed")

    cancel_event = threading.Event()
    _task_cancel[task.id] = cancel_event

    try:
        if task.is_deleted:
            raise ConflictError(f"Task {task.id} has been deleted")
        if task.status in TERMINAL_TASK_STATUSES:
            raise ConflictError(f"Task is already terminal: {task.status}")

        now = utcnow()
        if task.status == "pending":
            # claim：pending→running 条件 UPDATE，rowcount==1 才算我抢到这次执行权
            stmt = (
                sa_update(PublishTask)
                .where(
                    PublishTask.id == task.id,
                    PublishTask.status == "pending",
                    PublishTask.is_deleted == False,  # noqa: E712
                )
                .values(
                    status="running",
                    started_at=now,
                    cancel_requested=False,
                    worker_heartbeat_at=now,
                )
            )
            if db.execute(stmt).rowcount == 0:  # type: ignore[attr-defined]  # DML 执行返回 CursorResult
                db.flush()
                refreshed = get_task(db, task.id)
                if refreshed is None or refreshed.status in TERMINAL_TASK_STATUSES:
                    return refreshed or task
                task = refreshed
            else:
                task.status = "running"
                task.started_at = now
                task.cancel_requested = False
                task.worker_heartbeat_at = now
            add_log(db, task.id, None, "info", "Task started")
            _logger.info("Task %d started", task.id)
        else:
            _heartbeat_task_worker(db, task.id)

        _run_pending_records(db, task)
        db.flush()
        result = get_task(db, task.id) or task
        _logger.info("Task %d finished with status %s", task.id, result.status)
        return result
    finally:
        _task_locks.pop(task.id, None)
        _task_cancel.pop(task.id, None)
        if locked:
            lock.release()


def _heartbeat_task_worker(db: Session, task_id: int) -> None:
    # 续 worker 心跳/租约：只有生产 worker（设了 GEO_WORKER_ID）才续 lease，避免本机后台执行误占租约
    now = utcnow()
    values: dict[str, object] = {"worker_heartbeat_at": now}
    if os.environ.get("GEO_WORKER_ID"):
        values["worker_lease_until"] = now + timedelta(seconds=WORKER_LEASE_EXTENSION_SECONDS)
    db.execute(
        sa_update(PublishTask)
        .where(PublishTask.id == task_id, PublishTask.is_deleted == False)  # noqa: E712
        .values(**values)
    )


def _heartbeat_running_records(db: Session, task_id: int) -> None:
    now = utcnow()
    new_lease = now + timedelta(seconds=_record_execution_budget() + 60)
    db.execute(
        sa_update(PublishRecord)
        .where(
            PublishRecord.task_id == task_id,
            PublishRecord.status == "running",
            PublishRecord.is_deleted == False,  # noqa: E712
        )
        .values(lease_until=new_lease)
    )


def _task_cancel_requested(db: Session, task_id: int) -> bool:
    value = db.execute(
        select(PublishTask.cancel_requested).where(
            PublishTask.id == task_id,
            PublishTask.is_deleted == False,  # noqa: E712
        )
    ).scalar_one_or_none()
    return bool(value)


def _request_task_cancel(db: Session, task_id: int) -> None:
    db.execute(
        sa_update(PublishTask)
        .where(PublishTask.id == task_id, PublishTask.is_deleted == False)  # noqa: E712
        .values(cancel_requested=True)
    )


def _cancel_not_running_records(
    db: Session, task: PublishTask, records: list[PublishRecord]
) -> None:
    now = utcnow()
    changed = False
    for record in records:
        if record.status not in {"pending", "waiting_manual_publish", "waiting_user_input"}:
            continue
        if record.status in PAUSED_RECORD_STATUSES:
            _stop_record_session(record.id)
        record.status = "cancelled"
        record.finished_at = now
        record.lease_until = None
        record.queue_reason = None
        changed = True
    if changed:
        add_log(db, task.id, None, "warn", "Cancellation requested; pending records were stopped")


def _run_pending_records(db: Session, task: PublishTask) -> None:
    """核心执行循环：每轮续心跳→检查取消/暂停→拉起可跑记录→等 future 完成并写回结果。

    退出条件：取消且无在跑、暂停（waiting_user_input / stop_before_publish 停在 manual）且无在跑、
    或无 pending 且无在跑（此时聚合 task 终态）。超过 _record_execution_budget() 的 future
    判超时：标失败 + 停会话（关 Chromium → Playwright 线程收到 TargetClosedError 自行结束）。
    finally 兜底释放所有账号锁并 shutdown 线程池。
    """
    cancel_evt = _task_cancel.get(task.id)
    running: dict[Future, RunningRecord] = {}
    executor = ThreadPoolExecutor(
        max_workers=_max_concurrent_records(), thread_name_prefix="publish"
    )

    try:
        while True:
            _heartbeat_task_worker(db, task.id)
            _heartbeat_running_records(db, task.id)
            cancel_requested = _task_cancel_requested(db, task.id)
            if cancel_evt and cancel_evt.is_set():
                if not cancel_requested:
                    _request_task_cancel(db, task.id)
                    cancel_requested = True
            if cancel_requested:
                task.cancel_requested = True

            records = list_task_records(db, task.id)

            if cancel_requested:
                _cancel_not_running_records(db, task, records)
                records = list_task_records(db, task.id)
                if not running and not any(record.status == "running" for record in records):
                    aggregate_task_status(db, task, records)
                    db.commit()
                    return
            else:
                _paused_for_user = any(record.status == "waiting_user_input" for record in records)
                _paused_for_manual = task.stop_before_publish and any(
                    record.status == "waiting_manual_publish" for record in records
                )

                if _paused_for_user or _paused_for_manual:
                    if not running:
                        # 所有进行中的 future 都已完成，可以安全退出。
                        db.commit()
                        return
                    # 仍有运行中的 future，继续落到 wait 循环，等它们完成并把结果写回 DB。
                else:
                    _start_runnable_records(db, task, executor, running, records)

            if not running:
                if not any(record.status == "pending" for record in records):
                    aggregate_task_status(db, task, records)
                    db.commit()
                    return
                db.commit()
                time.sleep(0.2)
                continue

            done, _ = wait(running.keys(), timeout=1, return_when=FIRST_COMPLETED)
            timed_out = [
                future
                for future, running_record in running.items()
                if time.monotonic() - running_record.started_monotonic > _record_execution_budget()
            ]
            for future in set(done) | set(timed_out):
                running_record = running.pop(future)
                if future in timed_out and not future.done():
                    # #2：超时分支只有在确认发布线程已终止后才放账号/profile 锁，见 helper。
                    _handle_timed_out_record(db, task.id, running_record, future)
                    db.commit()
                    continue
                try:
                    _finish_record_future(db, task, running_record.record_id, future)
                finally:
                    _retire_running_slot(running_record)
                    db.commit()
    finally:
        for running_record in running.values():
            _retire_running_slot(running_record)
        executor.shutdown(wait=False, cancel_futures=True)


def _start_runnable_records(
    db: Session,
    task: PublishTask,
    executor: ThreadPoolExecutor,
    running: dict[Future, RunningRecord],
    records: list[PublishRecord],
) -> None:
    """填满空闲并发槽：挑下一条可跑 pending 记录，逐级拿账号锁 + profile 锁，claim 后提交到线程池。

    同账号同时只跑一条（running_accounts / blocked_accounts 跳过）。账号锁拿不到→记 blocked 跳过；
    profile 锁拿不到→把记录标排队（queue_reason）后续重试。异常路径必须释放已拿的锁（profile + 账号）。
    """
    running_accounts = {item.account_id for item in running.values()}
    blocked_accounts: set[int] = set()
    slots = _max_concurrent_records() - len(running)
    if task.stop_before_publish:
        # stop_before_publish 串行：一次只拉一条，方便人工逐条确认
        slots = min(slots, 1)
    if slots <= 0:
        return

    while slots > 0:
        db.flush()
        next_record = next(
            (
                record
                for record in records
                if record.status == "pending"
                and record.account_id not in running_accounts
                and record.account_id not in blocked_accounts
            ),
            None,
        )
        if next_record is None:
            return

        # Task 4 Step 5/#8：全局发布槽在主线程 submit 前非阻塞获取。满了直接 return——本轮不再填，
        # 执行循环下一轮重试；排队不再占记录执行预算（watchdog），也不在发布线程里阻塞。
        if not _global_publish_gate.try_acquire():
            return

        # 槽所有权：仅当 submit 成功并登记 RunningRecord 后才"移交"给运行生命周期
        # （gate_transferred=True，由 _retire_running_slot 在记录退场处释放）；任何 submit 前的
        # 跳过 / 异常都由下面的 finally 归还，绝不泄漏。
        gate_transferred = False
        try:
            if not _try_acquire_account_lock(next_record.account_id):
                blocked_accounts.add(next_record.account_id)
                continue

            try:
                article = _load_article_for_publish(db, next_record.article_id)
                account = db.get(Account, next_record.account_id)
                validation_error = _validate_record_inputs(article, account)
                if (
                    account is not None
                    and validation_error is None
                    and account.state_path is not None
                ):
                    profile_key = _profile_key_from_state_path(account.state_path)
                    reason = "账号正在执行发布或登录操作，发布记录已排队"
                    if not try_acquire_profile_lock(
                        profile_key,
                        owner_kind="publish",
                        owner_id=next_record.id,
                        queue_reason=reason,
                        lease_seconds=int(_record_execution_budget()) + 120,
                    ):
                        _defer_record_for_profile_lock(db, task.id, next_record, reason)
                        blocked_accounts.add(next_record.account_id)
                        _release_account_lock(next_record.account_id)
                        db.commit()
                        continue

                # claim：pending→running 条件 UPDATE，抢不到（被别人改了状态）就退还两把锁跳过
                if not _claim_record(db, task.id, next_record):
                    release_profile_lock_by_owner(owner_kind="publish", owner_id=next_record.id)
                    _release_account_lock(next_record.account_id)
                    continue

                if validation_error or article is None or account is None:
                    _mark_record_failed(
                        db,
                        task.id,
                        next_record.id,
                        validation_error or "Record article or account not found",
                    )
                    release_profile_lock_by_owner(owner_kind="publish", owner_id=next_record.id)
                    _release_account_lock(next_record.account_id)
                    db.commit()
                    continue

                # 把 ORM 对象从 session 摘下再交给发布线程：发布线程不碰 db（session 非线程安全）
                _detach_record_inputs(db, next_record, article, account)
                db.commit()
                future = executor.submit(
                    _publish_record, next_record, article, account, task.stop_before_publish
                )
                running[future] = RunningRecord(
                    next_record.id, next_record.account_id, time.monotonic()
                )
                running_accounts.add(next_record.account_id)
                slots -= 1
                gate_transferred = True  # submit 成功并登记，槽位移交运行生命周期
            except Exception:
                release_profile_lock_by_owner(owner_kind="publish", owner_id=next_record.id)
                _release_account_lock(next_record.account_id)
                raise
        finally:
            if not gate_transferred:
                _global_publish_gate.release()


def _try_acquire_account_lock(account_id: int) -> bool:
    # 进程内每账号串行锁：_account_locks_lock 只护住「取/建锁」这步，再 non-blocking 抢账号锁本身
    with _account_locks_lock:
        lock = _account_locks.setdefault(account_id, threading.Lock())
    return lock.acquire(blocking=False)


def _release_account_lock(account_id: int) -> None:
    lock = _account_locks.get(account_id)
    if lock is not None:
        try:
            lock.release()
        except RuntimeError:
            pass  # 已释放，无害


def _retire_running_slot(running_record: RunningRecord) -> None:
    """记录退场：归还移交给运行生命周期的全局发布槽 + 账号锁（Task 4 Step 5）。

    每条 RunningRecord 恰好持有一个闸槽（submit 成功时移交），退场处释放一次。over-release
    会被 ObservableGate 抛 ValueError——这里吞掉并告警（执行循环不应因释放漏口崩溃，同
    _release_account_lock 的防御姿态），异常本身也写进日志供排查。
    """
    try:
        _global_publish_gate.release()
    except ValueError:
        _logger.warning(
            "publish gate over-release for record %d (slot accounting bug?)",
            running_record.record_id,
            exc_info=True,
        )
    _release_account_lock(running_record.account_id)


def _defer_record_for_profile_lock(
    db: Session, task_id: int, record: PublishRecord, reason: str
) -> None:
    """profile 锁被占（账号正在发布/登录）时把记录标记为排队，留 pending 等下一轮重试。

    日志去重：reason 不变就不重复写 log，避免轮询循环刷屏。
    """
    already_queued = getattr(record, "queue_reason", None) == reason
    db.execute(
        sa_update(PublishRecord)
        .where(
            PublishRecord.id == record.id,
            PublishRecord.status == "pending",
            PublishRecord.is_deleted == False,  # noqa: E712
        )
        .values(queue_reason=reason, lease_until=None)
    )
    record.queue_reason = reason
    if not already_queued:
        add_log(db, task_id, record.id, "info", reason)


def _close_record_browser(record_id: int) -> None:
    """关该记录的远程浏览器会话 + 清会话映射（信号发布线程退出）。**不释放 profile 锁**——

    超时分支要等发布线程确认终止后才放锁，避免下一条同账号记录对同一 persistent profile 并发
    再开 Chromium 损坏目录（#2）。常规收尾走 `_stop_record_session`（关会话后立即放锁）。
    """
    try:
        session = get_session_for_record(record_id)
        if session is not None:
            stop_remote_browser_session(session.id)
    except Exception:
        _logger.warning("Failed to stop browser session for record %d", record_id, exc_info=True)
    try:
        disassociate_record(record_id)
    except Exception:
        _logger.warning(
            "Failed to clear browser session mapping for record %d", record_id, exc_info=True
        )


def _release_record_profile_lock(record_id: int) -> None:
    try:
        release_profile_lock_by_owner(owner_kind="publish", owner_id=record_id)
    except Exception:
        _logger.warning(
            "Failed to release browser profile lock for record %d", record_id, exc_info=True
        )


def _stop_record_session(record_id: int) -> None:
    """常规收尾：关会话 + 清映射 + 释放 profile 锁（线程已确认退场的路径）。"""
    _close_record_browser(record_id)
    _release_record_profile_lock(record_id)


def _mark_record_zombie(db: Session, task_id: int, record_id: int) -> None:
    """标超时记录为「僵尸待清」：发布线程未在超时内确认终止，账号/profile 锁有意保留。

    记录已被 `_mark_record_failed` 置 failed；这里仅在该行回填 queue_reason 作标记（不改 status、
    无需迁移）+ 写一条 warning 日志，供运维 / 下轮恢复识别。
    """
    db.execute(
        sa_update(PublishRecord)
        .where(
            PublishRecord.id == record_id,
            PublishRecord.is_deleted == False,  # noqa: E712
        )
        .values(queue_reason=_ZOMBIE_QUEUE_REASON)
    )
    add_log(db, task_id, record_id, "warning", _ZOMBIE_QUEUE_REASON)


def _handle_timed_out_record(
    db: Session,
    task_id: int,
    running_record: RunningRecord,
    future: Future,
    *,
    result_timeout: float = _THREAD_TERMINATION_TIMEOUT,
) -> bool:
    """处置执行超时的记录：标 failed → 关 Chromium context 信号线程退出 → 等线程终止。

    - 线程在 result_timeout 内确认终止：释放 profile 锁 + 退场（归还闸槽 + 账号锁）。
    - 线程仍存活（卡 IO 未响应 context 关闭，`result` 抛 FutureTimeoutError）：账号锁 + profile 锁 +
      闸槽**一律不释放**——避免下一条同账号记录对同一 persistent profile 并发开 Chromium 损坏目录
      （#2）；记录标「僵尸待清」+ 告警，交下轮恢复回收。

    返回线程是否已确认终止。
    """
    _mark_record_failed(
        db,
        task_id,
        running_record.record_id,
        f"Timeout: record execution exceeded {int(_record_execution_budget())}s",
    )
    # 关 Chromium context（让 Playwright 线程收到 TargetClosedError 终止）+ 清会话映射；
    # profile 锁先不放，等下方确认线程终止。
    try:
        _close_record_browser(running_record.record_id)
    except Exception:
        _logger.warning(
            "Failed to stop session for timed-out record %d",
            running_record.record_id,
            exc_info=True,
        )
    future.cancel()
    try:
        future.result(timeout=result_timeout)
        terminated = True
    except FutureTimeoutError:
        terminated = False
    except Exception:
        # 线程已终止（抛业务异常 / 被 cancel）——视为已退场
        terminated = True

    if terminated:
        _release_record_profile_lock(running_record.record_id)
        _retire_running_slot(running_record)
    else:
        _mark_record_zombie(db, task_id, running_record.record_id)
        emit_resource_alert(
            f"record {running_record.record_id} publish thread still alive after "
            f"{result_timeout:g}s; account/profile locks held, leaving for recovery",
            {"record_id": running_record.record_id, "account_id": running_record.account_id},
        )
    return terminated


def _claim_record(db: Session, task_id: int, record: PublishRecord) -> bool:
    """乐观锁认领记录：pending→running 条件 UPDATE，rowcount==1 才算抢到（返回 True）。

    遇可重试的 DB 行锁/死锁错误（1205/1213/1684）回滚返回 False，让上层下轮再试。
    """
    now = utcnow()
    lease_until = now + timedelta(seconds=_record_execution_budget() + 60)
    stmt = (
        sa_update(PublishRecord)
        .where(
            PublishRecord.id == record.id,
            PublishRecord.status == "pending",
            PublishRecord.is_deleted == False,  # noqa: E712
        )
        .values(status="running", started_at=now, lease_until=lease_until, queue_reason=None)
    )
    try:
        rowcount = db.execute(stmt).rowcount  # type: ignore[attr-defined]  # DML 执行返回 CursorResult
    except OperationalError as exc:
        if _is_retryable_db_lock_error(exc):
            db.rollback()
            return False
        raise
    if rowcount == 0:
        db.commit()
        return False
    record.status = "running"
    record.queue_reason = None
    record.started_at = now
    record.lease_until = lease_until
    add_log(db, task_id, record.id, "info", f"Record {record.id} started")
    return True


def _is_retryable_db_lock_error(exc: OperationalError) -> bool:
    original = getattr(exc, "orig", None)
    code = None
    if original is not None and getattr(original, "args", None):
        code = original.args[0]
    return code in {1205, 1213, 1684}


def _load_article_for_publish(db: Session, article_id: int) -> Article | None:
    return db.execute(
        select(Article)
        .where(Article.id == article_id, Article.is_deleted == False)  # noqa: E712
        .options(
            selectinload(Article.cover_asset),
            selectinload(Article.body_assets).selectinload(ArticleBodyAsset.asset),
        )
    ).scalar_one_or_none()


def _validate_record_inputs(article: Article | None, account: Account | None) -> str | None:
    if article is None or account is None:
        return "Record article or account not found"
    if getattr(account, "is_deleted", False):
        return "Record account has been deleted"
    if not article.title or not article.title.strip():
        return "文章标题不能为空"
    if not has_publishable_body(article):
        return "文章正文不能为空"
    if article.cover_asset_id is None:
        return "文章封面不能为空"
    if account.status != "valid":
        return (
            f"Account {account.id} is {account.status}: please re-verify the account authorization"
        )
    return None


def _detach_record_inputs(
    db: Session, record: PublishRecord, article: Article, account: Account
) -> None:
    """把记录/文章/账号及关联资源从 session expunge，供发布线程脱离 db 使用。

    expunge 前已 selectinload 的关系才安全；检测到关键关系（cover_asset/body_assets/asset）
    仍 unloaded 就直接抛 RuntimeError——detached 对象再触发懒加载会因无 session 炸在发布线程里。
    """
    # build_publish_runner_for_record 在发布线程读 record.platform.code 判 API/浏览器驱动；
    # detach 后该关系无法再懒加载（DetachedInstanceError），趁仍绑定 session 先把它加载进实例（见 PR#70 回归）。
    if record.platform is not None:
        _ = record.platform.code

    objects: list[object] = [record, article, account]
    if article.cover_asset is not None:
        objects.append(article.cover_asset)
    for link in article.body_assets:
        objects.append(link)
        if link.asset is not None:
            objects.append(link.asset)
    for obj in objects:
        if obj in db:
            db.expunge(obj)

    for obj in objects:
        if isinstance(obj, PublishRecord):
            continue
        insp = sa_inspect(obj)
        if insp is None:
            continue
        unloaded = insp.unloaded
        if isinstance(obj, Article):
            if "cover_asset" in unloaded or "body_assets" in unloaded:
                raise RuntimeError(
                    f"Detached Article has unloaded attributes: {unloaded}. "
                    f"Add selectinload to _load_article_for_publish or _detach_record_inputs."
                )
        elif isinstance(obj, ArticleBodyAsset):
            if "asset" in unloaded:
                raise RuntimeError(
                    f"Detached ArticleBodyAsset has unloaded attributes: {unloaded}. "
                    f"Add selectinload to _load_article_for_publish or _detach_record_inputs."
                )


def _record_execution_budget() -> float:
    """每条记录的执行预算（秒）。开启发布前延迟时按最大延迟加宽，
    避免延迟把执行时间撞上 publish_record_timeout_seconds 硬墙。"""
    s = get_settings()
    extra = s.publish_pre_delay_max_seconds if s.publish_pre_delay_enabled else 0.0
    return s.publish_record_timeout_seconds + extra


def _maybe_pre_publish_delay(
    record: PublishRecord,
    stop_before_publish: bool,
    *,
    sleep=time.sleep,
    rng=random.uniform,
) -> None:
    """发布前随机延迟（错峰防封）。stop_before_publish 的人工确认流程跳过。
    sleep / rng 作为可注入参数，便于测试零等待。"""
    if stop_before_publish:
        return
    s = get_settings()
    if not s.publish_pre_delay_enabled:
        return
    lo = max(0.0, s.publish_pre_delay_min_seconds)
    hi = max(lo, s.publish_pre_delay_max_seconds)
    if hi <= 0:
        return
    delay = rng(lo, hi)
    _logger.info("Pre-publish delay %.1fs for record %d", delay, record.id)
    sleep(delay)


def _publish_record(
    record: PublishRecord, article: Article, account: Account, stop_before_publish: bool
):
    """跑在线程池里的实际发布：构建 runner 调驱动，全程不碰 db（入参均为 detached ORM 对象）。

    Task 4 Step 5/#8：全局并发封顶已由主线程在 submit 前用 `_global_publish_gate.try_acquire()`
    把关，本函数**不再** acquire/release 闸——排队不再占记录执行预算（watchdog）、不在发布线程阻塞。
    诊断事件挂到异常的 publish_diagnostics 属性上随 raise 带回主线程持久化。
    """
    _logger.info(
        "Publishing record %d for article %d to account %d", record.id, article.id, account.id
    )
    diagnostics: list[PublishDiagnosticEvent] = []
    try:
        with capture_publish_diagnostics(diagnostics):
            _maybe_pre_publish_delay(record, stop_before_publish)
            runner = build_publish_runner_for_record(record)
            result = runner(article, account, stop_before_publish=stop_before_publish)
            return RecordPublishOutcome(result=result, diagnostics=list(diagnostics))
    except Exception as exc:
        exc.publish_diagnostics = list(diagnostics)  # type: ignore[attr-defined]  # 动态属性，用于随异常传递诊断
        raise


def _finish_record_future(db: Session, task: PublishTask, record_id: int, future: Future) -> None:
    """在主线程把一条发布 future 的结果写回 DB，按异常类型分流。

    成功（else 分支）：stop_before_publish→waiting_manual_publish，否则→succeeded 并收尾会话。
    UserInputRequired→waiting_user_input（保留会话供 noVNC 接管）；PublishError/ValueError/其它→failed
    并截图存证 + 停会话。所有状态写回都用 status='running' 条件 UPDATE，rowcount=0 说明状态被外部改过。
    """
    try:
        outcome = future.result()
        if isinstance(outcome, RecordPublishOutcome):
            _add_publish_diagnostics(db, task.id, record_id, outcome.diagnostics, task.user_id)
            result = outcome.result
        else:
            result = outcome
    except FutureTimeoutError:
        _mark_record_failed(
            db,
            task.id,
            record_id,
            f"Timeout: record execution exceeded {int(_record_execution_budget())}s",
        )
        _stop_record_session(record_id)
        _logger.warning("Record %d timed out", record_id)
    except UserInputRequired as exc:
        try:
            _add_publish_diagnostics(
                db, task.id, record_id, _diagnostics_from_exception(exc), task.user_id
            )
            screenshot_asset_id = _store_failure_screenshot(
                db, task.id, record_id, exc.screenshot, task.user_id
            )
            error_type = getattr(exc, "error_type", "login_required")
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
    except PublishError as exc:
        try:
            _add_publish_diagnostics(
                db, task.id, record_id, _diagnostics_from_exception(exc), task.user_id
            )
            screenshot_asset_id = _store_failure_screenshot(
                db, task.id, record_id, exc.screenshot, task.user_id
            )
            _mark_record_failed(
                db,
                task.id,
                record_id,
                f"{exc}\n{traceback.format_exc()}",
                screenshot_asset_id=screenshot_asset_id,
            )
            _stop_record_session(record_id)
            _logger.error("Record %d publish error: %s", record_id, exc)
        except Exception as _inner:
            _logger.error(
                "Record %d: error handling PublishError: %s", record_id, _inner, exc_info=True
            )
            _mark_record_failed(db, task.id, record_id, f"Error handling publish error: {_inner}")
            _stop_record_session(record_id)
    except ValueError as exc:
        try:
            _add_publish_diagnostics(
                db, task.id, record_id, _diagnostics_from_exception(exc), task.user_id
            )
            _mark_record_failed(db, task.id, record_id, f"{exc}\n{traceback.format_exc()}")
            _stop_record_session(record_id)
            _logger.error("Record %d value error: %s", record_id, exc)
        except Exception as _inner:
            _logger.error(
                "Record %d: error handling ValueError: %s", record_id, _inner, exc_info=True
            )
            _mark_record_failed(db, task.id, record_id, f"Error handling value error: {_inner}")
            _stop_record_session(record_id)
    except Exception as exc:
        try:
            _add_publish_diagnostics(
                db, task.id, record_id, _diagnostics_from_exception(exc), task.user_id
            )
            _mark_record_failed(
                db, task.id, record_id, f"Unexpected error: {exc}\n{traceback.format_exc()}"
            )
            _stop_record_session(record_id)
            _logger.error("Record %d unexpected error", record_id, exc_info=True)
        except Exception as _inner:
            _logger.error(
                "Record %d: error handling unexpected error: %s", record_id, _inner, exc_info=True
            )
            _mark_record_failed(
                db, task.id, record_id, f"Error handling unexpected error: {_inner}"
            )
            _stop_record_session(record_id)
    else:
        if task.stop_before_publish:
            stmt = (
                sa_update(PublishRecord)
                .where(
                    PublishRecord.id == record_id,
                    PublishRecord.status == "running",
                    PublishRecord.is_deleted == False,  # noqa: E712
                )
                .values(
                    status="waiting_manual_publish",
                    finished_at=utcnow(),
                    lease_until=None,
                    queue_reason=None,
                )
            )
            message = "等待手动确认发布"
        else:
            stmt = (
                sa_update(PublishRecord)
                .where(
                    PublishRecord.id == record_id,
                    PublishRecord.status == "running",
                    PublishRecord.is_deleted == False,  # noqa: E712
                )
                .values(
                    status="succeeded",
                    publish_url=result.url or None,
                    finished_at=utcnow(),
                    lease_until=None,
                    queue_reason=None,
                )
            )
            message = result.message
        if db.execute(stmt).rowcount > 0:  # type: ignore[attr-defined]  # DML 执行返回 CursorResult
            add_log(db, task.id, record_id, "info", message)
            if not task.stop_before_publish:
                _stop_record_session(record_id)
        else:
            _logger.warning(
                "Record %d publish succeeded on platform but DB update had rowcount=0 "
                "(status was changed externally)",
                record_id,
            )
        _logger.info("Record %d succeeded", record_id)


def _diagnostics_from_exception(exc: BaseException) -> list[PublishDiagnosticEvent]:
    diagnostics = getattr(exc, "publish_diagnostics", [])
    return diagnostics if isinstance(diagnostics, list) else []


def _add_publish_diagnostics(
    db: Session,
    task_id: int,
    record_id: int,
    diagnostics: list[PublishDiagnosticEvent],
    user_id: int,
) -> None:
    for event in diagnostics:
        level = event.level if event.level in {"info", "warn", "error"} else "info"
        screenshot_asset_id = _store_failure_screenshot(
            db, task_id, record_id, event.screenshot, user_id
        )
        add_log(
            db,
            task_id,
            record_id,
            level,
            f"[publish diagnostic] {event.message}",
            screenshot_asset_id=screenshot_asset_id,
        )


def _mark_record_failed(
    db: Session,
    task_id: int,
    record_id: int,
    error_message: str,
    screenshot_asset_id: str | None = None,
) -> None:
    stmt = (
        sa_update(PublishRecord)
        .where(
            PublishRecord.id == record_id,
            PublishRecord.status == "running",
            PublishRecord.is_deleted == False,  # noqa: E712
        )
        .values(
            status="failed",
            error_message=error_message,
            finished_at=utcnow(),
            lease_until=None,
            queue_reason=None,
        )
    )
    if db.execute(stmt).rowcount > 0:  # type: ignore[attr-defined]  # DML 执行返回 CursorResult
        add_log(
            db, task_id, record_id, "error", error_message, screenshot_asset_id=screenshot_asset_id
        )


def _mark_record_waiting_user_input(
    db: Session,
    task_id: int,
    record_id: int,
    message: str,
    screenshot_asset_id: str | None = None,
) -> None:
    stmt = (
        sa_update(PublishRecord)
        .where(
            PublishRecord.id == record_id,
            PublishRecord.status == "running",
            PublishRecord.is_deleted == False,  # noqa: E712
        )
        .values(
            status="waiting_user_input",
            error_message=message,
            finished_at=None,
            lease_until=None,
            queue_reason=None,
        )
    )
    if db.execute(stmt).rowcount > 0:  # type: ignore[attr-defined]  # DML 执行返回 CursorResult
        add_log(db, task_id, record_id, "warn", message, screenshot_asset_id=screenshot_asset_id)


def _store_failure_screenshot(
    db: Session,
    task_id: int,
    record_id: int,
    screenshot: bytes | None,
    user_id: int,
) -> str | None:
    if not screenshot:
        return None
    # 懒导入：避免 articles 包 ↔ tasks 包的包级循环依赖（见 CLAUDE.md）
    from server.app.modules.articles import store_bytes

    stored = store_bytes(
        db,
        user_id,
        screenshot,
        filename=f"task-{task_id}-record-{record_id}-failure.png",
        content_type="image/png",
    )
    return stored.asset.id


def build_publish_runner_for_record(record: PublishRecord):
    """构造该记录的发布闭包：预绑 record_id + 浏览器 channel/可执行路径，返回 (article, account) → PublishResult。

    API 型平台（驱动 mode='api'，如公众号）分叉到 runner_api（无浏览器）；
    浏览器平台驱动选择仍在 runner.run_publish 内按账号 state_path 的 platform_code 决定。
    懒导入 runner 避免循环依赖。
    """
    from server.app.modules.tasks.drivers import (
        is_api_driver,
        is_driver_registered,
        resolve_driver,
    )

    platform_code = record.platform.code if record.platform is not None else None
    # 驱动未在本进程注册时（多为某进程漏 import，见 drivers/bootstrap.py）显式报错，
    # 不要静默回退浏览器 run_publish——那会把根因伪装成误导性的「需要 storage_state」。
    if platform_code and not is_driver_registered(platform_code):
        from server.app.modules.tasks.drivers.base import PublishError

        raise PublishError(
            f"平台 {platform_code!r} 的发布驱动未在本进程注册；"
            "请确认进程已 import server.app.modules.tasks.drivers.bootstrap"
        )

    if platform_code and is_api_driver(platform_code):
        from server.app.modules.tasks.runner_api import run_publish_api

        driver = resolve_driver(platform_code)

        def _api_runner(article, account, *, stop_before_publish=False):
            # platform_code（=record.platform.code）显式传入，避免发布线程懒加载 detached account.platform（#90）
            return run_publish_api(
                article=article, account=account, driver=driver, platform_code=platform_code
            )

        return _api_runner

    from server.app.modules.tasks.runner import run_publish

    settings = get_settings()
    channel = settings.publish_browser_channel
    executable_path = settings.publish_browser_executable_path
    _record_id = record.id

    def _runner(article, account, *, stop_before_publish=False):
        return run_publish(
            record_id=_record_id,
            article=article,
            account=account,
            channel=channel,
            executable_path=executable_path,
            stop_before_publish=stop_before_publish,
        )

    return _runner


def cancel_task(db: Session, task: PublishTask) -> PublishTask:
    """请求取消任务：set 进程内取消事件 + 立刻取消所有非 running 记录。

    在跑的 running 记录不强杀，留到执行循环的下一个安全点收口（此时 task 仍保持 running）；
    若已无 running 记录则直接条件 UPDATE 把 task 置 cancelled。
    """
    if task.status in TERMINAL_TASK_STATUSES:
        return task

    evt = _task_cancel.get(task.id)
    if evt:
        evt.set()

    now = utcnow()
    records = list_task_records(db, task.id)
    task.cancel_requested = True
    _cancel_not_running_records(db, task, records)

    refreshed_records = list_task_records(db, task.id)
    if not any(record.status == "running" for record in refreshed_records):
        result = db.execute(
            sa_update(PublishTask)
            .where(
                PublishTask.id == task.id,
                PublishTask.status.not_in(TERMINAL_TASK_STATUSES),
                PublishTask.is_deleted == False,  # noqa: E712
            )
            .values(status="cancelled", finished_at=now)
        )
        rows = result.rowcount  # type: ignore[attr-defined]  # DML 执行返回 CursorResult
        if rows > 0:
            task.status = "cancelled"
            task.finished_at = now
            add_log(db, task.id, None, "warn", "Task cancelled")
    else:
        task.status = "running"
        add_log(
            db,
            task.id,
            None,
            "warn",
            "Cancellation requested; running record will finish at its next safe point",
        )
    db.flush()

    return get_task(db, task.id) or task
