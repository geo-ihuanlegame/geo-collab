import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from server.app.modules.pipelines.schedule_calc import current_slot, in_window
from server.app.modules.pipelines.service import validate_agent_fields
from server.app.shared.errors import ValidationError
from server.tests.utils import build_test_app

TZ = ZoneInfo("Asia/Shanghai")


def test_validate_ok_minimal():
    validate_agent_fields(
        name="智能体",
        type="general",
        tags=[],
        schedule_kind="none",
        schedule_minute=None,
        schedule_hour=None,
        schedule_weekday=None,
        window_start=None,
        window_end=None,
    )


def test_validate_name_too_long():
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="x" * 51,
            type="general",
            tags=[],
            schedule_kind="none",
            schedule_minute=None,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=None,
            window_end=None,
        )


def test_validate_bad_type_and_tags():
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="a",
            type="weird",
            tags=[],
            schedule_kind="none",
            schedule_minute=None,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=None,
            window_end=None,
        )
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="a",
            type="general",
            tags=["1", "2", "3", "4", "5", "6"],
            schedule_kind="none",
            schedule_minute=None,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=None,
            window_end=None,
        )


def test_validate_schedule_consistency():
    # daily 缺 hour
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="a",
            type="general",
            tags=[],
            schedule_kind="daily",
            schedule_minute=30,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=None,
            window_end=None,
        )
    # weekly 全齐 OK
    validate_agent_fields(
        name="a",
        type="general",
        tags=[],
        schedule_kind="weekly",
        schedule_minute=30,
        schedule_hour=9,
        schedule_weekday=0,
        window_start=None,
        window_end=None,
    )


def test_validate_window_order():
    # overnight window (start > end) is now ACCEPTED
    validate_agent_fields(
        name="a",
        type="general",
        tags=[],
        schedule_kind="none",
        schedule_minute=None,
        schedule_hour=None,
        schedule_weekday=None,
        window_start=dt.time(22, 0),
        window_end=dt.time(6, 0),
    )
    # zero-length window (start == end) still rejected
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="a",
            type="general",
            tags=[],
            schedule_kind="none",
            schedule_minute=None,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=dt.time(10, 0),
            window_end=dt.time(10, 0),
        )
    # paired-presence enforced: only one of start/end set → raises
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="a",
            type="general",
            tags=[],
            schedule_kind="none",
            schedule_minute=None,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=dt.time(10, 0),
            window_end=None,
        )


def test_in_window_overnight():
    import datetime as dt
    from zoneinfo import ZoneInfo

    from server.app.modules.pipelines.schedule_calc import in_window

    tz = ZoneInfo("Asia/Shanghai")

    def L(h, mi):
        return dt.datetime(2026, 6, 5, h, mi, tzinfo=tz)

    ws, we = dt.time(22, 0), dt.time(6, 0)
    assert in_window(ws, we, L(23, 0)) is True
    assert in_window(ws, we, L(3, 0)) is True
    assert in_window(ws, we, L(12, 0)) is False


def test_current_slot_daily_hit_and_miss():
    now = dt.datetime(2026, 6, 5, 9, 30, tzinfo=TZ)
    slot = current_slot("daily", 30, 9, None, now)
    assert slot is not None and slot.hour == 9 and slot.minute == 30
    assert current_slot("daily", 30, 9, None, now.replace(minute=31)) is None
    assert current_slot("daily", 30, 9, None, now.replace(hour=10)) is None


def test_current_slot_hourly_and_weekly():
    now = dt.datetime(2026, 6, 5, 14, 15, tzinfo=TZ)  # 2026-06-05 是周五 → weekday()==4
    assert current_slot("hourly", 15, None, None, now) is not None
    assert current_slot("hourly", 16, None, None, now) is None
    assert current_slot("weekly", 15, 14, 4, now) is not None
    assert current_slot("weekly", 15, 14, 0, now) is None  # 周一


def test_current_slot_none():
    now = dt.datetime(2026, 6, 5, 9, 30, tzinfo=TZ)
    assert current_slot("none", None, None, None, now) is None


def test_in_window():
    now = dt.datetime(2026, 6, 5, 9, 30, tzinfo=TZ)
    assert in_window(None, None, now) is True
    assert in_window(dt.time(7, 0), dt.time(23, 0), now) is True
    assert in_window(dt.time(10, 0), dt.time(23, 0), now) is False


def _publish_simple_pipeline(client, name="定时体", schedule=None):
    body = {"name": name}
    if schedule:
        body.update(schedule)
    pid = client.post("/api/pipelines", json=body).json()["id"]
    snap = {
        "schemaVersion": 1,
        "nodes": [
            {
                "node_type": "input",
                "name": "源",
                "node_index": 0,
                "config": {"question_text": "x"},
                "flow_meta": None,
            }
        ],
    }
    client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
    client.post(f"/api/pipelines/{pid}/publish", json={})
    return pid


