"""AI 生文模块路由。"""
import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.system.models import User
from server.app.modules.ai_generation.service import create_session, get_session
from server.app.modules.prompt_templates.service import get_visible_prompt_template
from server.app.modules.skills.service import get_skill
from server.app.modules.ai_generation.schemas import GenerationSessionCreate, GenerationSessionRead

logger = logging.getLogger(__name__)
router = APIRouter()

# 后台任务使用的 Session 工厂（测试时可替换为 TestingSessionLocal）
bg_session_factory: Any = None


@router.post("/sessions", status_code=202)
def start_generation(
    payload: GenerationSessionCreate,
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

    session = create_session(
        db,
        user_id=current_user.id,
        skill_id=payload.skill_id,
        prompt_template_id=payload.prompt_template_id,
        extra_instruction=payload.extra_instruction,
    )
    db.flush()
    db.commit()

    session_id = session.id

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
