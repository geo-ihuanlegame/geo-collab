from __future__ import annotations

from datetime import datetime, timedelta

from server.app.modules.pipelines.schemas import RunLogRow

_BEIJING_OFFSET = timedelta(hours=8)


def build_run_log_rows(run, name_by_index: dict[int, str]) -> list[RunLogRow]:
    """把单条 PipelineRun 的 node_results 摊平成日志行（按节点下标升序）。

    run 需具备 id / status / node_results / completed_at / created_at 属性。
    纯函数、无 DB 依赖，便于单测。
    """
    rows: list[RunLogRow] = []
    results = run.node_results or {}
    for key in sorted(results, key=lambda k: int(k)):
        idx = int(key)
        data = results[key] or {}
        if "error" in data:
            level, message = "ERROR", str(data["error"])
        elif data.get("errors"):
            level, message = "ERROR", "; ".join(str(e) for e in data["errors"])
        elif data.get("skipped"):
            level, message = "INFO", "已跳过"
        else:
            level, message = "INFO", "运行成功"
        rows.append(
            RunLogRow(
                batch=run.id,
                run_status=run.status,
                step=idx,
                task_name=name_by_index.get(idx, f"步骤 {idx}"),
                level=level,
                message=message,
                time=run.completed_at or run.created_at,
            )
        )
    return rows


def beijing_day_to_utc_range(
    start_date: str | None, end_date: str | None
) -> tuple[datetime | None, datetime | None]:
    """把北京日历日 YYYY-MM-DD 起止转成朴素 UTC 的半开区间 [start, end)。

    end_date 取「次日北京零点」作为开区间上界。解析失败抛 ValueError（调用方转 400）。
    产品 China-only，固定 +08:00（与前端 fmtTime 的 Asia/Shanghai 一致）。
    """
    start_dt = None
    end_dt = None
    if start_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d") - _BEIJING_OFFSET
    if end_date:
        end_dt = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)) - _BEIJING_OFFSET
    return start_dt, end_dt
