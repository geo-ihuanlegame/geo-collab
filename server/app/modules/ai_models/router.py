"""AI 模型注册表路由（仅管理员）。

GET/POST/PATCH/DELETE /api/ai-models —— 全部 require_admin。写操作记审计。
读下拉（方案/Pipeline 用）走 ai_generation.scheme_router 的 /ai-engines /format-engines，
那两个只需 get_current_user，不在此处。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from server.app.core.security import require_admin
from server.app.db.session import get_db
from server.app.modules.ai_models import service
from server.app.modules.ai_models.schemas import AiModelCreate, AiModelRead, AiModelUpdate
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.system.models import User

router = APIRouter()


def _audit_payload(row: object) -> dict:
    # 只记非敏感元数据；api_key_env 是变量名非密钥
    return {
        "label": getattr(row, "label", None),
        "model": getattr(row, "model", None),
        "scope": getattr(row, "scope", None),
        "base_url": getattr(row, "base_url", None),
        "api_key_env": getattr(row, "api_key_env", None),
        "is_enabled": getattr(row, "is_enabled", None),
        "is_default": getattr(row, "is_default", None),
    }


@router.get("", response_model=list[AiModelRead])
def list_ai_models(
    scope: str | None = Query(None),
    enabled_only: bool = Query(False),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[AiModelRead]:
    rows = service.list_models(db, scope=scope, enabled_only=enabled_only)
    return [AiModelRead.model_validate(r) for r in rows]


@router.post("", response_model=AiModelRead, status_code=201)
def create_ai_model(
    payload: AiModelCreate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
) -> AiModelRead:
    row = service.create_model(db, payload)
    add_audit_entry(
        db,
        user=current,
        action="ai_model.create",
        target_type="ai_model",
        target_id=row.id,
        payload=_audit_payload(row),
        request=request,
    )
    return AiModelRead.model_validate(row)


@router.get("/{model_id}", response_model=AiModelRead)
def get_ai_model(
    model_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> AiModelRead:
    row = service.get_model(db, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="AI 模型不存在")
    return AiModelRead.model_validate(row)


@router.patch("/{model_id}", response_model=AiModelRead)
def update_ai_model(
    model_id: int,
    payload: AiModelUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
) -> AiModelRead:
    row = service.update_model(db, model_id, payload)
    if row is None:
        raise HTTPException(status_code=404, detail="AI 模型不存在")
    add_audit_entry(
        db,
        user=current,
        action="ai_model.update",
        target_type="ai_model",
        target_id=row.id,
        payload=_audit_payload(row),
        request=request,
    )
    return AiModelRead.model_validate(row)


@router.delete("/{model_id}", status_code=204)
def delete_ai_model(
    model_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
) -> None:
    ok = service.delete_model(db, model_id)
    if not ok:
        raise HTTPException(status_code=404, detail="AI 模型不存在")
    add_audit_entry(
        db,
        user=current,
        action="ai_model.delete",
        target_type="ai_model",
        target_id=model_id,
        request=request,
    )
