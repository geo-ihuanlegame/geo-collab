from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session, selectinload

from server.app.core.time import utcnow
from server.app.modules.accounts.models import Account
from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
from server.app.modules.system.models import Platform
from server.app.modules.tasks.models import PublishRecord, PublishTask, PublishTaskAccount, TaskLog
from server.app.modules.tasks.schemas import (
    TaskAccountInput,
    TaskAssignmentPreviewItemRead,
    TaskAssignmentPreviewRead,
    TaskCreate,
)
from server.app.shared.errors import AccountError, ClientError, ValidationError

_logger = logging.getLogger(__name__)

VALID_TASK_TYPES = {"single", "group_round_robin", "article_round_robin"}
TERMINAL_TASK_STATUSES = {"succeeded", "partial_failed", "failed", "cancelled"}
PAUSED_RECORD_STATUSES = {"waiting_manual_publish", "waiting_user_input"}
ACTIVE_RECORD_STATUSES = {"running", *PAUSED_RECORD_STATUSES}
CAN_RETRY_TASK_STATUSES = {"failed", "partial_failed", "succeeded", "cancelled"}


@dataclass(frozen=True)
class TaskInputs:
    platform: Platform
    accounts: list[tuple[int, Account]]
    article_ids: list[int]


@dataclass(frozen=True)
class AssignmentItem:
    position: int
    article_id: int
    account_sort_order: int
    account: Account


