from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.app.core.mcp_auth import require_mcp_token
from server.app.db.session import get_db
from server.app.modules.performance.service import (
    get_account_performance,
    get_template_performance,
    record_publish_metrics,
)

router = APIRouter()


@router.get(
    "/prompt-templates/{template_id}/performance",
    dependencies=[Depends(require_mcp_token)],
)
def get_template_performance_endpoint(
    template_id: int,
    window_days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return get_template_performance(db, template_id, window_days)


@router.get(
    "/accounts/{account_id}/performance",
    dependencies=[Depends(require_mcp_token)],
)
def get_account_performance_endpoint(
    account_id: int,
    window_days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return get_account_performance(db, account_id, window_days)


class PublishMetricsPayload(BaseModel):
    metrics: dict[str, Any]


@router.post(
    "/publish-records/{record_id}/metrics",
    dependencies=[Depends(require_mcp_token)],
)
def post_publish_metrics(
    record_id: int,
    payload: PublishMetricsPayload,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        record_publish_metrics(db, record_id, payload.metrics)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return {"ok": True}
