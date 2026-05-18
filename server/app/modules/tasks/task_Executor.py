from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import inspect as sa_inspect, select, update as sa_update
from sqlalchemy.orm import Session, selectinload

from server.app.core.config import get_settings
from server.app.core.time import utcnow
from server.app.models import Account, Article, ArticleBodyAsset, PublishRecord, PublishTask
from server.app.modules.articles.tiptap_Parser import has_publishable_body
from server.app.modules.tasks.task_Crud import (
    TERMINAL_TASK_STATUSES,
    add_log,
    aggregate_task_status,
    get_task,
    list_task_records,
)
from server.app.modules.accounts import (
    associate_record_with_session,
    disassociate_record,
    get_session_for_record,
    stop_remote_browser_session,
)
from server.app.modules.articles import store_bytes
from server.app.modules.tasks.drivers.driver_Base import PublishError, UserInputRequired
from server.app.shared.diagnostics import PublishDiagnosticEvent, capture_publish_diagnostics
from server.app.shared.errors import ConflictError

MAX_CONCURRENT_RECORDS = 5
WORKER_LEASE_EXTENSION_SECONDS = 600

_task_locks: dict[int, threading.Lock] = {}
_account_locks: dict[int, threading.Lock] = {}
_account_locks_lock = threading.Lock()
_global_publish_sem = threading.Semaphore(MAX_CONCURRENT_RECORDS)
_task_cancel: dict[int, threading.Event] = {}

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunningRecord:
    record_id: int
    account_id: int
    started_monotonic: float


@dataclass(frozen=True)
class RecordPublishOutcome:
    result: object
    diagnostics: list[PublishDiagnosticEvent]


def _max_concurrent_records() -> int:
    return max(1, min(int(get_settings().publish_max_concurrent_records), MAX_CONCURRENT_RECORDS))


def execute_task(db: Session, task: PublishTask) -> PublishTask:
    lock = _task_locks.setdefault(task.id, threading.Lock())
    locked = lock.acquire(blocking=False)
    if not locked:
        raise ConflictError(f"Task {task.id} is already being executed")

    cancel_event = threading.Event()
    _task_cancel[task.id] = cancel_event

    try:
        if task.status in TERMINAL_TASK_STATUSES:
            raise ConflictError(f"Task is already terminal: {task.status}")

        now = utcnow()
        if task.status == "pending":
            stmt = (
                sa_update(PublishTask)
                .where(PublishTask.id == task.id, PublishTask.status == "pending")
                .values(
                    status="running",
                    started_at=now,
                    cancel_requested=False,
                    worker_heartbeat_at=now,
                )
            )
            if db.execute(stmt).rowcount == 0:
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
    now = utcnow()
    values: dict[str, object] = {"worker_heartbeat_at": now}
    if os.environ.get("GEO_WORKER_ID"):
        values["worker_lease_until"] = now + timedelta(seconds=WORKER_LEASE_EXTENSION_SECONDS)
    db.execute(sa_update(PublishTask).where(PublishTask.id == task_id).values(**values))


def _task_cancel_requested(db: Session, task_id: int) -> bool:
    value = db.execute(select(PublishTask.cancel_requested).where(PublishTask.id == task_id)).scalar_one_or_none()
    return bool(value)


def _request_task_cancel(db: Session, task_id: int) -> None:
    db.execute(sa_update(PublishTask).where(PublishTask.id == task_id).values(cancel_requested=True))


def _cancel_not_running_records(db: Session, task: PublishTask, records: list[PublishRecord]) -> None:
    now = utcnow()
    changed = False
    for record in records:
        if record.status not in {"pending", "waiting_manual_publish", "waiting_user_input"}:
            continue
        if record.status == "waiting_user_input":
            session = get_session_for_record(record.id)
            if session:
                stop_remote_browser_session(session.id)
            disassociate_record(record.id)
        record.status = "cancelled"
        record.finished_at = now
        record.lease_until = None
        changed = True
    if changed:
        add_log(db, task.id, None, "warn", "Cancellation requested; pending records were stopped")


def _run_pending_records(db: Session, task: PublishTask) -> None:
    cancel_evt = _task_cancel.get(task.id)
    running: dict[Future, RunningRecord] = {}
    executor = ThreadPoolExecutor(max_workers=_max_concurrent_records())

    try:
        while True:
            _heartbeat_task_worker(db, task.id)
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
                if task.stop_before_publish:
                    if any(record.status == "waiting_manual_publish" for record in records):
                        db.commit()
                        return

                if any(record.status == "waiting_user_input" for record in records):
                    db.commit()
                    return

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
                if time.monotonic() - running_record.started_monotonic > get_settings().publish_record_timeout_seconds
            ]
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
                _finish_record_future(db, task, running_record.record_id, future)
                _release_account_lock(running_record.account_id)
                db.commit()
    finally:
        for running_record in running.values():
            _release_account_lock(running_record.account_id)
        executor.shutdown(wait=False, cancel_futures=True)