def list_tasks(
    db: Session, skip: int = 0, limit: int = 100, user_id: int | None = None
) -> list[PublishTask]:
    stmt = (
        select(PublishTask)
        .options(
            selectinload(PublishTask.platform),
            selectinload(PublishTask.accounts).selectinload(PublishTaskAccount.account),
            selectinload(PublishTask.records),
        )
        .where(PublishTask.is_deleted == False)  # noqa: E712
        .order_by(PublishTask.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if user_id is not None:
        stmt = stmt.where(PublishTask.user_id == user_id)
    return list(db.execute(stmt).scalars().all())


def get_task(db: Session, task_id: int) -> PublishTask | None:
    stmt = (
        select(PublishTask)
        .where(PublishTask.id == task_id, PublishTask.is_deleted == False)  # noqa: E712
        .options(
            selectinload(PublishTask.platform),
            selectinload(PublishTask.accounts).selectinload(PublishTaskAccount.account),
            selectinload(PublishTask.records),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def list_task_records(db: Session, task_id: int) -> list[PublishRecord]:
    stmt = (
        select(PublishRecord)
        .where(PublishRecord.task_id == task_id, PublishRecord.is_deleted == False)  # noqa: E712
        .order_by(PublishRecord.id.asc())
    )
    return list(db.execute(stmt).scalars().all())


def list_task_logs(db: Session, task_id: int, after_id: int = 0, limit: int = 100) -> list[TaskLog]:
    stmt = (
        select(TaskLog)
        .where(TaskLog.task_id == task_id, TaskLog.id > after_id)
        .order_by(TaskLog.created_at.asc(), TaskLog.id.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def delete_all_tasks(db: Session) -> None:
    now = utcnow()
    db.execute(sa_update(PublishRecord).values(is_deleted=True, deleted_at=now))
    db.execute(sa_update(PublishTask).values(is_deleted=True, deleted_at=now))
    db.flush()


def create_task(
    db: Session, user_id: int, payload: TaskCreate, role: str = "operator"
) -> PublishTask:
    if payload.client_request_id:
        existing = db.execute(
            select(PublishTask).where(
                PublishTask.client_request_id == payload.client_request_id,
                PublishTask.is_deleted == False,  # noqa: E712
            )
        ).scalar_one_or_none()
        if existing is not None:
            return get_task(db, existing.id) or existing

    user_id_filter = None if role == "admin" else user_id
    inputs = _validated_task_inputs(db, payload, user_id=user_id_filter)
    assignments = _build_assignments(inputs.article_ids, inputs.accounts)

    task = PublishTask(
        user_id=user_id,
        name=payload.name,
        task_type=payload.task_type,
        status="pending",
        platform_id=inputs.platform.id,
        article_id=payload.article_id if payload.task_type == "single" else None,
        group_id=payload.group_id if payload.task_type == "group_round_robin" else None,
        stop_before_publish=payload.stop_before_publish,
        client_request_id=payload.client_request_id,
        cancel_requested=False,
    )
    db.add(task)
    db.flush()

    for sort_order, account in inputs.accounts:
        task.accounts.append(PublishTaskAccount(account_id=account.id, sort_order=sort_order))

    for assignment in assignments:
        task.records.append(
            PublishRecord(
                article_id=assignment.article_id,
                platform_id=inputs.platform.id,
                account_id=assignment.account.id,
                status="pending",
            )
        )

    db.flush()
    return get_task(db, task.id) or task


def preview_task_assignment(
    db: Session,
    payload: TaskCreate,
    user_id: int | None = None,
    role: str = "operator",
) -> TaskAssignmentPreviewRead:
    user_id_filter = None if role == "admin" else user_id
    inputs = _validated_task_inputs(db, payload, user_id=user_id_filter)
    assignments = _build_assignments(inputs.article_ids, inputs.accounts)
    return TaskAssignmentPreviewRead(
        task_type=payload.task_type,
        platform_code=inputs.platform.code,
        article_count=len(inputs.article_ids),
        account_count=len(inputs.accounts),
        items=[
            TaskAssignmentPreviewItemRead(
                position=assignment.position,
                article_id=assignment.article_id,
                account_id=assignment.account.id,
                account_sort_order=assignment.account_sort_order,
            )
            for assignment in assignments
        ],
    )


def get_record(db: Session, record_id: int) -> PublishRecord | None:
    return db.execute(
        select(PublishRecord).where(
            PublishRecord.id == record_id,
            PublishRecord.is_deleted == False,  # noqa: E712
        )
    ).scalar_one_or_none()


def manual_confirm_record(
    db: Session,
    record: PublishRecord,
    outcome: str,
    publish_url: str | None,
    error_message: str | None,
) -> PublishRecord:
    from server.app.modules.accounts import (
        disassociate_record,
        get_session_for_record,
        release_profile_lock_by_owner,
        stop_remote_browser_session,
    )

    if record.status != "waiting_manual_publish":
        raise ClientError(f"Record is not waiting for manual confirm: {record.status}")
    if outcome not in {"succeeded", "failed"}:
        raise ClientError(f"Invalid outcome: {outcome}")

    session = get_session_for_record(record.id)
    if session:
        stop_remote_browser_session(session.id)
    disassociate_record(record.id)
    release_profile_lock_by_owner(owner_kind="publish", owner_id=record.id)

    record.status = outcome
    record.queue_reason = None
    record.finished_at = utcnow()
    if outcome == "succeeded":
        record.publish_url = str(publish_url) if publish_url else None
        add_log(db, record.task_id, record.id, "info", "Record manually confirmed as succeeded")
    else:
        record.error_message = error_message or "Manually marked as failed"
        add_log(db, record.task_id, record.id, "warn", "Record manually confirmed as failed")

    task = get_task(db, record.task_id)
    if task is not None:
        records = list_task_records(db, task.id)
        aggregate_task_status(db, task, records)

    db.flush()
    return record


def resolve_user_input_record(db: Session, record: PublishRecord) -> PublishRecord:
    from server.app.modules.accounts import (
        disassociate_record,
        get_session_for_record,
        release_profile_lock_by_owner,
        stop_remote_browser_session,
    )

    if record.status != "waiting_user_input":
        raise ClientError(f"Record is not waiting for user input: {record.status}")

    session = get_session_for_record(record.id)
    if session:
        stop_remote_browser_session(session.id)
    disassociate_record(record.id)
    release_profile_lock_by_owner(owner_kind="publish", owner_id=record.id)

    record.status = "pending"
    record.error_message = None
    record.queue_reason = None
    record.started_at = None
    record.finished_at = None
    record.lease_until = None
    add_log(db, record.task_id, record.id, "info", "User input resolved; record requeued")

    task = get_task(db, record.task_id)
    if task is not None and task.status not in TERMINAL_TASK_STATUSES:
        task.status = "running"
        task.finished_at = None
        task.cancel_requested = False

    db.flush()
    return record


def retry_record(db: Session, record: PublishRecord) -> PublishRecord:
    if record.status != "failed":
        raise ClientError(f"Only failed records can be retried: {record.status}")
    if record.retry_of_record_id is not None:
        raise ClientError(
            "Retry records cannot be retried again; create a new task after checking the platform result"
        )

    existing_retry = db.execute(
        select(PublishRecord).where(
            PublishRecord.retry_of_record_id == record.id,
            PublishRecord.is_deleted == False,  # noqa: E712
        )
    ).scalar_one_or_none()
    if existing_retry is not None:
        raise ClientError(f"Record {record.id} already has retry record {existing_retry.id}")

    conflicting_record = db.execute(
        select(PublishRecord)
        .where(
            PublishRecord.task_id == record.task_id,
            PublishRecord.article_id == record.article_id,
            PublishRecord.account_id == record.account_id,
            PublishRecord.id != record.id,
            PublishRecord.is_deleted == False,  # noqa: E712
            PublishRecord.status.in_(
                ["pending", "running", "waiting_manual_publish", "waiting_user_input", "succeeded"]
            ),
        )
        .order_by(PublishRecord.id.asc())
    ).scalar_one_or_none()
    if conflicting_record is not None:
        raise ClientError(
            f"Article/account already has record {conflicting_record.id} in status {conflicting_record.status}"
        )

    new_record = PublishRecord(
        task_id=record.task_id,
        article_id=record.article_id,
        platform_id=record.platform_id,
        account_id=record.account_id,
        status="pending",
        retry_of_record_id=record.id,
    )
    db.add(new_record)

    task = get_task(db, record.task_id)
    if task is not None and task.status in CAN_RETRY_TASK_STATUSES:
        task.status = "running"
        task.finished_at = None
        task.cancel_requested = False
        add_log(db, task.id, None, "info", f"Task reopened for retry of record {record.id}")

    db.flush()
    return new_record


def recover_stuck_records(db: Session) -> None:
    """启动时恢复卡住的记录：status='running' 且 lease_until < utcnow()。"""
    now = utcnow()
    records = list(
        db.execute(
            select(PublishRecord).where(
                PublishRecord.status == "running",
                PublishRecord.lease_until < now,
                PublishRecord.is_deleted == False,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    for record in records:
        record.status = "pending"
        record.lease_until = None
        db.add(
            TaskLog(
                task_id=record.task_id,
                record_id=record.id,
                level="warn",
                message="进程重启：记录在上次运行中意外中断，已重置为等待状态",
            )
        )
    if records:
        _logger.warning("Recovered %d stuck records: %s", len(records), [r.id for r in records])
        db.commit()


def recover_stuck_task_claims(db: Session) -> None:
    """Worker 启动时释放过期的 worker 认领（worker 崩溃导致 lease 过期）。"""
    now = utcnow()
    result = db.execute(
        sa_update(PublishTask)
        .where(
            PublishTask.worker_id.is_not(None),
            PublishTask.worker_lease_until < now,
            PublishTask.is_deleted == False,  # noqa: E712
        )
        .values(worker_id=None, worker_lease_until=None, worker_heartbeat_at=None)
    )
    rows = result.rowcount  # type: ignore[attr-defined]  # DML execute returns CursorResult
    if rows:
        _logger.warning("Released %d expired worker task claims", rows)
        db.commit()


def aggregate_task_status(db: Session, task: PublishTask, records: list[PublishRecord]) -> None:
    from server.app.shared.feishu import notify_task_finished

    now = utcnow()
    if not records:
        task.status = "failed"
        task.finished_at = now
        add_log(db, task.id, None, "warn", "Task finished with status: failed")
        return
    if any(
        r.status in {"pending", "running", "waiting_manual_publish", "waiting_user_input"}
        for r in records
    ):
        return
    if task.cancel_requested or any(r.status == "cancelled" for r in records):
        task.status = "cancelled"
        task.finished_at = now
    elif all(r.status == "succeeded" for r in records):
        task.status = "succeeded"
        task.finished_at = now
    elif any(r.status == "failed" for r in records):
        task.status = (
            "partial_failed" if any(r.status == "succeeded" for r in records) else "failed"
        )
        task.finished_at = now
    if task.status in TERMINAL_TASK_STATUSES:
        add_log(
            db,
            task.id,
            None,
            "info" if task.status == "succeeded" else "warn",
            f"Task finished with status: {task.status}",
        )
        total = len(records)
        succeeded_count = sum(1 for r in records if r.status == "succeeded")
        failed_count = sum(1 for r in records if r.status == "failed")
        notify_task_finished(
            task_name=task.name,
            task_id=task.id,
            status=task.status,
            total=total,
            succeeded=succeeded_count,
            failed=failed_count,
        )


def add_log(
    db: Session,
    task_id: int,
    record_id: int | None,
    level: str,
    message: str,
    screenshot_asset_id: str | None = None,
) -> None:
    db.add(
        TaskLog(
            task_id=task_id,
            record_id=record_id,
            level=level,
            message=message,
            screenshot_asset_id=screenshot_asset_id,
        )
    )


def _validated_task_inputs(
    db: Session, payload: TaskCreate, user_id: int | None = None
) -> TaskInputs:
    if payload.task_type not in VALID_TASK_TYPES:
        raise ValidationError(f"Invalid task_type: {payload.task_type}")

    platform = db.execute(
        select(Platform).where(Platform.code == payload.platform_code)
    ).scalar_one_or_none()
    if platform is None:
        raise ClientError(f"Platform not found: {payload.platform_code}")

    ordered_accounts = _validated_accounts(db, platform.id, payload.accounts, user_id=user_id)
    article_ids = _article_ids_for_task(db, payload, user_id=user_id)
    _validate_unique_articles(article_ids)
    _validate_articles_approved(db, article_ids)

    if payload.task_type == "single" and len(ordered_accounts) != 1:
        raise ValidationError("Single task requires exactly one account")

    return TaskInputs(platform=platform, accounts=ordered_accounts, article_ids=article_ids)


def _build_assignments(
    article_ids: list[int], accounts: list[tuple[int, Account]]
) -> list[AssignmentItem]:
    return [
        AssignmentItem(
            position=index,
            article_id=article_id,
            account_sort_order=accounts[index % len(accounts)][0],
            account=accounts[index % len(accounts)][1],
        )
        for index, article_id in enumerate(article_ids)
    ]


def _validated_accounts(
    db: Session,
    platform_id: int,
    account_inputs: list[TaskAccountInput],
    user_id: int | None = None,
) -> list[tuple[int, Account]]:
    if not account_inputs:
        raise ValidationError("At least one account is required")

    seen: set[int] = set()
    ordered_inputs: list[tuple[int, int]] = []
    for index, item in enumerate(account_inputs):
        if item.account_id in seen:
            raise ValidationError(f"Duplicate account_id: {item.account_id}")
        seen.add(item.account_id)
        ordered_inputs.append(
            (item.sort_order if item.sort_order is not None else index, item.account_id)
        )
    ordered_inputs.sort(key=lambda item: item[0])

    account_ids = [account_id for _, account_id in ordered_inputs]
    accounts = {
        account.id: account
        for account in db.execute(
            select(Account).where(Account.id.in_(account_ids), Account.is_deleted == False)  # noqa: E712
        )
        .scalars()
        .all()
    }
    ordered_accounts: list[tuple[int, Account]] = []
    for sort_order, account_id in ordered_inputs:
        account = accounts.get(account_id)
        if account is None or (user_id is not None and account.user_id != user_id):
            raise AccountError(f"Account not found: {account_id}")
        if account.platform_id != platform_id:
            raise AccountError(f"Account platform mismatch: {account_id}")
        if account.status != "valid":
            raise AccountError(
                f"Account {account_id} is {account.status}: please re-verify the account authorization"
            )
        ordered_accounts.append((sort_order, account))
    return ordered_accounts


def _validate_unique_articles(article_ids: list[int]) -> None:
    if len(article_ids) != len(set(article_ids)):
        raise ValidationError("Duplicate article_id in task assignment")


def _validate_articles_approved(db: Session, article_ids: list[int]) -> None:
    """发布前审核门禁：目标文章必须全部 review_status == 'approved'。"""
    if not article_ids:
        return
    statuses = (
        db.execute(
            select(Article.review_status).where(
                Article.id.in_(article_ids),
                Article.is_deleted == False,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    if any(status != "approved" for status in statuses):
        raise ValidationError("存在未通过审核的文章，无法发布")


def _article_ids_for_task(
    db: Session, payload: TaskCreate, user_id: int | None = None
) -> list[int]:
    if payload.task_type == "single":
        if payload.article_id is None:
            raise ClientError("article_id is required for single task")
        article = db.execute(
            select(Article).where(
                Article.id == payload.article_id,
                Article.is_deleted == False,  # noqa: E712
            )
        ).scalar_one_or_none()
        if article is None or (user_id is not None and article.user_id != user_id):
            raise ClientError(f"Article not found: {payload.article_id}")
        return [payload.article_id]

    if payload.task_type == "article_round_robin":
        ids = list(payload.article_ids or [])
        if not ids:
            raise ClientError("article_ids is required for article_round_robin task")
        rows = db.execute(
            select(Article.id, Article.user_id).where(
                Article.id.in_(ids),
                Article.is_deleted == False,  # noqa: E712
            )
        ).all()
        owner_by_id = {r[0]: r[1] for r in rows}
        for aid in ids:
            if aid not in owner_by_id or (user_id is not None and owner_by_id[aid] != user_id):
                raise ClientError(f"Article not found: {aid}")
        return ids

    if payload.group_id is None:
        raise ClientError("group_id is required for group_round_robin task")
    group = db.execute(
        select(ArticleGroup).where(
            ArticleGroup.id == payload.group_id,
            ArticleGroup.is_deleted == False,  # noqa: E712
        )
    ).scalar_one_or_none()
    if group is None or (user_id is not None and group.user_id != user_id):
        raise ClientError(f"Article group not found: {payload.group_id}")
    items = list(
        db.execute(
            select(ArticleGroupItem)
            .where(ArticleGroupItem.group_id == payload.group_id)
            .order_by(ArticleGroupItem.sort_order.asc())
        )
        .scalars()
        .all()
    )
    if not items:
        raise ValidationError("Article group has no articles")
    article_ids = [item.article_id for item in items]
    active_article_ids = set(
        db.execute(
            select(Article.id).where(
                Article.id.in_(article_ids),
                Article.is_deleted == False,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    missing_ids = [article_id for article_id in article_ids if article_id not in active_article_ids]
    if missing_ids:
        raise ClientError(f"Article not found: {missing_ids[0]}")
    return article_ids
