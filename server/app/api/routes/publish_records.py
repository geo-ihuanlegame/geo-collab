import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.models import PublishRecord, PublishTask, User
from server.app.schemas.task import ManualConfirmInput, PublishRecordRead
from server.app.api.serializers import to_record_read
from server.app.modules.tasks import (
    TERMINAL_TASK_STATUSES,
    execute_task,
    get_record,
    get_task,
    manual_confirm_record,
    resolve_user_input_record,
    retry_record,
)

router = APIRouter()


def _verify_record_ownership(record: PublishRecord | None, current_user: User, db: Session) -> PublishRecord:
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    task = db.get(PublishTask, record.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Record not found")
    if current_user.role != "admin" and task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


def _start_background_execute(task_id: int) -> None:
    from server.app.api.routes.tasks import bg_session_factory as _bf
    if _bf is None:
        # Production mode: worker picks up the task when it finds pending records.
        return

    def _run() -> None:
        bg_db = _bf()
        try:
            bg_task = get_task(bg_db, task_id)
            if bg_task:
                execute_task(bg_db, bg_task)
            bg_db.commit()
        except Exception:
            bg_db.rollback()
            logging.getLogger(__name__).exception("Background execute after user action failed for task %s", task_id)
        finally:
            bg_db.close()

    threading.Thread(target=_run, daemon=True).start()


@router.post("/{record_id}/manual-confirm", response_model=PublishRecordRead)
def manual_confirm_record_endpoint(
    record_id: int,
    payload: ManualConfirmInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublishRecordRead:
    record = _verify_record_ownership(get_record(db, record_id), current_user, db)
    result = manual_confirm_record(db, record, payload.outcome, payload.publish_url, payload.error_message)
    db.commit()

    task = get_task(db, record.task_id)
    if task is not None and task.status not in TERMINAL_TASK_STATUSES:
        _start_background_execute(record.task_id)

    return to_record_read(result)


@router.post("/{record_id}/resolve-user-input", response_model=PublishRecordRead)
def resolve_user_input_record_endpoint(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublishRecordRead:
    record = _verify_record_ownership(get_record(db, record_id), current_user, db)
    result = resolve_user_input_record(db, record)
    db.commit()

    task = get_task(db, record.task_id)
    if task is not None and task.status not in TERMINAL_TASK_STATUSES:
        _start_background_execute(record.task_id)

    return to_record_read(result)


@router.post("/{record_id}/retry", response_model=PublishRecordRead)
def retry_record_endpoint(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublishRecordRead:
    record = _verify_record_ownership(get_record(db, record_id), current_user, db)
    result = retry_record(db, record)
    db.commit()
    _start_background_execute(record.task_id)
    return to_record_read(result)
