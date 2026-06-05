# server/app/modules/pipelines/router.py
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.pipelines import service as svc
from server.app.modules.pipelines.nodes.base import registered_types
from server.app.modules.pipelines.schemas import (
    DraftSave,
    PipelineCreate,
    PipelinePatch,
    PipelineRead,
    PublishRequest,
    RunRead,
    VersionRead,
)
from server.app.modules.system.models import User

router = APIRouter()

# 由 create_app() 注入（后台线程用）
bg_session_factory: Callable[[], Any] | None = None


def _owned(db: Session, pipeline_id: int, user: User):
    p = svc.get_pipeline(db, pipeline_id)
    if p is None or (user.role != "admin" and p.user_id != user.id):
        raise HTTPException(status_code=404, detail="工作流不存在")
    return p


def _to_read(db: Session, p) -> dict:
    if p.tags is None:
        p.tags = []
    nodes = svc.list_nodes(db, p.id)
    data = PipelineRead.model_validate(p).model_dump()
    data["nodes"] = [
        {
            "node_type": n.node_type,
            "name": n.name,
            "node_index": n.node_index,
            "config": n.config or {},
            "flow_meta": n.flow_meta,
        }
        for n in nodes
    ]
    return data


@router.get("/node-types")
def get_node_types() -> dict:
    # 节点 config 字段 schema，供前端属性面板渲染
    return {
        "node_types": [
            {
                "type": "input",
                "label": "输入源",
                "config_schema": [
                    {"key": "question_text", "type": "textarea", "label": "问题/主题"}
                ],
            },
            {
                "type": "ai_generate",
                "label": "AI 生文",
                "config_schema": [
                    {"key": "prompt_template_id", "type": "prompt_template", "label": "提示词模板"},
                    {"key": "count", "type": "number", "label": "生成数量"},
                    {"key": "model", "type": "text", "label": "模型(可空)"},
                ],
            },
            {
                "type": "article_group_source",
                "label": "已审核分组源",
                "config_schema": [
                    {"key": "group_id", "type": "article_group", "label": "内容分组"},
                ],
            },
            {
                "type": "distribute",
                "label": "内容分发",
                "config_schema": [
                    {"key": "account_ids", "type": "accounts", "label": "分发账号"},
                    {"key": "name", "type": "text", "label": "任务名(可空)"},
                ],
            },
            {
                "type": "question_source",
                "label": "问题源",
                "config_schema": [
                    {"key": "pool_id", "type": "question_pool", "label": "问题池"},
                    {"key": "question_type", "type": "question_type", "label": "问题类型"},
                ],
            },
            {
                "type": "ai_compose",
                "label": "AI创作",
                "config_schema": [
                    {"key": "ai_engine", "type": "ai_engine", "label": "AI 模型"},
                    {
                        "key": "prompt_template_ids",
                        "type": "prompt_templates",
                        "label": "提示词模板(可多选,运行时随机)",
                    },
                    {"key": "count", "type": "number", "label": "生成数量"},
                ],
            },
            {
                "type": "to_review",
                "label": "进入未审核库",
                "config_schema": [
                    {"key": "group_name", "type": "text", "label": "分组名(可空)"},
                ],
            },
        ],
        "registered": registered_types(),
    }


