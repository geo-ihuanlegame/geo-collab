"""AI 生文模块路由。"""

import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.ai_generation import question_bank as qb
from server.app.modules.ai_generation.schemas import (
    GenerationSessionCreate,
    GenerationSessionRead,
    QuestionItemRead,
    QuestionPoolCreate,
    QuestionPoolRead,
    SyncResult,
)
from server.app.modules.ai_generation.service import create_session, get_session
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.prompt_templates.service import get_visible_prompt_template
from server.app.modules.skills.service import get_skill
from server.app.modules.system.models import User

logger = logging.getLogger(__name__)
router = APIRouter()

# 后台任务使用的 Session 工厂（测试时可替换为 TestingSessionLocal）
bg_session_factory: Any = None


@router.post("/sessions", status_code=202)
def start_generation(
    payload: GenerationSessionCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    skill = get_skill(db, payload.skill_id)
    if skill is None or not skill.is_enabled:
        raise HTTPException(status_code=404, detail="Skill 不存在或已停用")

    prompt = get_visible_prompt_template(
        db,
        payload.prompt_template_id,
        user_id=current_user.id,
        scope="generation",
    )
    if prompt is None or not prompt.is_enabled:
        raise HTTPException(status_code=404, detail="提示词模板不存在或已停用")

    # 单批上限：避免限流/成本失控（手动勾选 + 自动 N 都受限）
    BATCH_MAX = 20

    pool_id: int | None = payload.pool_id
    item_ids = list(dict.fromkeys(payload.question_item_ids))  # 去重保序

    if item_ids:
        # 手动模式：校验选中的问题单元存在 + 归属（admin 跳过）+ 同池一致 + status=pending
        if len(item_ids) > BATCH_MAX:
            raise HTTPException(status_code=400, detail=f"单批最多 {BATCH_MAX} 条问题")
        items = qb.get_items(db, item_ids)
        if len(items) != len(set(item_ids)):
            raise HTTPException(status_code=400, detail="部分问题单元不存在")
        consumed = [it.id for it in items if it.status != "pending"]
        if consumed:
            raise HTTPException(
                status_code=400,
                detail=f"选中的问题已生成过，请刷新列表后重新勾选（共 {len(consumed)} 条）",
            )
        item_pool_ids = {it.pool_id for it in items}
        if len(item_pool_ids) > 1:
            raise HTTPException(status_code=400, detail="一次只能选同一个问题池的单元")
        derived_pool_id = next(iter(item_pool_ids))
        if pool_id is None:
            pool_id = derived_pool_id
        elif pool_id != derived_pool_id:
            raise HTTPException(status_code=400, detail="选中的单元不属于指定的问题池")
        if current_user.role != "admin":
            pool = qb.get_pool(db, pool_id)
            if pool is None or pool.user_id != current_user.id:
                raise HTTPException(status_code=403, detail="无权使用该问题池")
        auto_count_to_use: int | None = None
    else:
        # 自动模式：要求 pool_id + auto_count（>0、不超过 BATCH_MAX）
        if not payload.auto_count or payload.auto_count <= 0:
            raise HTTPException(status_code=400, detail="请勾选问题，或填写自动生成数量")
        if payload.auto_count > BATCH_MAX:
            raise HTTPException(status_code=400, detail=f"单批最多 {BATCH_MAX} 篇")
        if pool_id is None:
            raise HTTPException(status_code=400, detail="自动模式必须指定问题池")
        pool = qb.get_pool(db, pool_id)
        if pool is None or (current_user.role != "admin" and pool.user_id != current_user.id):
            raise HTTPException(status_code=404, detail="问题池不存在")
        auto_count_to_use = payload.auto_count

    session = create_session(
        db,
        user_id=current_user.id,
        skill_id=payload.skill_id,
        prompt_template_id=payload.prompt_template_id,
        extra_instruction=payload.extra_instruction,
        pool_id=pool_id,
        question_item_ids=item_ids,
        auto_count=auto_count_to_use,
    )
    db.flush()
    db.commit()

    session_id = session.id

    topic_count = len(item_ids) if item_ids else (auto_count_to_use or 0)
    add_audit_entry(
        db,
        user=current_user,
        action="generation_session.create",
        target_type="generation_session",
        target_id=session_id,
        payload={"topic_count": topic_count, "skill_id": payload.skill_id},
        request=request,
    )

    if bg_session_factory is None:
        logger.error(
            "bg_session_factory 未初始化，AI 生文后台线程将不会运行（session_id=%d）",
            session.id,
        )
    else:
        factory = bg_session_factory

        def _run() -> None:
            from server.app.modules.ai_generation.pipeline import run_pipeline

            bg_db = factory()
            try:
                run_pipeline(bg_db, session_id, session_factory=factory)
            except Exception:
                logger.exception("generation background thread failed for session %d", session_id)
            finally:
                bg_db.close()

        threading.Thread(target=_run, daemon=True).start()

    return JSONResponse(
        content={"session_id": session_id, "status": "pending"},
        status_code=202,
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
