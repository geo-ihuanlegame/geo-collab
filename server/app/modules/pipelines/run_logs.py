from __future__ import annotations

from server.app.modules.pipelines.schemas import RunLogRow


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
