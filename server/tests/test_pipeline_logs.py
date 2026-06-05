from datetime import datetime
from types import SimpleNamespace

import pytest

from server.tests.utils import build_test_app


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


def _publish_three_node_pipeline(client, name="日志测试"):
    pid = client.post("/api/pipelines", json={"name": name, "type": "generation"}).json()["id"]
    snapshot = {
        "schemaVersion": 1,
        "nodes": [
            {
                "node_type": "question_source",
                "name": "问题源",
                "node_index": 0,
                "config": {},
                "flow_meta": None,
            },
            {
                "node_type": "ai_compose",
                "name": "AI创作",
                "node_index": 1,
                "config": {},
                "flow_meta": None,
            },
            {
                "node_type": "to_review",
                "name": "进入未审核库",
                "node_index": 2,
                "config": {},
                "flow_meta": None,
            },
        ],
    }
    client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
    client.post(f"/api/pipelines/{pid}/publish", json={})
    return pid


def _add_run(app, pid, node_results, status="partial_failed"):
    from server.app.modules.pipelines.models import PipelineRun
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        uid = db.query(User).first().id
        run = PipelineRun(
            pipeline_id=pid, user_id=uid, status=status, node_results=node_results, article_ids=[]
        )
        db.add(run)
        db.commit()
        return run.id


@pytest.mark.mysql
def test_logs_endpoint_flattens_run(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        _add_run(
            app,
            pid,
            {"0": {"question_count": 3}, "1": {"errors": ["模板X无效"]}, "2": {"skipped": True}},
        )
        rows = client.get(f"/api/pipelines/{pid}/logs").json()
        assert [r["step"] for r in rows] == [0, 1, 2]
        assert rows[0]["task_name"] == "问题源"
        assert rows[0]["level"] == "INFO" and rows[0]["message"] == "运行成功"
        assert rows[1]["level"] == "ERROR" and "模板X无效" in rows[1]["message"]
        assert rows[2]["message"] == "已跳过"
        assert rows[0]["run_status"] == "partial_failed"
        assert all(r["batch"] for r in rows)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_newest_batch_first(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        first = _add_run(app, pid, {"0": {"ok": 1}}, status="done")
        second = _add_run(app, pid, {"0": {"ok": 1}}, status="done")
        assert second > first
        rows = client.get(f"/api/pipelines/{pid}/logs").json()
        # 较新批次的行在前
        assert rows[0]["batch"] == second
        assert rows[-1]["batch"] == first
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_empty_and_not_found(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = client.post("/api/pipelines", json={"name": "空日志", "type": "general"}).json()["id"]
        assert client.get(f"/api/pipelines/{pid}/logs").json() == []  # 无 run
        assert client.get("/api/pipelines/999999/logs").status_code == 404  # _owned 守卫
    finally:
        app.cleanup()
