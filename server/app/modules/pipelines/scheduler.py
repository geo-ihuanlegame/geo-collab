"""Pipeline 定时调度：镜像 ai_generation.sync_scheduler。run_due_pipelines_once 纯函数式可测，
后台线程只负责 wait→run_once。create_app 在 GEO_PIPELINE_SCHEDULER_ENABLED 时启动。"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import update

from server.app.core.config import get_settings
from server.app.modules.pipelines.executor import create_run, run_pipeline
from server.app.modules.pipelines.models import Pipeline, PipelineNode, PipelineRun
from server.app.modules.pipelines.schedule_calc import current_slot, in_window

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Any]

_stop = threading.Event()
_thread: threading.Thread | None = None


def _to_utc_naive(slot_local: dt.datetime) -> dt.datetime:
    return slot_local.astimezone(dt.UTC).replace(tzinfo=None)


def run_due_pipelines_once(session_factory: SessionFactory, now: dt.datetime | None = None) -> int:
    """扫描到期 pipeline 并触发。now 为带本地时区 datetime（默认按 GEO_SCHEDULER_TZ 取当前）。
    返回触发数。best-effort：单个失败只记日志。"""
    if now is None:
        now = dt.datetime.now(ZoneInfo(get_settings().scheduler_tz))
    triggered = 0
    db = session_factory()
    try:
        candidates = (
            db.query(Pipeline)
            .filter(Pipeline.is_enabled.is_(True), Pipeline.schedule_kind != "none")
            .all()
        )
        rows = [
            (
                p.id,
                p.schedule_kind,
                p.schedule_minute,
                p.schedule_hour,
                p.schedule_weekday,
                p.window_start,
                p.window_end,
            )
            for p in candidates
        ]
    finally:
        db.close()

    for pid, kind, minute, hour, weekday, w_start, w_end in rows:
        try:
            slot_local = current_slot(kind, minute, hour, weekday, now)
            if slot_local is None or not in_window(w_start, w_end, now):
                continue
            slot_utc = _to_utc_naive(slot_local)
            db = session_factory()
            try:
                # 无已发布节点 → 跳过
                has_nodes = (
                    db.query(PipelineNode.id).filter(PipelineNode.pipeline_id == pid).first()
                )
                if has_nodes is None:
                    continue
                # 运行中不重叠
                running = (
                    db.query(PipelineRun.id)
                    .filter(
                        PipelineRun.pipeline_id == pid,
                        PipelineRun.status.in_(("pending", "running")),
                    )
                    .first()
                )
                if running is not None:
                    continue
                # claim：条件 UPDATE，rowcount==1 才算抢到
                res = db.execute(
                    update(Pipeline)
                    .where(
                        Pipeline.id == pid,
                        (Pipeline.last_scheduled_run_at.is_(None))
                        | (Pipeline.last_scheduled_run_at < slot_utc),
                    )
                    .values(last_scheduled_run_at=slot_utc)
                )
                db.commit()
                if res.rowcount != 1:
                    continue
                p = db.get(Pipeline, pid)
                run = create_run(db, pipeline_id=pid, user_id=p.user_id)
                db.commit()
                run_id = run.id
            finally:
                db.close()
            threading.Thread(
                target=run_pipeline, args=(run_id, session_factory), daemon=True
            ).start()
            triggered += 1
        except Exception:  # noqa: BLE001
            logger.exception("scheduler: pipeline %s trigger failed", pid)
    return triggered


def start_pipeline_scheduler(session_factory: SessionFactory) -> bool:
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()

    def _loop() -> None:
        while not _stop.is_set():
            try:
                run_due_pipelines_once(session_factory)
            except Exception:  # noqa: BLE001
                logger.exception("pipeline scheduler loop error")
            interval = max(30, get_settings().pipeline_scheduler_interval_seconds)
            if _stop.wait(interval):
                break

    _thread = threading.Thread(target=_loop, daemon=True, name="pipeline-scheduler")
    _thread.start()
    return True


def stop_pipeline_scheduler() -> None:
    _stop.set()
