"""方案池 / 方案运行 路由（挂在 /api/generation 下）。

- 方案 CRUD：/schemes
- 方案运行：/schemes/{id}/runs（建 run + 异步执行）、/scheme-runs/{id}（查状态）
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.ai_generation import question_bank as qb
from server.app.modules.ai_generation import scheme_service as svc
from server.app.modules.ai_generation.models import (
    GenerationScheme,
    GenerationSchemeRun,
    GenerationSchemeRunTask,
)
from server.app.modules.ai_generation.schemas import (
    AiEngineRead,
    SchemeCreate,
    SchemeLineQuestionRead,
    SchemeLineRead,
    SchemePatch,
    SchemeRead,
    SchemeRunRead,
    SchemeRunSummary,
    SchemeRunTaskRead,
    SchemeUpdate,
)
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.system.models import User

logger = logging.getLogger(__name__)
scheme_router = APIRouter()

# 后台执行方案运行使用的会话工厂（create_app() 注入 SessionLocal；测试用 TestingSessionLocal）
bg_session_factory: Any = None


def _get_pool_or_404(db: Session, pool_id: int) -> Any:
    """问题池全员共享：任意登录用户都可在任一池上建方案，仅不存在 / 已删除时 404。
    （方案本身仍按用户私有，见 _get_owned_scheme。）"""
    pool = qb.get_pool(db, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="问题池不存在")
    return pool


def _get_owned_scheme(db: Session, scheme_id: int, current_user: User) -> GenerationScheme:
    scheme = svc.get_scheme(db, scheme_id)
    if scheme is None or (current_user.role != "admin" and scheme.user_id != current_user.id):
        raise HTTPException(status_code=404, detail="方案不存在")
    return scheme


def _scheme_to_read(db: Session, scheme: GenerationScheme) -> SchemeRead:
    line_reads: list[SchemeLineRead] = []
    for ln in svc.get_lines(db, scheme.id):
        questions = [
            SchemeLineQuestionRead.model_validate(q) for q in svc.get_line_questions(db, ln.id)
        ]
        line_reads.append(
            SchemeLineRead(
                id=ln.id,
                question_type=ln.question_type,
                article_count=ln.article_count,
                allowed_prompt_template_ids=ln.allowed_prompt_template_ids or [],
                questions=questions,
            )
        )
    return SchemeRead(
        id=scheme.id,
        name=scheme.name,
        pool_id=scheme.pool_id,
        is_enabled=scheme.is_enabled,
        ai_engine=scheme.ai_engine,
        created_at=scheme.created_at,
        updated_at=scheme.updated_at,
        lines=line_reads,
    )


@scheme_router.get("/ai-engines", response_model=list[AiEngineRead])
def list_ai_engines(
    current_user: User = Depends(get_current_user),
) -> Any:
    """方案可选的 AI 引擎列表（来自 settings.ai_engines，给方案编辑器下拉用）。"""
    from server.app.core.config import get_settings

    return [AiEngineRead(**e) for e in get_settings().ai_engines]


@scheme_router.get("/schemes", response_model=list[SchemeRead])
def list_schemes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    schemes = svc.list_schemes(db, user_id=current_user.id, is_admin=current_user.role == "admin")
    return [_scheme_to_read(db, s) for s in schemes]


@scheme_router.post("/schemes", response_model=SchemeRead, status_code=201)
def create_scheme(
    payload: SchemeCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    pool = _get_pool_or_404(db, payload.pool_id)
    scheme = svc.create_scheme(db, user_id=current_user.id, pool_id=pool.id, payload=payload)
    db.commit()
    db.refresh(scheme)
    add_audit_entry(
        db,
        user=current_user,
        action="generation_scheme.create",
        target_type="generation_scheme",
        target_id=scheme.id,
        payload={"name": scheme.name, "pool_id": pool.id, "lines": len(payload.lines)},
        request=request,
    )
    return _scheme_to_read(db, scheme)


@scheme_router.get("/schemes/{scheme_id}", response_model=SchemeRead)
def get_scheme(
    scheme_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    scheme = _get_owned_scheme(db, scheme_id, current_user)
    return _scheme_to_read(db, scheme)


@scheme_router.put("/schemes/{scheme_id}", response_model=SchemeRead)
def update_scheme(
    scheme_id: int,
    payload: SchemeUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    scheme = _get_owned_scheme(db, scheme_id, current_user)
    svc.update_scheme(db, scheme=scheme, user_id=current_user.id, payload=payload)
    db.commit()
    db.refresh(scheme)
    add_audit_entry(
        db,
        user=current_user,
        action="generation_scheme.update",
        target_type="generation_scheme",
        target_id=scheme.id,
        payload={"name": scheme.name, "lines": len(payload.lines)},
        request=request,
    )
    return _scheme_to_read(db, scheme)


@scheme_router.patch("/schemes/{scheme_id}", response_model=SchemeRead)
def patch_scheme(
    scheme_id: int,
    payload: SchemePatch,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """轻量更新（目前仅启用状态），不重建问题行。"""
    scheme = _get_owned_scheme(db, scheme_id, current_user)
    if payload.is_enabled is not None:
        scheme.is_enabled = payload.is_enabled
    db.commit()
    db.refresh(scheme)
    add_audit_entry(
        db,
        user=current_user,
        action="generation_scheme.patch",
        target_type="generation_scheme",
        target_id=scheme.id,
        payload={"is_enabled": scheme.is_enabled},
        request=request,
    )
    return _scheme_to_read(db, scheme)


@scheme_router.delete("/schemes/{scheme_id}", status_code=204)
def delete_scheme(
    scheme_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    scheme = _get_owned_scheme(db, scheme_id, current_user)
    svc.delete_scheme(db, scheme)
    db.commit()
    add_audit_entry(
        db,
        user=current_user,
        action="generation_scheme.delete",
        target_type="generation_scheme",
        target_id=scheme_id,
        payload={},
        request=request,
    )


# ── 方案运行 ───────────────────────────────────────────────────────────────────


def _run_to_read(db: Session, run: GenerationSchemeRun) -> SchemeRunRead:
    tasks = (
        db.query(GenerationSchemeRunTask)
        .filter(GenerationSchemeRunTask.run_id == run.id)
        .order_by(GenerationSchemeRunTask.id.asc())
        .all()
    )
    return SchemeRunRead(
        id=run.id,
        scheme_id=run.scheme_id,
        status=run.status,
        article_ids=run.article_ids or [],
        error_message=run.error_message,
        created_at=run.created_at,
        completed_at=run.completed_at,
        tasks=[SchemeRunTaskRead.model_validate(t) for t in tasks],
    )


@scheme_router.post("/schemes/{scheme_id}/runs", status_code=202)
def create_scheme_run(
    scheme_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    from server.app.modules.ai_generation.scheme_executor import create_run, run_scheme

    scheme = _get_owned_scheme(db, scheme_id, current_user)
    if not scheme.is_enabled:
        raise HTTPException(status_code=400, detail="方案已停用，无法运行")

    run = create_run(db, scheme=scheme, user_id=current_user.id)
    db.commit()
    run_id = run.id

    add_audit_entry(
        db,
        user=current_user,
        action="generation_scheme_run.create",
        target_type="generation_scheme_run",
        target_id=run_id,
        payload={"scheme_id": scheme_id},
        request=request,
    )

    if bg_session_factory is None:
        # 与 pipelines/router 对齐：后台执行器未就绪时标记运行失败 + 返回 503，
        # 不再返回虚假的 202（否则运行永远卡在 pending、调用方以为仍在执行）。
        logger.error("bg_session_factory 未注入，方案运行无法执行（run_id=%d）", run_id)
        from server.app.modules.ai_generation.models import GenerationSchemeRun

        run_obj = db.get(GenerationSchemeRun, run_id)
        if run_obj is not None:
            run_obj.status = "failed"
            run_obj.error_message = "后台执行器未就绪（bg_session_factory 未注入）"
            db.commit()
        return JSONResponse(status_code=503, content={"run_id": run_id, "status": "failed"})

    factory = bg_session_factory

    def _run() -> None:
        try:
            run_scheme(run_id, factory)
        except Exception:
            logger.exception("scheme run background thread failed for run %d", run_id)

    threading.Thread(target=_run, daemon=True).start()

    return JSONResponse(content={"run_id": run_id, "status": "pending"}, status_code=202)


@scheme_router.get("/schemes/{scheme_id}/runs", response_model=list[SchemeRunSummary])
def list_scheme_runs(
    scheme_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """某方案的历次运行（精简，倒序），供运行历史切换器使用。"""
    from sqlalchemy import func

    scheme = _get_owned_scheme(db, scheme_id, current_user)
    runs = (
        db.query(GenerationSchemeRun)
        .filter(GenerationSchemeRun.scheme_id == scheme.id)
        .order_by(GenerationSchemeRun.created_at.desc(), GenerationSchemeRun.id.desc())
        .limit(50)
        .all()
    )
    run_ids = [r.id for r in runs]
    task_counts: dict[int, int] = {}
    if run_ids:
        rows = (
            db.query(GenerationSchemeRunTask.run_id, func.count())
            .filter(GenerationSchemeRunTask.run_id.in_(run_ids))
            .group_by(GenerationSchemeRunTask.run_id)
            .all()
        )
        task_counts = {rid: cnt for rid, cnt in rows}
    return [
        SchemeRunSummary(
            id=r.id,
            status=r.status,
            article_count=len(r.article_ids or []),
            task_count=task_counts.get(r.id, 0),
            created_at=r.created_at,
            completed_at=r.completed_at,
        )
        for r in runs
    ]


@scheme_router.get("/scheme-runs/{run_id}", response_model=SchemeRunRead)
def get_scheme_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    run = db.get(GenerationSchemeRun, run_id)
    if run is None or (current_user.role != "admin" and run.user_id != current_user.id):
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return _run_to_read(db, run)
