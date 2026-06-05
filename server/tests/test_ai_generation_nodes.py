import pytest

from server.tests.utils import build_test_app


def _make_pool_with_items(app, items):
    """items: list[(category, question_text, source_active)]. 返回 pool_id + user_id。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool

    with app.session_factory() as db:
        from server.app.modules.system.models import User

        uid = db.query(User).first().id
        pool = QuestionPool(user_id=uid, name="测试池")
        db.add(pool)
        db.flush()
        for i, (cat, text, active) in enumerate(items):
            db.add(
                QuestionItem(
                    pool_id=pool.id,
                    record_id=f"r{i}",
                    fields={},
                    category=cat,
                    question_text=text,
                    source_active=active,
                )
            )
        db.commit()
        return pool.id, uid


@pytest.mark.mysql
def test_question_source_picks_type_and_active(monkeypatch):
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.question_source import run_question_source

    app = build_test_app(monkeypatch)
    try:
        pool_id, uid = _make_pool_with_items(
            app,
            [
                ("美食", "怎么做红烧肉", True),
                ("美食", "怎么做糖醋排骨", True),
                ("旅游", "去哪玩", True),
                ("美食", "停用的问题", False),
            ],
        )
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"pool_id": pool_id, "question_type": "美食"},
            inputs={},
            upstream={},
        )
        res = run_question_source(ctx)
        assert "红烧肉" in res.output["question_text"]
        assert "糖醋排骨" in res.output["question_text"]
        assert "去哪玩" not in res.output["question_text"]
        assert "停用" not in res.output["question_text"]
        assert res.output["question_count"] == 2
        # 无匹配类型 → 空 question_text，不报错
        ctx2 = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"pool_id": pool_id, "question_type": "不存在"},
            inputs={},
            upstream={},
        )
        assert run_question_source(ctx2).output["question_text"] == ""
    finally:
        app.cleanup()
