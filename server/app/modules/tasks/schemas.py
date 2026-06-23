"""
任务模块 Pydantic 请求 / 响应模型 + 序列化函数。

合并自：
  - schemas/task.py
  - api/serializers.py（to_task_read、to_record_read、to_log_read）
"""

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, HttpUrl, model_validator

if TYPE_CHECKING:
    from server.app.modules.tasks.models import PublishRecord, PublishTask, TaskLog


# ── 请求体 ──────────────────────────────────────────────────────────────────


class TaskAccountInput(BaseModel):
    account_id: int
    sort_order: int | None = None


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    client_request_id: str | None = Field(default=None, max_length=80)
    task_type: str  # 任务类型：single / group_round_robin / article_round_robin
    article_id: int | None = None
    group_id: int | None = None
    article_ids: list[int] | None = None  # 仅 article_round_robin 用
    platform_code: str = "toutiao"
    accounts: list[TaskAccountInput]
    stop_before_publish: bool = False


class AutoDistributeRequest(BaseModel):
    article_id: int | None = None
    group_id: int | None = None
    account_ids: list[int]
    name: str | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "AutoDistributeRequest":
        if (self.article_id is None) == (self.group_id is None):
            raise ValueError("必须且只能提供 article_id 或 group_id 其中之一")
        return self


class ManualConfirmInput(BaseModel):
    outcome: str
    publish_url: HttpUrl | None = None
    error_message: str | None = None


# ── 响应体 ──────────────────────────────────────────────────────────────────


class TaskAccountRead(BaseModel):
    account_id: int
    sort_order: int
    display_name: str
    status: str


class PublishRecordRead(BaseModel):
    id: int
    task_id: int
    article_id: int
    platform_id: int
    account_id: int
    status: str
    queue_reason: str | None = None
    publish_url: str | None
    error_message: str | None
    retry_of_record_id: int | None
    started_at: datetime | None
    finished_at: datetime | None
    lease_until: datetime | None = None
    remote_browser_session_id: str | None = None
    novnc_url: str | None = None
    failure_kind: str | None = None


class TaskStatusRead(BaseModel):
    id: int
    status: str
    lease_until: datetime | None = None


class TaskLogRead(BaseModel):
    id: int
    task_id: int
    record_id: int | None
    level: str  # 日志级别：info / warn / error
    message: str
    screenshot_asset_id: str | None
    created_at: datetime


class TaskAssignmentPreviewItemRead(BaseModel):
    position: int
    article_id: int
    account_id: int
    account_sort_order: int


class TaskAssignmentPreviewRead(BaseModel):
    task_type: str
    platform_code: str
    article_count: int
    account_count: int
    items: list[TaskAssignmentPreviewItemRead]


class TaskRead(BaseModel):
    id: int
    name: str
    task_type: str
    status: str
    platform_id: int
    platform_code: str
    article_id: int | None
    group_id: int | None
    stop_before_publish: bool
    cancel_requested: bool = False
    accounts: list[TaskAccountRead]
    record_count: int
    worker_id: str | None = None
    worker_heartbeat_at: datetime | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AutoDistributeResponse(BaseModel):
    """自动分发响应：按平台拆分后可能建多个任务（一个任务=单平台），全部返回。"""

    tasks: list[TaskRead]


# ── 序列化函数（原 api/serializers.py）──────────────────────────────────────


def to_task_read(task: "PublishTask") -> TaskRead:
    accounts = sorted(task.accounts, key=lambda item: item.sort_order)
    return TaskRead(
        id=task.id,
        name=task.name,
        task_type=task.task_type,
        status=task.status,
        platform_id=task.platform.id,
        platform_code=task.platform.code,
        article_id=task.article_id,
        group_id=task.group_id,
        stop_before_publish=task.stop_before_publish,
        cancel_requested=bool(task.cancel_requested),
        accounts=[
            TaskAccountRead(
                account_id=item.account_id,
                sort_order=item.sort_order,
                display_name=item.account.display_name,
                status=item.account.status,
            )
            for item in accounts
        ],
        record_count=len(task.records),
        worker_id=task.worker_id,
        worker_heartbeat_at=task.worker_heartbeat_at,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def to_record_read(record: "PublishRecord") -> PublishRecordRead:
    """序列化发布记录，并附带当前关联的远程浏览器会话 id / noVNC 接管链接（无会话则为 None）。"""
    from server.app.modules.accounts import get_session_for_record  # 避免循环 import

    session = get_session_for_record(record.id)
    return PublishRecordRead(
        id=record.id,
        task_id=record.task_id,
        article_id=record.article_id,
        platform_id=record.platform_id,
        account_id=record.account_id,
        status=record.status,
        queue_reason=getattr(record, "queue_reason", None),
        publish_url=record.publish_url,
        error_message=record.error_message,
        retry_of_record_id=record.retry_of_record_id,
        started_at=record.started_at,
        finished_at=record.finished_at,
        lease_until=record.lease_until,
        remote_browser_session_id=session.id if session else None,
        novnc_url=session.novnc_url if session else None,
        failure_kind=getattr(record, "failure_kind", None),
    )


def to_log_read(log: "TaskLog") -> TaskLogRead:
    return TaskLogRead(
        id=log.id,
        task_id=log.task_id,
        record_id=log.record_id,
        level=log.level,
        message=log.message,
        screenshot_asset_id=log.screenshot_asset_id,
        created_at=log.created_at,
    )
