# Pipeline 编排路由
"""Pipeline 编排 API（/api/pipelines/*，前端 UI 叫「智能体管理」）：增删改查、草稿 / 发布 / 版本、
触发运行与运行日志。运行在 create_app() 注入的 bg_session_factory 后台线程里跑，无独立工作进程。"""

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
    RunLogPage,
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
    from server.app.modules.pipelines.models import PipelineRun

    if p.tags is None:
        p.tags = []
    nodes = svc.list_nodes(db, p.id)
    data = PipelineRead.model_validate(p).model_dump()
    # 是否有在途运行（pending / running）——与 service 删除前的活跃运行判定一致
    active_run = (
        db.query(PipelineRun.id)
        .filter(
            PipelineRun.pipeline_id == p.id,
            PipelineRun.status.in_(("pending", "running")),
        )
        .first()
    )
    data["is_running"] = active_run is not None
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
    # 节点 config 字段结构，供前端属性面板渲染
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
                    {"key": "model", "type": "ai_engine", "label": "模型"},
                    {
                        "key": "daily_group",
                        "type": "toggle",
                        "label": "边生成边进每日分组",
                        "hint": "开启后：生成前先建好「每日生成 · 日期」分组，每生成一篇立即进组并标待审；"
                        "运行中可实时看到逐篇进组，中途失败也不丢已生成的文章。同一天多次运行并入同一组。",
                        "default": False,
                    },
                ],
            },
            {
                "type": "distribute",
                "label": "内容分发",
                "config_schema": [
                    {"key": "account_selection", "type": "account_selector", "label": "分发账号"},
                    {"key": "name", "type": "text", "label": "任务名(可空)"},
                ],
            },
            {
                "type": "question_source",
                "label": "问题源",
                "config_schema": [
                    {"key": "pool_id", "type": "question_pool", "label": "问题池"},
                    {
                        "key": "question_types",
                        "type": "question_types",
                        "label": "问题类型（多选，留空=全部）",
                    },
                    {
                        "key": "question_record_ids",
                        "type": "question_records",
                        "label": "具体问题（可选，留空=上述类型全部）",
                    },
                ],
            },
            {
                "type": "ai_compose",
                "label": "AI创作",
                "config_schema": [
                    {"key": "ai_engine", "type": "ai_engine", "label": "AI 模型"},
                    # 模型能力开关（默认开）：透传到 litellm。联网搜索＝各家原生（Claude/OpenAI 系
                    # 走 web_search_options，Moonshot/Kimi 走 $web_search builtin 工具循环，其它模型
                    # 自动忽略）；深度思考＝统一 reasoning_effort（不支持的模型 drop_params 静默忽略）。
                    # 均 best-effort：能力不支持/失败时回退普通生文，不拖垮流程。见 article_writer + model_capabilities。
                    {
                        "key": "web_search",
                        "type": "toggle",
                        "label": "联网搜索",
                        "hint": "让模型联网检索后再写；不支持的模型自动忽略",
                        "default": True,
                    },
                    {
                        "key": "deep_thinking",
                        "type": "toggle",
                        "label": "深度思考",
                        "hint": "开启模型推理/扩展思考；不支持的模型自动忽略",
                        "default": True,
                    },
                    {
                        "key": "prompt_template_ids",
                        "type": "prompt_templates",
                        "label": "提示词模板(可多选,运行时随机)",
                    },
                    {"key": "count", "type": "number", "label": "生成数量"},
                    {
                        "key": "daily_group",
                        "type": "toggle",
                        "label": "边生成边进每日分组",
                        "hint": "开启后：生成前先建好「每日生成 · 日期」分组，每生成一篇立即进组并标待审；"
                        "运行中可实时看到逐篇进组，中途失败也不丢已生成的文章。同一天多次运行并入同一组。",
                        "default": False,
                    },
                ],
            },
            {
                "type": "ai_illustrate",
                "label": "AI配图",
                "config_schema": [
                    {
                        "key": "main_category_id",
                        "type": "stock_category_main",
                        "label": "图片库 · 主推游戏",
                    },
                    # 配图风格：开=「积极配图」(每个明确出现的游戏都插，保留"不确定不插"准星)，
                    # 关=保守(图少文多)。默认开。见 ai_format._builtin_prompt_template 的 aggressive 变体。
                    {
                        "key": "aggressive_images",
                        "type": "toggle",
                        "label": "激进配图（每个游戏都插）",
                        "hint": "开=每个明确出现的游戏都配图；关=保守·图少文多",
                        "default": True,
                    },
                    # 数量旋钮：与风格解耦，单独控制上限/间距。留空随风格取默认(激进12/1、保守3/5)，
                    # 同时作为插图阶段硬上限。见 ai_format._maybe_insert_images。
                    {
                        "key": "max_images",
                        "type": "number",
                        "label": "最多配图数（默认激进12 / 保守3）",
                    },
                    {
                        "key": "min_spacing",
                        "type": "number",
                        "label": "最小配图间距·节点数（默认激进1 / 保守5）",
                    },
                    # 联网兜底：开启后，模型可点名可用栏目外的陪衬游戏，执行器自动建栏目 +
                    # 百度千帆搜图补图（需配 GEO_BAIDU_API_KEY）。见 ai_format._maybe_insert_images。
                    {
                        "key": "web_fallback",
                        "type": "toggle",
                        "label": "联网兜底",
                        "hint": "陪衬游戏在库中无图时，联网搜图补充",
                        "default": False,
                    },
                    # 顺带配封面：从主推游戏栏目随机取一张落成 Asset 设为封面，
                    # 仅当文章还没封面时生效。见 image_library.cover.set_random_cover_from_category。
                    {
                        "key": "set_cover",
                        "type": "toggle",
                        "label": "顺带配封面",
                        "hint": "从主推游戏图库随机取一张作封面（仅当文章还没封面）",
                        "default": True,
                    },
                    # AI格式提示词：选「提示词管理 · AI格式」里的模板覆盖排版/配图措辞；
                    # 留空(preset_id 缺省/非 int)= 用内置默认提示词。见 ai_illustrate.run + ai_format._load_ai_format_prompt。
                    {
                        "key": "preset_id",
                        "type": "ai_format_template",
                        "label": "AI格式提示词（留空=内置默认）",
                        "hint": "覆盖排版/配图的系统提示词措辞；不影响数量/间距等旋钮",
                    },
                ],
            },
            {
                "type": "to_review",
                "label": "进入未审核库",
                "config_schema": [
                    {"key": "group_name", "type": "text", "label": "分组名(可空)"},
                    {
                        "key": "daily_group",
                        "type": "toggle",
                        "label": "按天归组",
                        "hint": "开启后，当天所有运行/流水线产出并入同一个「每日生成 · 日期」分组",
                        "default": False,
                    },
                ],
            },
            {
                "type": "approved_content_source",
                "label": "已审核待发布",
                "config_schema": [
                    {"key": "limit", "type": "number", "label": "取多少篇(默认20)"},
                    {
                        "key": "exclude_distributed",
                        "type": "checkbox",
                        "label": "跳过已分发过的",
                        "default": True,
                    },
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


# 预留给「版本详情/差异」UI（当前前端未调用，勿当死代码删）
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


# 预留给「运行历史」列表 UI（当前前端只轮询单个运行，勿当死代码删）
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
    page: int = 1,
    page_size: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from server.app.modules.pipelines.run_logs import (
        beijing_day_to_utc_range,
        list_run_log_page,
    )

    _owned(db, pipeline_id, user)
    page = max(1, page)
    page_size = page_size if page_size in (20, 30) else 30
    try:
        start_dt, end_dt = beijing_day_to_utc_range(start_date, end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD") from None

    rows, total = list_run_log_page(
        db, pipeline_id, page=page, page_size=page_size, start_dt=start_dt, end_dt=end_dt
    )
    return RunLogPage(items=rows, total=total, page=page, page_size=page_size).model_dump()
