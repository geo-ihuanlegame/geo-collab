"""审计日志写入与查询服务。

设计原则：
  - 审计失败不影响主流程：任何异常被 try/except 吞下，仅记 logger.warning。
  - payload 敏感字段自动脱敏（密码、token、secret、api_key 等）。
  - target_id 统一转 str，兼容 int / UUID 主键。
  - helper 内 db.add + db.commit，调用者无需额外 commit；如果 commit 失败，rollback 并继续。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from server.app.modules.audit.models import AuditLog
from server.app.modules.system.models import User

_logger = logging.getLogger(__name__)

# 大小写不敏感匹配。key 命中即把 value 替换为 "***"。
_REDACT_KEYS = {
    "password",
    "old_password",
    "new_password",
    "password_hash",
    "access_token",
    "refresh_token",
    "api_key",
    "secret",
    "feishu_app_token",
    "feishu_app_secret",
    "token",
}


def _redact(payload: Any) -> Any:
    """递归脱敏：对 dict/list 深度遍历，命中 _REDACT_KEYS 的 value 整体替换为 "***"。其余值原样返回。"""
    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for k, v in payload.items():
            if isinstance(k, str) and k.lower() in _REDACT_KEYS:
                result[k] = "***"
            else:
                result[k] = _redact(v)
        return result
    if isinstance(payload, list):
        return [_redact(item) for item in payload]
    return payload


def add_audit_entry(
    db: Session,
    *,
    user: User | None,
    action: str,
    target_type: str,
    target_id: str | int | None = None,
    payload: dict | None = None,
    request: Request | None = None,
) -> None:
    """写入一条审计记录。任何异常都被吞下，不影响调用方主流程。"""
    try:
        ip_address: str | None = None
        user_agent: str | None = None
        if request is not None:
            if request.client is not None:
                ip_address = request.client.host
            ua = request.headers.get("user-agent") or ""
            user_agent = ua[:255] or None

        entry = AuditLog(
            user_id=user.id if user is not None else None,
            username=(user.username if user is not None else None),
            action=action,
            target_type=target_type,
            target_id=(str(target_id) if target_id is not None else None),
            payload_json=(_redact(payload) if payload is not None else None),
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.add(entry)
        db.commit()
    except Exception:
        _logger.warning(
            "audit log write failed: action=%s target=%s", action, target_type, exc_info=True
        )
        try:
            db.rollback()
        except Exception:
            pass


def list_audit_logs(
    db: Session,
    *,
    user_id: int | None = None,
    action_prefix: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    cursor: int | None = None,
    limit: int = 100,
) -> tuple[list[AuditLog], int | None]:
    """按 id 倒序游标分页查询审计日志，返回 (本页记录, next_cursor)。

    cursor 是上一页最后一条的 id，传入后只取 id 更小（更旧）的记录。
    next_cursor 为 None 表示没有下一页。
    """
    q = db.query(AuditLog)
    if user_id is not None:
        q = q.filter(AuditLog.user_id == user_id)
    if action_prefix:
        q = q.filter(AuditLog.action.like(f"{action_prefix}%"))
    if target_type:
        q = q.filter(AuditLog.target_type == target_type)
    if target_id:
        q = q.filter(AuditLog.target_id == target_id)
    if start_at is not None:
        q = q.filter(AuditLog.created_at >= start_at)
    if end_at is not None:
        q = q.filter(AuditLog.created_at <= end_at)
    if cursor is not None:
        q = q.filter(AuditLog.id < cursor)

    # 多取一条（limit+1）探测是否还有下一页，再裁回 limit，避免额外 count 查询。
    rows = q.order_by(AuditLog.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = items[-1].id if (has_more and items) else None
    return items, next_cursor
