import pytest

from server.tests.utils import build_test_app


def _make_pool(app, items):
    """items: list[(category, text, source_active)]. record_id='r{i}'. 返回 (pool_id, uid)。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        uid = db.query(User).first().id
        pool = QuestionPool(user_id=uid, name="池")
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


def _run(app, uid, config):
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.question_source import run_question_source

    return run_question_source(
        NodeRunContext(
            session_factory=app.session_factory, user_id=uid, config=config, inputs={}, upstream={}
        )
    )


@pytest.mark.mysql
def test_units_emit_per_type_with_tpl_and_count(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        # r0,r1 美食; r2 旅游
        pid, uid = _make_pool(
            app, [("美食", "红烧肉", True), ("美食", "糖醋", True), ("旅游", "去哪玩", True)]
        )
        cfg = {
            "pool_id": pid,
            "units": [
                {
                    "question_type": "美食",
                    "record_ids": ["r0", "r1"],
                    "allowed_prompt_template_ids": [7],
                    "article_count": 2,
                },
                {
                    "question_type": "旅游",
                    "record_ids": None,
                    "allowed_prompt_template_ids": [],
                    "article_count": None,
                },
            ],
        }
        out = _run(app, uid, cfg).output
        gus = out["generation_units"]
        assert len(gus) == 2
        um = {g["question_type"]: g for g in gus}
        assert "红烧肉" in um["美食"]["question_text"] and "糖醋" in um["美食"]["question_text"]
        assert um["美食"]["allowed_prompt_template_ids"] == [7]
        assert um["美食"]["article_count"] == 2
        assert "去哪玩" in um["旅游"]["question_text"]  # record_ids=None → 整类
        assert um["旅游"]["allowed_prompt_template_ids"] == []
        assert um["旅游"]["article_count"] is None
        # 扁平字段保留
        assert out["question_count"] == 3
        assert "红烧肉" in out["question_text"] and "去哪玩" in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_unit_without_questions_is_dropped(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(app, [("美食", "红烧肉", True)])
        cfg = {
            "pool_id": pid,
            "units": [
                {
                    "question_type": "美食",
                    "record_ids": ["r0"],
                    "allowed_prompt_template_ids": [1],
                    "article_count": 1,
                },
                {
                    "question_type": "旅游",
                    "record_ids": [],
                    "allowed_prompt_template_ids": [2],
                    "article_count": 5,
                },
            ],
        }
        out = _run(app, uid, cfg).output
        types = [g["question_type"] for g in out["generation_units"]]
        assert types == ["美食"]  # 旅游无问题 → 弃用
        assert out["question_count"] == 1
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_legacy_config_maps_to_units_by_category(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(
            app, [("美食", "红烧肉", True), ("旅游", "去哪玩", True), ("科技", "AI", True)]
        )
        out = _run(app, uid, {"pool_id": pid, "question_types": ["美食", "旅游"]}).output
        gus = {g["question_type"]: g for g in out["generation_units"]}
        assert set(gus) == {"美食", "旅游"}
        assert all(
            g["allowed_prompt_template_ids"] == [] and g["article_count"] is None
            for g in gus.values()
        )
        assert out["question_count"] == 2
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_uncategorized_whole_type(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(app, [("美食", "红烧肉", True), (None, "无分类题", True)])
        cfg = {
            "pool_id": pid,
            "units": [
                {
                    "question_type": "__uncategorized__",
                    "record_ids": None,
                    "allowed_prompt_template_ids": [],
                    "article_count": None,
                },
            ],
        }
        out = _run(app, uid, cfg).output
        gus = out["generation_units"]
        assert len(gus) == 1
        assert gus[0]["question_type"] == "__uncategorized__"
        assert "无分类题" in gus[0]["question_text"]
        assert "红烧肉" not in gus[0]["question_text"]
    finally:
        app.cleanup()
