"""任务模块路由。"""

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy import update as _upd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.system.models import User
from server.app.modules.tasks import (
    TERMINAL_TASK_STATUSES,
    cancel_task,
    create_task,
    execute_task,
    get_record,
    get_task,
    list_task_logs,
    list_task_records,
    list_tasks,
    manual_confirm_record,
    preview_task_assignment,
    resolve_user_input_record,
    retry_record,
)
from server.app.modules.tasks.models import PublishRecord, PublishTask
from server.app.modules.tasks.schemas import (
    AutoDistributeRequest,
    ManualConfirmInput,
    PublishRecordRead,
    TaskAccountInput,
    TaskAssignmentPreviewRead,
    TaskCreate,
    TaskLogRead,
    TaskRead,
    to_log_read,
    to_record_read,
    to_task_read,
)

tasks_router = APIRouter()
publish_records_router = APIRouter()

# 后台任务使用的 Session 工厂（测试时可替换为 TestingSessionLocal）
bg_session_factory: Any = None


# ── Task helpers ──────────────────────────────────────────────────────────────


def _verify_task_ownership(task: PublishTask | None, current_user: User) -> PublishTask:
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if current_user.role != "admin" and task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


# ── Task routes ───────────────────────────────────────────────────────────────


@tasks_router.get("", response_model=list[TaskRead])
def read_tasks(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TaskRead]:
    user_id_filter = None if current_user.role == "admin" else current_user.id
    tasks = list_tasks(db, skip=skip, limit=limit, user_id=user_id_filter)
    return [to_task_read(task) for task in tasks]