def _start_runnable_records(
    db: Session,
    task: PublishTask,
    executor: ThreadPoolExecutor,
    running: dict[Future, RunningRecord],
    records: list[PublishRecord],
) -> None:
    running_accounts = {item.account_id for item in running.values()}
    blocked_accounts: set[int] = set()
    slots = _max_concurrent_records() - len(running)
    if task.stop_before_publish:
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

        if not _try_acquire_account_lock(next_record.account_id):
            blocked_accounts.add(next_record.account_id)
            continue

        try:
            if not _claim_record(db, task.id, next_record):
                _release_account_lock(next_record.account_id)
                continue

            article = _load_article_for_publish(db, next_record.article_id)
            account = db.get(Account, next_record.account_id)
            validation_error = _validate_record_inputs(article, account)
            if validation_error:
                _mark_record_failed(db, task.id, next_record.id, validation_error)
                _release_account_lock(next_record.account_id)
                db.commit()
                continue

            _detach_record_inputs(db, next_record, article, account)
            future = executor.submit(_publish_record, next_record, article, account, task.stop_before_publish)
            running[future] = RunningRecord(next_record.id, next_record.account_id, time.monotonic())
            running_accounts.add(next_record.account_id)
            slots -= 1
            db.commit()
        except Exception:
            _release_account_lock(next_record.account_id)
            raise


def _try_acquire_account_lock(account_id: int) -> bool:
    with _account_locks_lock:
        lock = _account_locks.setdefault(account_id, threading.Lock())
    return lock.acquire(blocking=False)


def _release_account_lock(account_id: int) -> None:
    lock = _account_locks.get(account_id)
    if lock is not None and lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass


def _claim_record(db: Session, task_id: int, record: PublishRecord) -> bool:
    now = utcnow()
    lease_until = now + timedelta(seconds=get_settings().publish_record_timeout_seconds + 60)
    stmt = (
        sa_update(PublishRecord)
        .where(PublishRecord.id == record.id, PublishRecord.status == "pending")
        .values(status="running", started_at=now, lease_until=lease_until)
    )
    if db.execute(stmt).rowcount == 0:
        db.commit()
        return False
    record.status = "running"
    record.started_at = now
    record.lease_until = lease_until
    add_log(db, task_id, record.id, "info", f"Record {record.id} started")
    return True


def _load_article_for_publish(db: Session, article_id: int) -> Article | None:
    return db.execute(
        select(Article)
        .where(Article.id == article_id)
        .options(
            selectinload(Article.cover_asset),
            selectinload(Article.body_assets).selectinload(ArticleBodyAsset.asset),
        )
    ).scalar_one_or_none()


def _validate_record_inputs(article: Article | None, account: Account | None) -> str | None:
    if article is None or account is None:
        return "Record article or account not found"
    if not article.title or not article.title.strip():
        return "文章标题不能为空"
    if not has_publishable_body(article):
        return "文章正文不能为空"
    if article.cover_asset_id is None:
        return "文章封面不能为空"
    if account.status != "valid":
        return f"Account is not valid: {account.id}"
    return None


def _detach_record_inputs(db: Session, record: PublishRecord, article: Article, account: Account) -> None:
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


def _publish_record(record: PublishRecord, article: Article, account: Account, stop_before_publish: bool):
    _logger.info("Publishing record %d for article %d to account %d", record.id, article.id, account.id)
    _global_publish_sem.acquire()
    diagnostics: list[PublishDiagnosticEvent] = []
    try:
        with capture_publish_diagnostics(diagnostics):
            runner = build_publish_runner_for_record(record)
            result = runner(article, account, stop_before_publish=stop_before_publish)
            return RecordPublishOutcome(result=result, diagnostics=list(diagnostics))
    except BaseException as exc:
        setattr(exc, "publish_diagnostics", list(diagnostics))
        raise
    finally:
        _global_publish_sem.release()


