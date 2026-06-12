"""审计日志查询路由（仅管理员）。

GET /api/audit-logs
  - user_id, action_prefix, target_type, target_id, start_at, end_at
  - cursor（id 游标，倒序）
  - limit（默认 100，上限 500）

按 id 倒序分页：response.next_cursor 是下一页应传入的 cursor 值。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from server.app.core.security import require_admin
from server.app.db.session import get_db
from server.app.modules.audit.schemas import AuditLogList, AuditLogRead
from server.app.modules.audit.service import list_audit_logs
from server.app.modules.system.models import User

router = APIRouter()


@router.get("", response_model=AuditLogList)
def read_audit_logs(
    user_id: int | None = Query(None),
    action_prefix: str | None = Query(None),
    target_type: str | None = Query(None),
    target_id: str | None = Query(None),
    start_at: datetime | None = Query(None),
    end_at: datetime | None = Query(None),
    cursor: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> AuditLogList:
    """查询审计日志（仅管理员）。各过滤参数可叠加，按 id 倒序游标分页。"""
    items, next_cursor = list_audit_logs(
        db,
        user_id=user_id,
        action_prefix=action_prefix,
        target_type=target_type,
        target_id=target_id,
        start_at=start_at,
        end_at=end_at,
        cursor=cursor,
        limit=limit,
    )
    return AuditLogList(
        items=[AuditLogRead.model_validate(r) for r in items],
        next_cursor=next_cursor,
    )
