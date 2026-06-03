"""AI 生文模块路由（问题池 CRUD/同步 + 问题类型聚合）。

注：旧 `POST /sessions` 问题池直连生成已硬切下线，改走方案流（scheme_router）。
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.ai_generation import question_bank as qb
from server.app.modules.ai_generation.schemas import (
    GenerationSessionRead,
    QuestionBrief,
    QuestionItemRead,
    QuestionPoolCreate,
    QuestionPoolRead,
    QuestionTypeRead,
    SyncResult,
)
from server.app.modules.ai_generation.service import get_session
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.system.models import User

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sessions")
def start_generation() -> None:
    """旧问题池直连生成已硬切下线。改用方案流（scheme run）。"""
    raise HTTPException(
        status_code=410,
        detail=(
            "问题池直连生成已下线，请改用方案流：先 POST /api/generation/schemes 建方案，"
            "再 POST /api/generation/schemes/{scheme_id}/runs 执行。"
        ),
    )


@router.get("/sessions/{session_id}", response_model=GenerationSessionRead)
def get_generation_status(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    session = get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


# ── 问题库 ───────────────────────────────────────────────────────────────────


def _pool_to_read(pool: Any, pending_count: int) -> QuestionPoolRead:
    return QuestionPoolRead(
        id=pool.id,
        name=pool.name,
        feishu_app_token=pool.feishu_app_token,
        feishu_table_id=pool.feishu_table_id,
        last_synced_at=pool.last_synced_at,
        created_at=pool.created_at,
        pending_count=pending_count,
    )


def _get_owned_pool(db: Session, pool_id: int, current_user: User) -> Any:
    pool = qb.get_pool(db, pool_id)
    if pool is None or (current_user.role != "admin" and pool.user_id != current_user.id):
        raise HTTPException(status_code=404, detail="问题池不存在")
    return pool


@router.get("/question-pools", response_model=list[QuestionPoolRead])
def list_question_pools(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    pools = qb.list_pools(db, user_id=current_user.id, is_admin=current_user.role == "admin")
    return [_pool_to_read(p, len(qb.list_items(db, p.id, status="pending"))) for p in pools]


@router.post("/question-pools", response_model=QuestionPoolRead, status_code=201)
def create_question_pool(
    payload: QuestionPoolCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    pool = qb.create_pool(
        db,
        user_id=current_user.id,
        name=payload.name,
        feishu_app_token=payload.feishu_app_token,
        feishu_table_id=payload.feishu_table_id,
    )
    db.commit()
    add_audit_entry(
        db,
        user=current_user,
        action="question_pool.create",
        target_type="question_pool",
        target_id=pool.id,
        payload={"name": pool.name},
        request=request,
    )
    return _pool_to_read(pool, 0)


@router.post("/question-pools/{pool_id}/sync", response_model=SyncResult)
def sync_question_pool(
    pool_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    pool = _get_owned_pool(db, pool_id, current_user)
    result = qb.sync_pool(db, pool)
    db.commit()
    add_audit_entry(
        db,
        user=current_user,
        action="question_pool.sync",
        target_type="question_pool",
        target_id=pool_id,
        payload={
            "total": result.get("total"),
            "added": result.get("added"),
            "updated": result.get("updated"),
            "reactivated": result.get("reactivated"),
            "deactivated": result.get("deactivated"),
        },
        request=request,
    )
    return SyncResult(**result)


@router.get("/question-pools/{pool_id}/items", response_model=list[QuestionItemRead])
def list_question_items(
    pool_id: int,
    status: str = "pending",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    pool = _get_owned_pool(db, pool_id, current_user)
    return qb.list_items(db, pool.id, status=(None if status == "all" else status))


@router.get(
    "/question-pools/{pool_id}/question-types",
    response_model=list[QuestionTypeRead],
)
def list_question_types(
    pool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """按问题类型（category）聚合该池所有 source_active 问题，供方案录入页使用。"""
    from server.app.modules.ai_generation import scheme_service as svc

    pool = _get_owned_pool(db, pool_id, current_user)
    return [
        QuestionTypeRead(
            question_type=qtype,
            count=len(items),
            questions=[QuestionBrief.model_validate(it) for it in items],
        )
        for qtype, items in svc.question_types(db, pool.id)
    ]
