"""审计日志 Pydantic 模型。"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None = None
    username: str | None = None
    action: str
    target_type: str
    target_id: str | None = None
    payload_json: Any | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime


class AuditLogList(BaseModel):
    items: list[AuditLogRead]
    next_cursor: int | None = None
