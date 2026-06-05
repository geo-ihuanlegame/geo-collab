from datetime import datetime
from types import SimpleNamespace


def test_build_run_log_rows_levels_and_order():
    from server.app.modules.pipelines.run_logs import build_run_log_rows

    run = SimpleNamespace(
        id=7,
        status="partial_failed",
        node_results={
            "2": {"skipped": True},
            "0": {"question_count": 3},
            "1": {"errors": ["X无效"]},
        },
        completed_at=datetime(2026, 6, 5, 8, 0, 0),
        created_at=datetime(2026, 6, 5, 7, 0, 0),
    )
    names = {0: "问题源", 1: "AI创作", 2: "进入未审核库"}
    rows = build_run_log_rows(run, names)
    assert [r.step for r in rows] == [0, 1, 2]  # 按下标升序
    assert (
        rows[0].level == "INFO" and rows[0].message == "运行成功" and rows[0].task_name == "问题源"
    )
    assert rows[1].level == "ERROR" and "X无效" in rows[1].message
    assert rows[2].level == "INFO" and rows[2].message == "已跳过"
    assert all(r.batch == 7 and r.run_status == "partial_failed" for r in rows)
    assert rows[0].time == datetime(2026, 6, 5, 8, 0, 0)  # 优先 completed_at


def test_build_run_log_rows_error_fallback_name_and_time():
    from server.app.modules.pipelines.run_logs import build_run_log_rows

    run = SimpleNamespace(
        id=9,
        status="failed",
        node_results={"0": {"error": "boom"}, "5": {"foo": 1}},
        completed_at=None,
        created_at=datetime(2026, 6, 5, 7, 0, 0),
    )
    rows = build_run_log_rows(run, {0: "问题源"})  # 下标 5 无对应节点名
    assert rows[0].level == "ERROR" and rows[0].message == "boom"
    assert rows[1].task_name == "步骤 5"  # 回退
    assert rows[1].message == "运行成功"  # 非错误/跳过 → 兜底
    assert rows[0].time == datetime(2026, 6, 5, 7, 0, 0)  # completed_at 缺 → created_at


def test_build_run_log_rows_empty():
    from server.app.modules.pipelines.run_logs import build_run_log_rows

    run = SimpleNamespace(
        id=1,
        status="running",
        node_results={},
        completed_at=None,
        created_at=datetime(2026, 6, 5, 7, 0, 0),
    )
    assert build_run_log_rows(run, {}) == []
