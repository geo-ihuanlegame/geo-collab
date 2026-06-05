# server/app/modules/pipelines/executor.py
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from server.app.core.time import utcnow
from server.app.modules.pipelines.flow_meta import apply_input_mapping, should_skip
from server.app.modules.pipelines.models import Pipeline, PipelineNode, PipelineRun
from server.app.modules.pipelines.nodes.base import NodeRunContext, get_handler

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Any]


def create_run(db, *, pipeline_id: int, user_id: int) -> PipelineRun:
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

    for spec in node_specs:
        idx = spec["node_index"]
        meta = spec["flow_meta"]
        # 上游视图：按 dependsOnIndex 取指定节点输出，否则合并全部已执行输出
        if meta and meta.get("dependsOnIndex") is not None:
            upstream = context.get(meta["dependsOnIndex"], {})
        else:
            upstream = {k: v for out in context.values() for k, v in out.items()}

        if should_skip(meta, upstream):
            node_results[str(idx)] = {"skipped": True}
            continue

        inputs = apply_input_mapping(meta, upstream)
        node_failed = False
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
                node_failed = True
            if result.article_ids or spec["node_type"] == "input":
                had_success = True
        except Exception as exc:
            logger.exception("pipeline run %s node #%s failed", run_id, idx)
            node_results[str(idx)] = {"error": str(exc)}
            had_failure = True
            node_failed = True

        if node_failed and not ignore_exception:
            break  # fail-fast：停掉后续节点

    # 聚合状态
    if had_failure and had_success:
        status = "partial_failed"
    elif had_failure:
        status = "failed"
    else:
        status = "done"

    db = session_factory()
    try:
        run = db.get(PipelineRun, run_id)
        if run is not None:
            run.status = status
            run.node_results = node_results
            run.article_ids = article_ids
            run.completed_at = utcnow()
            db.commit()
    finally:
        db.close()

    # Track A: 产出文章 → pending + 成组（best-effort，不影响 run 状态）
    if article_ids:
        try:
            from server.app.modules.articles.service import mark_pending_and_group

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
                mark_pending_and_group(
                    session_factory, article_ids=article_ids, user_id=uid, base_name=base_name
                )
        except Exception:  # noqa: BLE001
            logger.exception("pipeline run %s post-grouping failed", run_id)
