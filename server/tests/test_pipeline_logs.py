from datetime import datetime
from types import SimpleNamespace

import pytest

from server.tests.utils import build_test_app


def test_beijing_day_to_utc_range():
    from server.app.modules.pipelines.run_logs import beijing_day_to_utc_range

    # 北京 2026-06-05 这一天 → UTC [2026-06-04 16:00, 2026-06-05 16:00)
    start_dt, end_dt = beijing_day_to_utc_range("2026-06-05", "2026-06-05")
    assert start_dt == datetime(2026, 6, 4, 16, 0, 0)
    assert end_dt == datetime(2026, 6, 5, 16, 0, 0)

    # 仅 start / 仅 end / 都空
    s_only, e_none = beijing_day_to_utc_range("2026-06-05", None)
    assert s_only == datetime(2026, 6, 4, 16, 0, 0) and e_none is None
    n_start, e_only = beijing_day_to_utc_range(None, "2026-06-05")
    assert n_start is None and e_only == datetime(2026, 6, 5, 16, 0, 0)
    assert beijing_day_to_utc_range(None, None) == (None, None)


def test_beijing_day_to_utc_range_bad_format():
    import pytest as _pytest

    from server.app.modules.pipelines.run_logs import beijing_day_to_utc_range

    with _pytest.raises(ValueError):
        beijing_day_to_utc_range("2026-13-99", None)


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


def test_build_run_log_rows_enriched_duration_and_summary():
    """富化：node_results 带 duration_ms / article_ids / error_type → 行带耗时、摘要、异常类型。"""
    from server.app.modules.pipelines.run_logs import build_run_log_rows

    run = SimpleNamespace(
        id=11,
        status="partial_failed",
        node_results={
            "0": {"article_ids": [1, 2, 3], "group_id": 9, "duration_ms": 4200},
            "1": {"error": "boom", "error_type": "ValidationError", "duration_ms": 12},
            "2": {"duration_ms": 5},  # 无业务产出 → 运行成功
        },
        completed_at=datetime(2026, 6, 5, 8, 0, 0),
        created_at=datetime(2026, 6, 5, 7, 0, 0),
    )
    rows = build_run_log_rows(run, {0: "AI创作", 1: "配图", 2: "进未审"})
    assert rows[0].level == "INFO"
    assert "生成 3 篇" in rows[0].message and "进组 9" in rows[0].message
    assert rows[0].duration_ms == 4200
    assert rows[1].level == "ERROR" and rows[1].message == "[ValidationError] boom"
    assert rows[1].duration_ms == 12
    assert rows[2].message == "运行成功" and rows[2].duration_ms == 5


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


def _add_run(app, pid, node_results, status="partial_failed", completed_at=None):
    from server.app.modules.pipelines.models import PipelineRun
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        uid = db.query(User).first().id
        run = PipelineRun(
            pipeline_id=pid,
            user_id=uid,
            status=status,
            node_results=node_results,
            article_ids=[],
            completed_at=completed_at,
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
        body = client.get(f"/api/pipelines/{pid}/logs").json()
        rows = body["items"]
        assert body["total"] == 3
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
        rows = client.get(f"/api/pipelines/{pid}/logs").json()["items"]
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
        body = client.get(f"/api/pipelines/{pid}/logs").json()
        assert body["items"] == [] and body["total"] == 0  # 无 run
        assert client.get("/api/pipelines/999999/logs").status_code == 404  # _owned 守卫
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_paginate_by_row_across_batches(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        # 8 批 × 每批 3 行 = 24 行，id 递增 → 最新（高 id）在前
        ids = [
            _add_run(app, pid, {"0": {"ok": 1}, "1": {"ok": 1}, "2": {"ok": 1}}, status="done")
            for _ in range(8)
        ]
        p1 = client.get(f"/api/pipelines/{pid}/logs?page=1&page_size=20").json()
        p2 = client.get(f"/api/pipelines/{pid}/logs?page=2&page_size=20").json()
        assert p1["total"] == 24 and p2["total"] == 24
        assert len(p1["items"]) == 20 and len(p2["items"]) == 4
        combined = p1["items"] + p2["items"]
        keys = [(r["batch"], r["step"]) for r in combined]
        assert len(keys) == 24 and len(set(keys)) == 24  # 不重不漏
        # 第 7 新批次 = ids[1]，其 step1 被切到第 1 页末、step2 在第 2 页首（边界连续）
        assert p1["items"][-1]["batch"] == ids[1] and p1["items"][-1]["step"] == 1
        assert p2["items"][0]["batch"] == ids[1] and p2["items"][0]["step"] == 2
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_date_range_beijing_boundary(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        # A：北京 2026-06-04 23:30 = UTC 06-04 15:30；B：北京 2026-06-05 00:30 = UTC 06-04 16:30
        a = _add_run(
            app, pid, {"0": {"ok": 1}}, status="done", completed_at=datetime(2026, 6, 4, 15, 30)
        )
        b = _add_run(
            app, pid, {"0": {"ok": 1}}, status="done", completed_at=datetime(2026, 6, 4, 16, 30)
        )
        d5 = client.get(
            f"/api/pipelines/{pid}/logs?start_date=2026-06-05&end_date=2026-06-05"
        ).json()
        assert [r["batch"] for r in d5["items"]] == [b] and d5["total"] == 1
        d4 = client.get(
            f"/api/pipelines/{pid}/logs?start_date=2026-06-04&end_date=2026-06-04"
        ).json()
        assert [r["batch"] for r in d4["items"]] == [a] and d4["total"] == 1
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_page_size_normalized(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        for _ in range(8):
            _add_run(app, pid, {"0": {"ok": 1}, "1": {"ok": 1}, "2": {"ok": 1}}, status="done")
        # page_size=7 非法 → 归 30
        r = client.get(f"/api/pipelines/{pid}/logs?page_size=7").json()
        assert r["page_size"] == 30 and len(r["items"]) == 24
        r20 = client.get(f"/api/pipelines/{pid}/logs?page_size=20").json()
        assert r20["page_size"] == 20 and len(r20["items"]) == 20
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_page_out_of_range(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        _add_run(app, pid, {"0": {"ok": 1}}, status="done")
        r = client.get(f"/api/pipelines/{pid}/logs?page=999&page_size=20").json()
        assert r["items"] == [] and r["total"] == 1
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_bad_date_400(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        assert client.get(f"/api/pipelines/{pid}/logs?start_date=2026-13-99").status_code == 400
    finally:
        app.cleanup()