@tasks_router.post("", response_model=TaskRead)
def create_task_endpoint(
    payload: TaskCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskRead:
    try:
        new_task = create_task(db, current_user.id, payload, role=current_user.role)
        add_audit_entry(
            db,
            user=current_user,
            action="task.create",
            target_type="task",
            target_id=new_task.id,
            payload={
                "name": getattr(payload, "name", None),
                "platform_code": getattr(payload, "platform_code", None),
                "account_ids": list(getattr(payload, "account_ids", []) or []),
            },
            request=request,
        )
        return to_task_read(new_task)
    except IntegrityError as exc:
        db.rollback()
        if payload.client_request_id:
            existing = db.execute(
                select(PublishTask).where(
                    PublishTask.client_request_id == payload.client_request_id,
                    PublishTask.user_id == current_user.id,
                    PublishTask.is_deleted == False,  # noqa: E712
                )
            ).scalar_one_or_none()
            if existing is not None:
                # Idempotent retry: the concurrent request already created it.
                refreshed = get_task(db, existing.id)
                return to_task_read(refreshed or existing)
        # Any IntegrityError not resolved by the idempotency lookup above is a
        # genuine constraint conflict — surface it as 409, never fall through to
        # an implicit `return None` (which would serialize as an opaque 500).
        raise HTTPException(
            status_code=409, detail="请求冲突：client_request_id 已存在或数据完整性约束失败"
        ) from exc


@tasks_router.post("/auto-distribute", response_model=TaskRead)
def auto_distribute_endpoint(
    payload: AutoDistributeRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskRead:
    is_group = payload.group_id is not None
    target_label = f"分组 {payload.group_id}" if is_group else f"文章 {payload.article_id}"
    task_create = TaskCreate(
        name=payload.name or f"自动分发 {target_label}",
        task_type="group_round_robin" if is_group else "single",
        article_id=payload.article_id,
        group_id=payload.group_id,
        accounts=[
            TaskAccountInput(account_id=account_id, sort_order=index)
            for index, account_id in enumerate(payload.account_ids)
        ],
        stop_before_publish=False,
    )
    # 审核门禁 + 账号有效性校验都在 create_task 内部执行（抛命名异常 → 全局映射 400）。
    new_task = create_task(db, current_user.id, task_create, role=current_user.role)
    db.commit()

    add_audit_entry(
        db,
        user=current_user,
        action="task.auto_distribute",
        target_type="task",
        target_id=new_task.id,
        payload={
            "name": task_create.name,
            "article_id": payload.article_id,
            "group_id": payload.group_id,
            "account_ids": list(payload.account_ids),
        },
        request=request,
    )

    # 触发后台执行（与 POST /api/tasks/{id}/execute 同一条懒加载 bg_session_factory 路径）。
    _start_background_execute(new_task.id)

    return to_task_read(new_task)


@tasks_router.post("/preview", response_model=TaskAssignmentPreviewRead)
def preview_task_assignment_endpoint(
    payload: TaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskAssignmentPreviewRead:
    return preview_task_assignment(db, payload, user_id=current_user.id, role=current_user.role)


class _ExecuteResponse(BaseModel):
    queued: bool


@tasks_router.post("/{task_id}/execute", status_code=202, response_model=_ExecuteResponse)
def start_task_execution(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> _ExecuteResponse:
    from server.app.core.time import utcnow as _utcnow

    task = _verify_task_ownership(get_task(db, task_id), current_user)
    if task.status in TERMINAL_TASK_STATUSES:
        raise HTTPException(status_code=409, detail=f"Task is already terminal: {task.status}")

    if bg_session_factory is not None:
        # Test/dev mode: execute immediately in a background thread
        def _run() -> None:
            bg_db = bg_session_factory()
            try:
                bg_task = get_task(bg_db, task_id)
                if bg_task:
                    execute_task(bg_db, bg_task)
                bg_db.commit()
            except Exception:
                bg_db.rollback()
                logging.getLogger(__name__).exception("Background task %s failed", task_id)
            finally:
                bg_db.close()

        threading.Thread(target=_run, daemon=True).start()
    else:
        # Production mode: release any stale worker claim; worker will pick it up
        db.execute(
            select(PublishTask).where(PublishTask.id == task_id)  # re-lock for update
        )
        db.execute(
            _upd(PublishTask)
            .where(
                PublishTask.id == task_id,
                (PublishTask.worker_lease_until < _utcnow()) | PublishTask.worker_id.is_(None),
            )
            .values(worker_id=None, worker_lease_until=None, worker_heartbeat_at=None)
        )
        db.commit()

    add_audit_entry(
        db,
        user=current_user,
        action="task.execute.start",
        target_type="task",
        target_id=task_id,
        payload={"stop_before_publish": task.stop_before_publish},
        request=request,
    )
    return _ExecuteResponse(queued=True)


@tasks_router.post("/{task_id}/cancel", response_model=TaskRead)
def cancel_existing_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskRead:
    task = _verify_task_ownership(get_task(db, task_id), current_user)
    cancelled = cancel_task(db, task)
    add_audit_entry(
        db,
        user=current_user,
        action="task.cancel",
        target_type="task",
        target_id=task_id,
        payload=None,
        request=request,
    )
    return to_task_read(cancelled)


@tasks_router.get("/{task_id}/logs", response_model=list[TaskLogRead])
def read_task_logs(
    task_id: int,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TaskLogRead]:
    _verify_task_ownership(get_task(db, task_id), current_user)
    return [to_log_read(log) for log in list_task_logs(db, task_id, after_id=after_id, limit=limit)]


@tasks_router.get("/{task_id}", response_model=TaskRead)
def read_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskRead:
    task = _verify_task_ownership(get_task(db, task_id), current_user)
    return to_task_read(task)


@tasks_router.get("/{task_id}/records", response_model=list[PublishRecordRead])
def read_task_records(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[PublishRecordRead]:
    _verify_task_ownership(get_task(db, task_id), current_user)
    return [to_record_read(record) for record in list_task_records(db, task_id)]


@tasks_router.get("/{task_id}/stream")
def stream_task_events(
    task_id: int,
    after_log_id: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    from server.app.db.session import SessionLocal as _SL

    check_db = _SL()
    try:
        _verify_task_ownership(get_task(check_db, task_id), current_user)
    finally:
        check_db.close()

    def _generate():
        from server.app.db.session import SessionLocal as _SL2

        last_id = after_log_id
        prev_records = ""
        prev_task = ""

        sess = _SL2()
        try:
            while True:
                sess.expire_all()
                try:
                    task = get_task(sess, task_id)
                    if task is None:
                        yield "event: done\ndata: {}\n\n"
                        break

                    task_json = to_task_read(task).model_dump_json()
                    if task_json != prev_task:
                        yield f"event: task\ndata: {task_json}\n\n"
                        prev_task = task_json

                    new_logs = list_task_logs(sess, task_id, after_id=last_id, limit=100)
                    for log in new_logs:
                        yield f"event: log\ndata: {to_log_read(log).model_dump_json()}\n\n"
                    if new_logs:
                        last_id = max(log.id for log in new_logs)

                    records = list_task_records(sess, task_id)
                    records_json = (
                        "[" + ",".join(to_record_read(r).model_dump_json() for r in records) + "]"
                    )
                    if records_json != prev_records:
                        yield f"event: records\ndata: {records_json}\n\n"
                        prev_records = records_json

                    if task.status in TERMINAL_TASK_STATUSES:
                        yield "event: done\ndata: {}\n\n"
                        break

                except GeneratorExit:
                    break
                except Exception:
                    logging.getLogger(__name__).exception("SSE error for task %s", task_id)
                    try:
                        yield "retry: 15000\nevent: error\ndata: {}\n\n"
                    except Exception:
                        pass
                    break

                time.sleep(1)
        finally:
            sess.close()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Publish record helpers ────────────────────────────────────────────────────


def _verify_record_ownership(
    record: PublishRecord | None, current_user: User, db: Session
) -> PublishRecord:
    if record is None:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    task = get_task(db, record.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    if current_user.role != "admin" and task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    return record


def _start_background_execute(task_id: int) -> None:
    if bg_session_factory is None:
        # Production mode: worker picks up the task when it finds pending records.
        return

    def _run() -> None:
        bg_db = bg_session_factory()
        try:
            bg_task = get_task(bg_db, task_id)
            if bg_task:
                execute_task(bg_db, bg_task)
            bg_db.commit()
        except Exception:
            bg_db.rollback()
            logging.getLogger(__name__).exception(
                "Background execute after user action failed for task %s", task_id
            )
        finally:
            bg_db.close()

    threading.Thread(target=_run, daemon=True).start()


# ── Publish record routes ─────────────────────────────────────────────────────


@publish_records_router.post("/{record_id}/manual-confirm", response_model=PublishRecordRead)
def manual_confirm_record_endpoint(
    record_id: int,
    payload: ManualConfirmInput,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublishRecordRead:
    record = _verify_record_ownership(get_record(db, record_id), current_user, db)
    publish_url = str(payload.publish_url) if payload.publish_url else None
    result = manual_confirm_record(db, record, payload.outcome, publish_url, payload.error_message)
    db.commit()

    add_audit_entry(
        db,
        user=current_user,
        action="publish_record.manual_confirm",
        target_type="publish_record",
        target_id=record_id,
        payload=None,
        request=request,
    )

    task = get_task(db, record.task_id)
    if task is not None and task.status not in TERMINAL_TASK_STATUSES:
        _start_background_execute(record.task_id)

    return to_record_read(result)


@publish_records_router.post("/{record_id}/resolve-user-input", response_model=PublishRecordRead)
def resolve_user_input_record_endpoint(
    record_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublishRecordRead:
    record = _verify_record_ownership(get_record(db, record_id), current_user, db)
    result = resolve_user_input_record(db, record)
    db.commit()

    add_audit_entry(
        db,
        user=current_user,
        action="publish_record.resolve_user_input",
        target_type="publish_record",
        target_id=record_id,
        payload=None,
        request=request,
    )

    task = get_task(db, record.task_id)
    if task is not None and task.status not in TERMINAL_TASK_STATUSES:
        _start_background_execute(record.task_id)

    return to_record_read(result)


@publish_records_router.post("/{record_id}/retry", response_model=PublishRecordRead)
def retry_record_endpoint(
    record_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublishRecordRead:
    record = _verify_record_ownership(get_record(db, record_id), current_user, db)
    result = retry_record(db, record)
    db.commit()
    add_audit_entry(
        db,
        user=current_user,
        action="publish_record.retry",
        target_type="publish_record",
        target_id=record_id,
        payload={"new_record_id": result.id},
        request=request,
    )
    _start_background_execute(record.task_id)
    return to_record_read(result)
