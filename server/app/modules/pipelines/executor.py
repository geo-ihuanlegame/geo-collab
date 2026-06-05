# server/app/modules/pipelines/executor.py
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from server.app.core.time import utcnow
from server.app.modules.articles.service import mark_pending_and_group
from server.app.modules.pipelines.flow_meta import apply_input_mapping, should_skip
from server.app.modules.pipelines.models import Pipeline, PipelineNode, PipelineRun
from server.app.modules.pipelines.nodes.base import NodeRunContext, get_handler
from server.app.shared.errors import ConflictError

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Any]


def create_run(db, *, pipeline_id: int, user_id: int) -> PipelineRun:
    # 串行化同一 pipeline 的 run 创建：锁住 pipeline 行后检查活跃 run，避免并发重复运行
    db.query(Pipeline).filter(Pipeline.id == pipeline_id).with_for_update().first()
    active = (
        db.query(PipelineRun.id)
        .filter(
            PipelineRun.pipeline_id == pipeline_id,
            PipelineRun.status.in_(("pending", "running")),
        )
        .first()
    )
    if active is not None:
        raise ConflictError("该工作流已有正在运行的任务，请等待其完成后再运行")
    run = PipelineRun(
        pipeline_id=pipeline_id,
        user_id=user_id,
        status="pending",
        node_results={},
        article_ids=[],
    )
    db.add(run)
    db.flush()
    return run


def run_pipeline(run_id: int, session_factory: SessionFactory) -> None:
    """后台线程入口：线性执行节点，聚合 run 状态。"""
    db = session_factory()
    try:
        run = db.get(PipelineRun, run_id)
        if run is None:
            logger.error("run_pipeline: run %s not found", run_id)
            return
        run.status = "running"
        pipeline_id, user_id = run.pipeline_id, run.user_id
        pipeline = db.get(Pipeline, pipeline_id)
        ignore_exception = bool(pipeline.ignore_exception) if pipeline is not None else False
        nodes = (
            db.query(PipelineNode)
            .filter(PipelineNode.pipeline_id == pipeline_id)
            .order_by(PipelineNode.node_index.asc())
            .all()
        )
        node_specs = [
            {
                "node_type": n.node_type,
                "node_index": n.node_index,
                "config": n.config or {},
                "flow_meta": n.flow_meta,
            }
            for n in nodes
        ]
        db.commit()
    finally:
        db.close()

    context: dict[int, dict] = {}  # node_index -> output
    node_results: dict[str, Any] = {}
    article_ids: list[int] = []
    had_success = False
    had_failure = False
    failed_indices: set[int] = set()

    for spec in node_specs:
        idx = spec["node_index"]
        meta = spec["flow_meta"]
        # 上游视图：按 dependsOnIndex 取指定节点输出，否则合并全部已执行输出
        if meta and meta.get("dependsOnIndex") is not None:
            upstream = context.get(meta["dependsOnIndex"], {})
        else:
            upstream = {k: v for out in context.values() for k, v in out.items()}

        # 上游依赖失败 → 阻断本节点，避免拿空 upstream 静默回退 config 产生副作用。
        # ignore_exception=True 时不阻断：允许"忽略异常、继续往下跑"（出错的下游自负其责）。
        dep = meta.get("dependsOnIndex") if meta else None
        if dep is not None and dep in failed_indices and not ignore_exception:
            node_results[str(idx)] = {"error": f"上游节点 #{dep} 失败，已中止本节点"}
            had_failure = True
            failed_indices.add(idx)
            continue

        if should_skip(meta, upstream):
            node_results[str(idx)] = {"skipped": True}
            continue

        inputs = apply_input_mapping(meta, upstream)
        try:
            handler = get_handler(spec["node_type"])
            result = handler(
                NodeRunContext(
                    session_factory=session_factory,
                    user_id=user_id,
                    config=spec["config"],
                    inputs=inputs,
                    upstream=upstream,
                )
            )
            context[idx] = result.output
            node_results[str(idx)] = result.output
            article_ids.extend(result.article_ids)
            if result.output.get("errors"):
                had_failure = True
            # had_success 仅在真正产出业务结果时置位：
            # ai_generate 产文(result.article_ids) 或 distribute 建任务(output.task_id)。
            # input / 读取类节点(article_group_source) 不计入成功，避免零产出被误判 partial。
            if result.article_ids or result.output.get("task_id"):
                had_success = True
        except Exception as exc:
            logger.exception("pipeline run %s node #%s failed", run_id, idx)
            node_results[str(idx)] = {"error": str(exc)}
            had_failure = True
            failed_indices.add(idx)

    # 聚合状态
    if had_failure and had_success:
        status = "partial_failed"
    elif had_failure:
        status = "failed"
    else:
        status = "done"

    # 汇总各节点错误，写入 run.error_message（失败原因不止埋在 node_results）
    error_parts: list[str] = []
    for k, v in node_results.items():
        if isinstance(v, dict):
            if v.get("error"):
                error_parts.append(f"node#{k}: {v['error']}")
            elif v.get("errors"):
                error_parts.append(f"node#{k}: {'; '.join(str(e) for e in v['errors'])}")
    error_message = "; ".join(error_parts)[:2000] or None

    db = session_factory()
    try:
        run = db.get(PipelineRun, run_id)
        if run is not None:
            run.status = status
            run.node_results = node_results
            run.article_ids = article_ids
            run.error_message = error_message
            run.completed_at = utcnow()
            db.commit()
    finally:
        db.close()

    # Track A: 产出文章 → pending + 成组。失败不能静默——会让未审文章被误用
    if article_ids:
        gid = None
        try:
            db = session_factory()
            try:
                run = db.get(PipelineRun, run_id)
                p = db.get(Pipeline, run.pipeline_id) if run is not None else None
                pname = p.name if p is not None else f"工作流 {run_id}"
                created = run.created_at if run is not None else None
                base_name = (
                    f"{created:%Y/%m/%d %H:%M} · {pname}" if created else f"{pname} #{run_id}"
                )
                uid = run.user_id if run is not None else None
            finally:
                db.close()
            if uid is not None:
                gid = mark_pending_and_group(
                    session_factory,
                    article_ids=article_ids,
                    user_id=uid,
                    base_name=base_name,
                    fallback_suffix=f"#{run_id}",
                )
        except Exception:  # noqa: BLE001
            logger.exception("pipeline run %s post-grouping failed", run_id)

        if gid is None:
            # 成组/送审失败：降级 run 状态 + 写明原因，避免 UI 显示成功
            db = session_factory()
            try:
                run = db.get(PipelineRun, run_id)
                if run is not None:
                    if run.status == "done":
                        run.status = "partial_failed"
                    note = "文章已生成但送审/成组失败，请手动核对审核状态"
                    run.error_message = (
                        f"{run.error_message}; {note}" if run.error_message else note
                    )
                    db.commit()
            finally:
                db.close()