@pytest.mark.mysql
def test_run_due_triggers_once_and_claims(monkeypatch):
    triggered = []
    monkeypatch.setattr(
        "server.app.modules.pipelines.scheduler.run_pipeline",
        lambda run_id, sf: triggered.append(run_id),
    )
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        pid = _publish_simple_pipeline(
            client,
            schedule={
                "schedule_kind": "daily",
                "schedule_minute": 30,
                "schedule_hour": 9,
                "is_enabled": True,
            },
        )
        from server.app.modules.pipelines.scheduler import run_due_pipelines_once

        now = dt.datetime(2026, 6, 5, 9, 30, tzinfo=TZ)
        n1 = run_due_pipelines_once(test_app.session_factory, now=now)
        assert n1 == 1 and len(triggered) == 1
        # 同 slot 再跑：claim 幂等
        n2 = run_due_pipelines_once(test_app.session_factory, now=now)
        assert n2 == 0
        from server.app.modules.pipelines.models import Pipeline

        with test_app.session_factory() as db:
            assert db.get(Pipeline, pid).last_scheduled_run_at is not None
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_run_due_skips_disabled_window_and_no_nodes(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.scheduler.run_pipeline", lambda run_id, sf: None
    )
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        from server.app.modules.pipelines.scheduler import run_due_pipelines_once

        now = dt.datetime(2026, 6, 5, 9, 30, tzinfo=TZ)
        # disabled
        _publish_simple_pipeline(
            client,
            name="停用",
            schedule={
                "schedule_kind": "daily",
                "schedule_minute": 30,
                "schedule_hour": 9,
                "is_enabled": False,
            },
        )
        # 窗外
        _publish_simple_pipeline(
            client,
            name="窗外",
            schedule={
                "schedule_kind": "daily",
                "schedule_minute": 30,
                "schedule_hour": 9,
                "window_start": "10:00:00",
                "window_end": "23:00:00",
            },
        )
        # 无已发布节点（建但不发布）
        client.post(
            "/api/pipelines",
            json={
                "name": "无节点",
                "schedule_kind": "daily",
                "schedule_minute": 30,
                "schedule_hour": 9,
            },
        ).json()["id"]
        assert run_due_pipelines_once(test_app.session_factory, now=now) == 0
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_tags_dedup_on_create_and_patch(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        # create with duplicates and whitespace variants
        resp = client.post(
            "/api/pipelines",
            json={"name": "去重体", "tags": ["a", "a", "b", " b ", "c"]},
        )
        assert resp.status_code in (200, 201), resp.text
        pid = resp.json()["id"]
        got = client.get(f"/api/pipelines/{pid}").json()
        assert got["tags"] == ["a", "b", "c"]

        # patch with duplicates also deduped
        resp2 = client.patch(
            f"/api/pipelines/{pid}",
            json={"tags": ["x", "x", " y", "y"]},
        )
        assert resp2.status_code == 200, resp2.text
        got2 = client.get(f"/api/pipelines/{pid}").json()
        assert got2["tags"] == ["x", "y"]
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_ignore_exception_fail_fast_vs_continue(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline

        def _build(ignore: bool):
            pid = client.post(
                "/api/pipelines", json={"name": f"ie-{ignore}", "ignore_exception": ignore}
            ).json()["id"]
            snap = {
                "schemaVersion": 1,
                "nodes": [
                    {
                        "node_type": "ai_generate",
                        "name": "坏",
                        "node_index": 0,
                        "config": {"prompt_template_id": 999999, "count": 1, "question_text": "x"},
                        "flow_meta": None,
                    },
                    {
                        "node_type": "input",
                        "name": "后",
                        "node_index": 1,
                        "config": {"question_text": "y"},
                        "flow_meta": {"dependsOnIndex": 0},
                    },
                ],
            }
            client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
            client.post(f"/api/pipelines/{pid}/publish", json={})
            with test_app.session_factory() as db:
                p = db.get(Pipeline, pid)
                run = create_run(db, pipeline_id=pid, user_id=p.user_id)
                db.commit()
                rid = run.id
            run_pipeline(rid, test_app.session_factory)
            return client.get(f"/api/pipelines/runs/{rid}").json()

        # ignore_exception=False：上游(节点0)失败 → 依赖它的节点1 被阻断（带"上游"错误）
        r_off = _build(False)
        assert r_off["status"] == "failed"
        assert "上游" in r_off["node_results"].get("1", {}).get("error", "")
        # ignore_exception=True：不阻断依赖 → 节点1 仍执行（input 产出 question_text，无"上游"阻断）
        r_on = _build(True)
        assert r_on["node_results"]["1"].get("question_text") == "y"
        assert "上游" not in str(r_on["node_results"]["1"])
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# Pure-logic tests for last_due_slot (no DB needed)
# ---------------------------------------------------------------------------
from server.app.modules.pipelines.schedule_calc import last_due_slot  # noqa: E402

_TZ = ZoneInfo("Asia/Shanghai")


def _local(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=_TZ)


def test_last_due_slot_hourly_returns_recent_even_when_minute_passed():
    slot = last_due_slot("hourly", 30, None, None, _local(2026, 6, 5, 9, 47))
    assert (slot.hour, slot.minute) == (9, 30)


def test_last_due_slot_hourly_wraps_prev_hour():
    slot = last_due_slot("hourly", 30, None, None, _local(2026, 6, 5, 9, 10))
    assert (slot.hour, slot.minute) == (8, 30)


def test_last_due_slot_daily_before_time_wraps_prev_day():
    slot = last_due_slot("daily", 30, 9, None, _local(2026, 6, 5, 8, 0))
    assert (slot.day, slot.hour, slot.minute) == (4, 9, 30)


def test_last_due_slot_none_kind():
    assert last_due_slot("none", None, None, None, _local(2026, 6, 5, 9, 0)) is None


# ---------------------------------------------------------------------------
# DB tests: Task 6 (drift) + Task 7 (atomic claim)
# ---------------------------------------------------------------------------
from server.app.modules.system.models import User  # noqa: E402


@pytest.mark.mysql
def test_run_due_triggers_even_when_poll_minute_mismatch(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines import scheduler as sched
        from server.app.modules.pipelines import service as svc
        from server.app.modules.pipelines.models import PipelineNode

        with app.session_factory() as db:
            admin_id = db.query(User).filter_by(username="testadmin").first().id
            p = svc.create_pipeline(
                db,
                user_id=admin_id,
                name="hourly",
                description=None,
                schedule_kind="hourly",
                schedule_minute=30,
            )
            db.add(
                PipelineNode(
                    pipeline_id=p.id,
                    node_type="input",
                    name="in",
                    node_index=0,
                    config={"question_text": "x"},
                    flow_meta=None,
                )
            )
            db.commit()
        monkeypatch.setattr(sched, "run_pipeline", lambda *a, **k: None)
        now = _local(2026, 6, 5, 9, 47)  # schedule minute=30; poll at :47 — old impl would miss
        assert sched.run_due_pipelines_once(app.session_factory, now=now) == 1
        assert sched.run_due_pipelines_once(app.session_factory, now=now) == 0  # same slot deduped
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_claim_rolled_back_when_create_run_fails(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines import scheduler as sched
        from server.app.modules.pipelines import service as svc
        from server.app.modules.pipelines.models import Pipeline, PipelineNode

        with app.session_factory() as db:
            admin_id = db.query(User).filter_by(username="testadmin").first().id
            p = svc.create_pipeline(
                db,
                user_id=admin_id,
                name="hourly",
                description=None,
                schedule_kind="hourly",
                schedule_minute=30,
            )
            db.add(
                PipelineNode(
                    pipeline_id=p.id,
                    node_type="input",
                    name="in",
                    node_index=0,
                    config={},
                    flow_meta=None,
                )
            )
            db.commit()
            pid = p.id

        def _boom(*a, **k):
            raise RuntimeError("simulated create_run failure")

        monkeypatch.setattr(sched, "create_run", _boom)
        now = _local(2026, 6, 5, 9, 47)
        assert sched.run_due_pipelines_once(app.session_factory, now=now) == 0
        with app.session_factory() as db:
            assert db.get(Pipeline, pid).last_scheduled_run_at is None  # claim rolled back
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_patch_can_clear_window(monkeypatch):
    """PATCH with None for window_start/window_end should clear those nullable fields."""
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines import service as svc

        with app.session_factory() as db:
            admin_id = db.query(User).filter_by(username="testadmin").first().id
            p = svc.create_pipeline(
                db,
                user_id=admin_id,
                name="window-test",
                description=None,
                window_start=dt.time(9, 0),
                window_end=dt.time(18, 0),
            )
            db.commit()
            pid = p.id

        with app.session_factory() as db:
            p = db.get(type(p), pid)
            assert p.window_start == dt.time(9, 0)
            assert p.window_end == dt.time(18, 0)

            svc.patch_pipeline(db, p, fields={"window_start": None, "window_end": None})
            db.commit()

        with app.session_factory() as db:
            p = db.get(type(p), pid)
            assert p.window_start is None
            assert p.window_end is None
    finally:
        app.cleanup()
