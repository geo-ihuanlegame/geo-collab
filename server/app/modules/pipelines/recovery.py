# server/app/modules/pipelines/recovery.py
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.modules.pipelines.models import PipelineRun

logger = logging.getLogger(__name__)


def recover_stuck_pipeline_runs(db: Session) -> None:
    """启动时复位上次崩溃残留的 pipeline run。

    进程刚启动时没有任何 run 真正在执行，因此所有 running/pending 都是僵死残留，
    直接置 failed（无租约机制，故不按阈值，全量复位）。
    """
    now = utcnow()
    runs = list(
        db.execute(select(PipelineRun).where(PipelineRun.status.in_(("running", "pending"))))
        .scalars()
        .all()
    )
    for run in runs:
        run.status = "failed"
        run.error_message = "进程重启：运行在上次执行中意外中断"
        run.completed_at = now
    if runs:
        logger.warning("Recovered %d stuck pipeline runs: %s", len(runs), [r.id for r in runs])
        db.commit()
