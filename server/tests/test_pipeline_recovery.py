import pytest

from server.tests.utils import build_test_app


def test_recovery_skipped_when_flag_off(monkeypatch):
    monkeypatch.setenv("GEO_RUN_STARTUP_RECOVERY", "false")
    from server.app.core.config import get_settings

    get_settings.cache_clear()
    assert get_settings().run_startup_recovery is False
    get_settings.cache_clear()


@pytest.mark.mysql
def test_recover_stuck_pipeline_runs_resets_running_and_pending(monkeypatch):
    from server.app.modules.pipelines.models import Pipeline, PipelineRun
    from server.app.modules.pipelines.recovery import recover_stuck_pipeline_runs

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            uid = __import__("server.app.modules.system.models", fromlist=["User"]).User
            user_id = db.query(uid).first().id
            p = Pipeline(user_id=user_id, name="p", has_draft=False)
            db.add(p)
            db.flush()
            db.add(
                PipelineRun(
                    pipeline_id=p.id,
                    user_id=user_id,
                    status="running",
                    node_results={},
                    article_ids=[],
                )
            )
            db.add(
                PipelineRun(
                    pipeline_id=p.id,
                    user_id=user_id,
                    status="pending",
                    node_results={},
                    article_ids=[],
                )
            )
            db.add(
                PipelineRun(
                    pipeline_id=p.id,
                    user_id=user_id,
                    status="done",
                    node_results={},
                    article_ids=[],
                )
            )
            db.commit()

        with test_app.session_factory() as db:
            recover_stuck_pipeline_runs(db)

        with test_app.session_factory() as db:
            rows = db.query(PipelineRun).order_by(PipelineRun.id.asc()).all()
            assert rows[0].status == "failed" and rows[0].error_message
            assert rows[1].status == "failed" and rows[1].error_message
            assert rows[2].status == "done"  # 已终态的不动
    finally:
        test_app.cleanup()