@router.get("")
def list_pipelines(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    items = svc.list_pipelines(db, user_id=user.id, is_admin=user.role == "admin")
    return [_to_read(db, p) for p in items]


@router.post("", status_code=201)
def create_pipeline(
    payload: PipelineCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    p = svc.create_pipeline(
        db,
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        type=payload.type,
        tags=payload.tags,
        ignore_exception=payload.ignore_exception,
        is_enabled=payload.is_enabled,
        schedule_kind=payload.schedule_kind,
        schedule_minute=payload.schedule_minute,
        schedule_hour=payload.schedule_hour,
        schedule_weekday=payload.schedule_weekday,
        window_start=payload.window_start,
        window_end=payload.window_end,
    )
    db.commit()
    return _to_read(db, p)


@router.get("/{pipeline_id}")
def get_pipeline(
    pipeline_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    p = _owned(db, pipeline_id, user)
    return _to_read(db, p)


@router.patch("/{pipeline_id}")
def patch_pipeline(
    pipeline_id: int,
    payload: PipelinePatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = _owned(db, pipeline_id, user)
    svc.patch_pipeline(db, p, fields=payload.model_dump(exclude_unset=True))
    db.commit()
    return _to_read(db, p)


@router.delete("/{pipeline_id}", status_code=204)
def delete_pipeline(
    pipeline_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    p = _owned(db, pipeline_id, user)
    svc.delete_pipeline(db, p)
    db.commit()


@router.post("/{pipeline_id}/draft")
def save_draft(
    pipeline_id: int,
    payload: DraftSave,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = _owned(db, pipeline_id, user)
    svc.save_draft(db, p, payload.snapshot)
    db.commit()
    return {"ok": True}


@router.post("/{pipeline_id}/publish")
def publish(
    pipeline_id: int,
    payload: PublishRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = _owned(db, pipeline_id, user)
    version_no = svc.publish_draft(db, p, remark=payload.remark, user_id=user.id)
    db.commit()
    return {"version_no": version_no}


@router.post("/{pipeline_id}/draft/discard")
def discard(
    pipeline_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    p = _owned(db, pipeline_id, user)
    svc.discard_draft(db, p)
    db.commit()
    return {"ok": True}


@router.get("/{pipeline_id}/versions")
def list_versions(
    pipeline_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    _owned(db, pipeline_id, user)
    out = []
    for v in svc.list_versions(db, pipeline_id):
        vo = VersionRead.model_validate(v).model_dump()
        vo["snapshot"] = None
        out.append(vo)
    return out


# 预留给「版本详情/diff」UI（当前前端未调用，勿当死代码删）
@router.get("/versions/{version_id}")
def get_version(
    version_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    v = svc.get_version(db, version_id)
    if v is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    _owned(db, v.pipeline_id, user)
    return VersionRead.model_validate(v).model_dump()


@router.post("/versions/{version_id}/rollback")
def rollback(
    version_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    v = svc.get_version(db, version_id)
    if v is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    p = _owned(db, v.pipeline_id, user)
    svc.rollback_to_draft(db, p, v)
    db.commit()
    return {"ok": True}


@router.post("/{pipeline_id}/runs", status_code=202)
def create_run(
    pipeline_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> JSONResponse:
    from server.app.modules.pipelines.executor import create_run as _create_run
    from server.app.modules.pipelines.executor import run_pipeline

    p = _owned(db, pipeline_id, user)
    if not svc.list_nodes(db, p.id):
        raise HTTPException(status_code=400, detail="工作流没有已发布的节点，请先发布")
    run = _create_run(db, pipeline_id=p.id, user_id=user.id)
    db.commit()
    run_id = run.id

    factory = bg_session_factory
    if factory is None:
        import logging

        logging.getLogger(__name__).error("bg_session_factory 未注入，run %s 无法执行", run_id)
        from server.app.modules.pipelines.models import PipelineRun

        run_obj = db.get(PipelineRun, run_id)
        if run_obj is not None:
            run_obj.status = "failed"
            run_obj.error_message = "后台执行器未就绪（bg_session_factory 未注入）"
            db.commit()
        return JSONResponse(status_code=503, content={"run_id": run_id, "status": "failed"})

    def _runner() -> None:
        try:
            run_pipeline(run_id, factory)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("pipeline run %s thread crashed", run_id)

    threading.Thread(target=_runner, daemon=True).start()
    return JSONResponse(status_code=202, content={"run_id": run_id, "status": "pending"})


# 预留给「运行历史」列表 UI（当前前端只轮询单个 run，勿当死代码删）
@router.get("/{pipeline_id}/runs")
def list_runs(
    pipeline_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    from server.app.modules.pipelines.models import PipelineRun

    _owned(db, pipeline_id, user)
    rows = (
        db.query(PipelineRun)
        .filter(PipelineRun.pipeline_id == pipeline_id)
        .order_by(PipelineRun.id.desc())
        .all()
    )
    return [RunRead.model_validate(r).model_dump() for r in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from server.app.modules.pipelines.models import PipelineRun

    r = db.get(PipelineRun, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    _owned(db, r.pipeline_id, user)
    return RunRead.model_validate(r).model_dump()


@router.get("/{pipeline_id}/logs")
def list_run_logs(
    pipeline_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from server.app.modules.pipelines.models import PipelineNode, PipelineRun
    from server.app.modules.pipelines.run_logs import build_run_log_rows

    _owned(db, pipeline_id, user)
    limit = max(1, min(limit, 200))
    name_by_index = {
        n.node_index: n.name
        for n in db.query(PipelineNode).filter(PipelineNode.pipeline_id == pipeline_id).all()
    }
    runs = (
        db.query(PipelineRun)
        .filter(PipelineRun.pipeline_id == pipeline_id)
        .order_by(PipelineRun.id.desc())
        .limit(limit)
        .all()
    )
    rows: list[dict] = []
    for run in runs:
        rows.extend(r.model_dump() for r in build_run_log_rows(run, name_by_index))
    return rows
