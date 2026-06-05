import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_version_no_unique_and_cascade_delete(monkeypatch):
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    from server.app.modules.pipelines.models import (
        Pipeline,
        PipelineNode,
        PipelineRun,
        PipelineVersion,
    )
    from server.app.modules.system.models import User

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            uid = db.query(User).first().id
            p = Pipeline(user_id=uid, name="p", has_draft=False)
            db.add(p)
            db.flush()
            db.add(PipelineVersion(pipeline_id=p.id, version_no=1, snapshot={}, created_by=uid))
            db.commit()
            pid = p.id

        # (a) 同 (pipeline_id, version_no) 第二条应撞唯一约束
        with test_app.session_factory() as db:
            db.add(PipelineVersion(pipeline_id=pid, version_no=1, snapshot={}, created_by=uid))
            with _pytest.raises(IntegrityError):
                db.commit()

        # (b) 删 pipeline 应级联删子表（DB 层 CASCADE，不靠应用手删）
        with test_app.session_factory() as db:
            uid2 = db.query(User).first().id
            db.add(
                PipelineNode(pipeline_id=pid, node_type="input", name="n", node_index=0, config={})
            )
            db.add(
                PipelineRun(
                    pipeline_id=pid,
                    user_id=uid2,
                    status="done",
                    node_results={},
                    article_ids=[],
                )
            )
            db.commit()
            db.execute(
                __import__("sqlalchemy").text("DELETE FROM pipelines WHERE id = :i"),
                {"i": pid},
            )
            db.commit()
            assert db.query(PipelineNode).filter_by(pipeline_id=pid).count() == 0
            assert db.query(PipelineRun).filter_by(pipeline_id=pid).count() == 0
            assert db.query(PipelineVersion).filter_by(pipeline_id=pid).count() == 0
    finally:
        test_app.cleanup()