def _finish_record_future(db: Session, task: PublishTask, record_id: int, future: Future) -> None:
    try:
        outcome = future.result()
        if isinstance(outcome, RecordPublishOutcome):
            _add_publish_diagnostics(db, task.id, record_id, outcome.diagnostics, task.user_id)
            result = outcome.result
        else:
            result = outcome
    except FutureTimeoutError:
        _mark_record_failed(db, task.id, record_id, "Timeout: record execution exceeded 300s")
        _logger.warning("Record %d timed out", record_id)
    except UserInputRequired as exc:
        _add_publish_diagnostics(db, task.id, record_id, _diagnostics_from_exception(exc), task.user_id)
        screenshot_asset_id = _store_failure_screenshot(db, task.id, record_id, exc.screenshot, task.user_id)
        error_type = getattr(exc, "error_type", "login_required")
        type_label = {"login_required": "需要登录", "captcha_required": "需要验证码", "qr_scan_required": "需要扫码"}.get(error_type, "需要人工操作")
        _mark_record_waiting_user_input(db, task.id, record_id, f"[{type_label}] {exc}\n{traceback.format_exc()}", screenshot_asset_id=screenshot_asset_id)
        if exc.session_id:
            associate_record_with_session(record_id, exc.session_id)
        _logger.info("Record %d waiting user input (type=%s)", record_id, error_type)
    except PublishError as exc:
        _add_publish_diagnostics(db, task.id, record_id, _diagnostics_from_exception(exc), task.user_id)
        screenshot_asset_id = _store_failure_screenshot(db, task.id, record_id, exc.screenshot, task.user_id)
        _mark_record_failed(db, task.id, record_id, f"{exc}\n{traceback.format_exc()}", screenshot_asset_id=screenshot_asset_id)
        _logger.error("Record %d publish error: %s", record_id, exc)
    except ValueError as exc:
        _add_publish_diagnostics(db, task.id, record_id, _diagnostics_from_exception(exc), task.user_id)
        _mark_record_failed(db, task.id, record_id, f"{exc}\n{traceback.format_exc()}")
        _logger.error("Record %d value error: %s", record_id, exc)
    except Exception as exc:
        _add_publish_diagnostics(db, task.id, record_id, _diagnostics_from_exception(exc), task.user_id)
        _mark_record_failed(db, task.id, record_id, f"Unexpected error: {exc}\n{traceback.format_exc()}")
        _logger.error("Record %d unexpected error", record_id, exc_info=True)
    else:
        if task.stop_before_publish:
            stmt = (
                sa_update(PublishRecord)
                .where(PublishRecord.id == record_id, PublishRecord.status == "running")
                .values(status="waiting_manual_publish", finished_at=utcnow(), lease_until=None)
            )
            message = "等待手动确认发布"
        else:
            stmt = (
                sa_update(PublishRecord)
                .where(PublishRecord.id == record_id, PublishRecord.status == "running")
                .values(status="succeeded", publish_url=result.url or None, finished_at=utcnow(), lease_until=None)
            )
            message = result.message
        if db.execute(stmt).rowcount > 0:
            add_log(db, task.id, record_id, "info", message)
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
        screenshot_asset_id = _store_failure_screenshot(db, task_id, record_id, event.screenshot, user_id)
        add_log(db, task_id, record_id, level, f"[publish diagnostic] {event.message}", screenshot_asset_id=screenshot_asset_id)


def _mark_record_failed(
    db: Session,
    task_id: int,
    record_id: int,
    error_message: str,
    screenshot_asset_id: str | None = None,
) -> None:
    stmt = (
        sa_update(PublishRecord)
        .where(PublishRecord.id == record_id, PublishRecord.status == "running")
        .values(status="failed", error_message=error_message, finished_at=utcnow(), lease_until=None)
    )
    if db.execute(stmt).rowcount > 0:
        add_log(db, task_id, record_id, "error", error_message, screenshot_asset_id=screenshot_asset_id)


def _mark_record_waiting_user_input(
    db: Session,
    task_id: int,
    record_id: int,
    message: str,
    screenshot_asset_id: str | None = None,
) -> None:
    stmt = (
        sa_update(PublishRecord)
        .where(PublishRecord.id == record_id, PublishRecord.status == "running")
        .values(status="waiting_user_input", error_message=message, finished_at=None, lease_until=None)
    )
    if db.execute(stmt).rowcount > 0:
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
    stored = store_bytes(
        db,
        user_id,
        screenshot,
        filename=f"task-{task_id}-record-{record_id}-failure.png",
        content_type="image/png",
    )
    return stored.asset.id


def build_publish_runner_for_record(record: PublishRecord):
    from server.app.modules.tasks.publish_Runner import run_publish
    settings = get_settings()
    channel = settings.publish_browser_channel
    executable_path = settings.publish_browser_executable_path

    def _runner(article, account, *, stop_before_publish=False):
        return run_publish(
            article=article,
            account=account,
            channel=channel,
            executable_path=executable_path,
            stop_before_publish=stop_before_publish,
        )

    return _runner


def cancel_task(db: Session, task: PublishTask) -> PublishTask:
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
        task.status = "cancelled"
        task.finished_at = now
        add_log(db, task.id, None, "warn", "Task cancelled")
    else:
        task.status = "running"
        add_log(db, task.id, None, "warn", "Cancellation requested; running record will finish at its next safe point")
    db.flush()

    return get_task(db, task.id) or task
