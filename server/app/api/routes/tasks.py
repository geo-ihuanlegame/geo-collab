"""
任务相关 API 路由。

核心流程：
  1. POST /api/tasks — 创建任务，返回 TaskRead（含 records 数量）
  2. POST /api/tasks/{id}/execute — 启动后台线程立即执行（非队列模式），返回 {"queued": true} + 202
  3. GET  /api/tasks/{id}/records — 获取发布记录列表（含 novnc_url）
  4. GET  /api/tasks/{id}/logs — 增量拉取日志

后台执行：
  - 使用独立 DB Session（bg_session_factory）避免与请求 Session 冲突
  - 测试时 bg_session_factory 被 monkeypatch 为 TestingSessionLocal
"""
import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.models import PublishTask, User
from server.app.schemas.task import (
    PublishRecordRead,
    TaskAssignmentPreviewRead,
    TaskCreate,
    TaskLogRead,
    TaskRead,
)
from server.app.api.serializers import to_log_read, to_record_read, to_task_read
from server.app.modules.tasks import (
    TERMINAL_TASK_STATUSES,
    cancel_task,
    create_task,
    execute_task,
    get_task,
    list_task_logs,
    list_task_records,
    list_tasks,
    preview_task_assignment,
)

router = APIRouter()

# 后台任务使用的 Session 工厂（测试时可替换为内存数据库的 factory）
bg_session_factory: Any = None


def _verify_task_ownership(task: PublishTask | None, current_user: User) -> PublishTask:
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if current_user.role != "admin" and task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# 获取所有任务列表
@router.get("", response_model=list[TaskRead])
def read_tasks(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TaskRead]:
    user_id_filter = None if current_user.role == "admin" else current_user.id
    tasks = list_tasks(db, skip=skip, limit=limit, user_id=user_id_filter)
    return [to_task_read(task) for task in tasks]


# 创建新任务
@router.post("", response_model=TaskRead)
def create_task_endpoint(
    payload: TaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskRead:
    try:
        return to_task_read(create_task(db, current_user.id, payload, role=current_user.role))
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
                refreshed = get_task(db, existing.id)
                return to_task_read(refreshed or existing)
            raise HTTPException(status_code=409, detail="请求冲突：client_request_id 已存在或数据异常")


# 预览任务分配（分组轮询时的文章-账号映射）
@router.post("/preview", response_model=TaskAssignmentPreviewRead)
def preview_task_assignment_endpoint(
    payload: TaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskAssignmentPreviewRead:
    return preview_task_assignment(db, payload, user_id=current_user.id, role=current_user.role)


# 执行任务。
# 生产环境：worker 容器轮询 DB 认领并执行，API 只清理过期 worker 认领并返回 202。
# 测试环境：bg_session_factory 被 monkeypatch 为 TestingSessionLocal 时，在后台线程本地执行。
@router.post("/{task_id}/execute", status_code=202)
def start_task_execution(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
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
        from sqlalchemy import update as _upd
        db.execute(
            _upd(PublishTask)
            .where(
                PublishTask.id == task_id,
                (PublishTask.worker_lease_until < _utcnow()) | PublishTask.worker_id.is_(None),
            )
            .values(worker_id=None, worker_lease_until=None, worker_heartbeat_at=None)
        )
        db.commit()

    return {"queued": True}


# 取消任务
@router.post("/{task_id}/cancel", response_model=TaskRead)
def cancel_existing_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskRead:
    task = _verify_task_ownership(get_task(db, task_id), current_user)
    return to_task_read(cancel_task(db, task))


# 获取任务日志（支持增量拉取）
@router.get("/{task_id}/logs", response_model=list[TaskLogRead])
def read_task_logs(
    task_id: int,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TaskLogRead]:
    _verify_task_ownership(get_task(db, task_id), current_user)
    return [to_log_read(log) for log in list_task_logs(db, task_id, after_id=after_id, limit=limit)]


# 获取任务详情
@router.get("/{task_id}", response_model=TaskRead)
def read_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskRead:
    task = _verify_task_ownership(get_task(db, task_id), current_user)
    return to_task_read(task)


# 获取任务的发布记录列表
@router.get("/{task_id}/records", response_model=list[PublishRecordRead])
def read_task_records(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[PublishRecordRead]:
    _verify_task_ownership(get_task(db, task_id), current_user)
    return [to_record_read(record) for record in list_task_records(db, task_id)]


# 任务事件流（SSE）：替代前端轮询，推送日志/记录/任务状态变更
# 每 1 秒检查一次 DB，有变化时发送对应事件类型；任务进入终态后发 done 并关闭流
@router.get("/{task_id}/stream")
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
                    records_json = "[" + ",".join(to_record_read(r).model_dump_json() for r in records) + "]"
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
