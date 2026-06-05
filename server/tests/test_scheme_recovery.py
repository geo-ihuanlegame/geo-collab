"""Task 13：scheme run 启动恢复——与 pipeline run 对称，复位崩溃残留的 running/pending。"""

import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_recover_stuck_scheme_runs_resets_running_and_pending(monkeypatch):
    from server.app.modules.ai_generation.models import (
        GenerationScheme,
        GenerationSchemeRun,
        QuestionPool,
    )
    from server.app.modules.ai_generation.scheme_executor import recover_stuck_scheme_runs
    from server.app.modules.system.models import User

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            uid = db.query(User).first().id
            pool = QuestionPool(user_id=uid, name="池")
            db.add(pool)
            db.flush()
            scheme = GenerationScheme(user_id=uid, pool_id=pool.id, name="方案")
            db.add(scheme)
            db.flush()
            db.add(
                GenerationSchemeRun(
                    scheme_id=scheme.id, user_id=uid, status="running", article_ids=[]
                )
            )
            db.add(
                GenerationSchemeRun(
                    scheme_id=scheme.id, user_id=uid, status="pending", article_ids=[]
                )
            )
            db.add(
                GenerationSchemeRun(scheme_id=scheme.id, user_id=uid, status="done", article_ids=[])
            )
            db.commit()

        with test_app.session_factory() as db:
            recover_stuck_scheme_runs(db)

        with test_app.session_factory() as db:
            rows = db.query(GenerationSchemeRun).order_by(GenerationSchemeRun.id.asc()).all()
            assert rows[0].status == "failed" and rows[0].error_message
            assert rows[1].status == "failed" and rows[1].error_message
            assert rows[2].status == "done"  # 已终态的不动
    finally:
        test_app.cleanup()
