import pytest

from server.tests.utils import build_test_app


def _make_pool(app, items):
    """items: list[(category, text, source_active)]. record_id = 'r{i}'. 返回 (pool_id, uid)。"""
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
def test_multi_type(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(
            app, [("美食", "红烧肉", True), ("旅游", "去哪玩", True), ("科技", "AI", True)]
        )
        out = _run(app, uid, {"pool_id": pid, "question_types": ["美食", "旅游"]}).output
        assert out["question_count"] == 2
        assert "红烧肉" in out["question_text"] and "去哪玩" in out["question_text"]
        assert "AI" not in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_types_with_uncategorized(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(
            app, [("美食", "红烧肉", True), (None, "无分类题", True), ("科技", "AI", True)]
        )
        out = _run(
            app, uid, {"pool_id": pid, "question_types": ["美食", "__uncategorized__"]}
        ).output
        assert out["question_count"] == 2
        assert "红烧肉" in out["question_text"] and "无分类题" in out["question_text"]
        assert "AI" not in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_record_ids_override_types(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        # r0 美食, r1 旅游
        pid, uid = _make_pool(app, [("美食", "红烧肉", True), ("旅游", "去哪玩", True)])
        out = _run(
            app, uid, {"pool_id": pid, "question_types": ["美食"], "question_record_ids": ["r1"]}
        ).output
        assert out["question_count"] == 1
        assert "去哪玩" in out["question_text"]  # record_ids 优先、忽略类型
        assert "红烧肉" not in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_record_ids_lenient_to_stale(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        # r0 active, r1 inactive
        pid, uid = _make_pool(app, [("美食", "有效题", True), ("美食", "失效题", False)])
        out = _run(app, uid, {"pool_id": pid, "question_record_ids": ["r0", "r1", "不存在"]}).output
        assert out["question_count"] == 1  # 只取 active 且存在的
        assert "有效题" in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_legacy_question_type(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(app, [("美食", "红烧肉", True), ("旅游", "去哪玩", True)])
        out = _run(app, uid, {"pool_id": pid, "question_type": "美食"}).output  # 旧单选
        assert out["question_count"] == 1 and "红烧肉" in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_empty_means_all(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(
            app,
            [("美食", "a", True), ("旅游", "b", True), (None, "c", True), ("美食", "停用", False)],
        )
        out = _run(app, uid, {"pool_id": pid}).output  # 无 types 无 record_ids
        assert out["question_count"] == 3  # 全部 active
        assert "停用" not in out["question_text"]
    finally:
        app.cleanup()
