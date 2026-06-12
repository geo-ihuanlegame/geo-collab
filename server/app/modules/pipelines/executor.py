# Pipeline 运行执行器
"""Pipeline 运行执行器：create_run 冻结节点快照并行锁去重，run_pipeline 在后台线程线性
跑节点、按上游成败聚合运行终态。无独立工作进程，跑在 API 服务后台线程；全局并发受
_RUN_SEMAPHORE（GEO_PIPELINE_MAX_CONCURRENT_RUNS）限制。"""

from __future__ import annotations

import logging
import threading as _threading
from collections.abc import Callable
from typing import Any

from server.app.core.config import get_settings as _get_settings
from server.app.core.time import utcnow
from server.app.modules.articles.service import mark_pending_and_group
from server.app.modules.pipelines.flow_meta import apply_input_mapping, should_skip
from server.app.modules.pipelines.models import Pipeline, PipelineNode, PipelineRun
from server.app.modules.pipelines.nodes.base import NodeRunContext, get_handler
from server.app.modules.pipelines.snapshot import nodes_to_snapshot, snapshot_to_node_dicts
from server.app.shared.errors import ConflictError

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Any]

# 全局并发闸：限制单进程同时执行的 pipeline 运行数（与单主实例约束配合即为全局上限）
_RUN_SEMAPHORE = _threading.Semaphore(max(1, _get_settings().pipeline_max_concurrent_runs))


def create_run(db, *, pipeline_id: int, user_id: int) -> PipelineRun:
    """创建一条待处理运行并冻结当前实时节点为快照。

    在 pipeline 行锁内检查活跃运行，已有 pending/running 时抛 ConflictError（同一 pipeline 不并发）。
    不提交事务，由调用方提交。
    """
    # 串行化同一 pipeline 的运行创建：锁住 pipeline 行后检查活跃运行，避免并发重复运行
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
    # 冻结当前实时节点为快照：执行只读快照，创建→执行之间的发布不影响本次运行
    nodes = (
        db.query(PipelineNode)
        .filter(PipelineNode.pipeline_id == pipeline_id)
        .order_by(PipelineNode.node_index.asc())
        .all()
    )
    run = PipelineRun(
        pipeline_id=pipeline_id,
        user_id=user_id,
        status="pending",
        node_results={},
        article_ids=[],
        snapshot=nodes_to_snapshot(nodes),
    )
    db.add(run)
    db.flush()
    return run


def _run_pipeline_inner(run_id: int, session_factory: SessionFactory) -> None:
    """后台线程入口：线性执行节点，聚合运行状态。"""
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
        if run.snapshot:
            # 优先读运行快照（创建时冻结）；旧运行无快照时回退实时节点
            node_specs = [
                {
                    "node_type": d["node_type"],
                    "node_index": d["node_index"],
                    "config": d.get("config") or {},
                    "flow_meta": d.get("flow_meta"),
                }
                for d in snapshot_to_node_dicts(run.snapshot)
            ]
        else:
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
    # 是否已有节点（如 to_review）真正成了组：判"成功执行"而非"节点存在"，
    # 避免 to_review 被跳过/漏配/失败时执行器误以为已成组、把文章留成孤儿。
    grouped = False
    failed_indices: set[int] = set()

    for spec in node_specs:
        idx = spec["node_index"]
        meta = spec["flow_meta"]
        # 上游视图：按 dependsOnIndex 取指定节点输出，否则合并全部已执行输出
        if meta and meta.get("dependsOnIndex") is not None:
            upstream = context.get(meta["dependsOnIndex"], {})
        else:
            upstream = {k: v for out in context.values() for k, v in out.items()}

        # 上游依赖失败 → 阻断本节点，避免拿空上游输出静默回退 config 产生副作用。
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
            if result.output.get("group_id"):
                grouped = True
            if result.output.get("errors"):
                had_failure = True
            # 成功标记仅在真正产出业务结果时置位：
            # ai_generate 产文(result.article_ids) 或 distribute 建任务(output.task_id)。
            # input / 读取类节点(article_group_source) 不计入成功，避免零产出被误判为部分失败。
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

    # 路径 A：产出文章 → pending + 成组。先成组拿到结果，再一次性写终态，消除 done→partial_failed 闪烁。
    # to_review 节点已成组时由它接管，执行器不重复成组；否则执行器兜底成组，
    # 防止 to_review 被跳过/漏配/失败留下孤儿文章。失败不能静默——会让未审文章被误用：
    # 成组失败时把 done 降级 partial_failed 并写明原因。
    if article_ids and not grouped:
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
            note = "文章已生成但送审/成组失败，请手动核对审核状态"
            error_message = f"{error_message}; {note}" if error_message else note
            if status == "done":
                status = "partial_failed"

    # 一次性写终态：status / node_results / article_ids / error_message / completed_at 写一次到位
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


def run_pipeline(run_id: int, session_factory: SessionFactory) -> None:
    """后台线程入口：占全局并发信号量后执行运行；顶层异常兜底把 pending/running 置 failed。"""
    try:
        with _RUN_SEMAPHORE:
            _run_pipeline_inner(run_id, session_factory)
    except Exception:
        logger.exception("pipeline run %s crashed at top level", run_id)
        db = session_factory()
        try:
            run = db.get(PipelineRun, run_id)
            if run is not None and run.status in ("pending", "running"):
                run.status = "failed"
                run.error_message = "执行器内部异常，运行已中止"
                run.completed_at = utcnow()
                db.commit()
        finally:
            db.close()
