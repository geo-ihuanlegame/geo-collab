"""运行日志：把 PipelineRun.node_results 摊平成「日志行」并按行做服务端分页。

日志行的粒度是单个节点结果，一次运行产出多行；分页跨多条运行切片（见 list_run_log_page）。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func

from server.app.modules.pipelines.schemas import RunLogRow

_BEIJING_OFFSET = timedelta(hours=8)


def _success_message(data: dict) -> str:
    """成功节点的摘要消息：产文篇数 / 进组 / 建任务，无业务产出则「运行成功」。"""
    parts: list[str] = []
    aids = data.get("article_ids")
    if isinstance(aids, list) and aids:
        parts.append(f"生成 {len(aids)} 篇")
    if data.get("group_id"):
        parts.append(f"进组 {data['group_id']}")
    if data.get("task_id"):
        parts.append(f"建任务 {data['task_id']}")
    return "；".join(parts) if parts else "运行成功"


def build_run_log_rows(run, name_by_index: dict[int, str]) -> list[RunLogRow]:
    """把单条 PipelineRun 的 node_results 摊平成日志行（按节点下标升序）。

    运行对象需具备 id / status / node_results / completed_at / created_at 属性。
    node_results 各节点项除业务输出外，执行器还富化了 duration_ms / error_type（best-effort，旧运行没有）。
    纯函数、无 DB 依赖，便于单测。
    """
    rows: list[RunLogRow] = []
    results = run.node_results or {}
    for key in sorted(results, key=lambda k: int(k)):
        idx = int(key)
        data = results[key] or {}
        duration_ms = data.get("duration_ms") if isinstance(data, dict) else None
        if "error" in data:
            level = "ERROR"
            etype = data.get("error_type")
            message = f"[{etype}] {data['error']}" if etype else str(data["error"])
        elif data.get("errors"):
            level, message = "ERROR", "; ".join(str(e) for e in data["errors"])
        elif data.get("skipped"):
            level, message = "INFO", "已跳过"
        else:
            level, message = "INFO", _success_message(data)
        rows.append(
            RunLogRow(
                batch=run.id,
                run_status=run.status,
                step=idx,
                task_name=name_by_index.get(idx, f"步骤 {idx}"),
                level=level,
                message=message,
                duration_ms=duration_ms if isinstance(duration_ms, int) else None,
                time=run.completed_at or run.created_at,
            )
        )
    return rows


def beijing_day_to_utc_range(
    start_date: str | None, end_date: str | None
) -> tuple[datetime | None, datetime | None]:
    """把北京日历日 YYYY-MM-DD 起止转成朴素 UTC 的半开区间 [start, end)。

    end_date 取「次日北京零点」作为开区间上界。解析失败抛 ValueError（调用方转 400）。
    产品仅面向中国时区，固定 +08:00（与前端 fmtTime 的 Asia/Shanghai 一致）。
    """
    start_dt = None
    end_dt = None
    if start_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d") - _BEIJING_OFFSET
    if end_date:
        end_dt = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)) - _BEIJING_OFFSET
    return start_dt, end_dt


def list_run_log_page(
    db, pipeline_id, *, page: int, page_size: int, start_dt, end_dt
) -> tuple[list[RunLogRow], int]:
    """按「日志行」服务端分页。返回 (当前页行, 满足筛选的总行数)。

    A 方案：SUM(JSON_LENGTH) 取精确总行数；游走 (id, 行数) 整型找到覆盖
    [offset, offset+page_size) 的运行，只对这些运行加载 node_results 摊平后精确切片。
    """
    from server.app.modules.pipelines.models import PipelineNode, PipelineRun

    name_by_index = {
        n.node_index: n.name
        for n in db.query(PipelineNode).filter(PipelineNode.pipeline_id == pipeline_id).all()
    }
    time_col = func.coalesce(PipelineRun.completed_at, PipelineRun.created_at)
    rowcount_col = func.coalesce(func.json_length(PipelineRun.node_results), 0)

    base = db.query(PipelineRun).filter(PipelineRun.pipeline_id == pipeline_id)
    if start_dt is not None:
        base = base.filter(time_col >= start_dt)
    if end_dt is not None:
        base = base.filter(time_col < end_dt)

    total = int(base.with_entities(func.coalesce(func.sum(rowcount_col), 0)).scalar() or 0)
    offset = (page - 1) * page_size
    if offset >= total:
        return [], total

    id_counts = (
        base.with_entities(PipelineRun.id, rowcount_col)
        .order_by(time_col.desc(), PipelineRun.id.desc())
        .all()
    )
    cum = 0
    skip_in_first = 0
    window_ids: list[int] = []
    for run_id, cnt in id_counts:
        cnt = int(cnt)
        if cnt == 0:
            continue
        if cum + cnt <= offset:
            cum += cnt
            continue
        if not window_ids:
            skip_in_first = offset - cum
        window_ids.append(run_id)
        cum += cnt
        if cum >= offset + page_size:
            break

    if not window_ids:
        return [], total

    runs = {r.id: r for r in db.query(PipelineRun).filter(PipelineRun.id.in_(window_ids)).all()}
    rows: list[RunLogRow] = []
    for rid in window_ids:  # 保持时间倒序
        rows.extend(build_run_log_rows(runs[rid], name_by_index))
    # 首个窗口内第一条运行之前要丢弃的前导行数（窗口行已按时间倒序拼好）
    return rows[skip_in_first : skip_in_first + page_size], total
