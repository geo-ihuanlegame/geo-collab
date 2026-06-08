import pytest

from server.tests.utils import build_test_app


def _make_approved_article(client, title="文章"):
    r = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": {"type": "doc", "content": []},
            "content_html": "<p>x</p>",
            "plain_text": "x",
            "word_count": 1,
            "status": "ready",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _make_group(app, uid, name, article_ids):
    """直接建分组 + 组项（顺序即 sort_order）。返回 group_id。"""
    from server.app.modules.articles.models import ArticleGroup, ArticleGroupItem

    with app.session_factory() as db:
        g = ArticleGroup(user_id=uid, name=name)
        db.add(g)
        db.flush()
        for i, aid in enumerate(article_ids):
            db.add(ArticleGroupItem(group_id=g.id, article_id=aid, sort_order=i))
        db.commit()
        return g.id


def _set_pending(app, article_id):
    from server.app.modules.articles.models import Article

    with app.session_factory() as db:
        db.get(Article, article_id).review_status = "pending"
        db.commit()


def _make_account(app, client, key="acc1", name="账号1"):
    """镜像 test_auto_distribute.py 的账号夹具：写 storage_state + /api/accounts/toutiao/login。"""
    import json as _json
    from pathlib import Path

    state_dir = Path(app.data_dir) / "browser_states" / "toutiao" / key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text(
        _json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
    )
    r = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": name, "account_key": key, "use_browser": False},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mark_distributed(app, client, article_ids, key="dk"):
    """用 create_task(article_round_robin) 给文章产出 PublishRecord，标记已分发。"""
    from server.app.modules.articles.models import Article
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    acc = _make_account(app, client, key=key, name=key)
    with app.session_factory() as db:
        uid = db.get(Article, article_ids[0]).user_id
        create_task(
            db,
            uid,
            TaskCreate(
                name="标记已分发",
                task_type="article_round_robin",
                article_ids=list(article_ids),
                accounts=[TaskAccountInput(account_id=acc, sort_order=0)],
            ),
            role="admin",
        )
        db.commit()


def _uid(app):
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        return db.query(User).first().id


def _run_node(app, uid, config):
    from server.app.modules.pipelines.nodes.article_group_source import run_article_group_source
    from server.app.modules.pipelines.nodes.base import NodeRunContext

    return run_article_group_source(
        NodeRunContext(
            session_factory=app.session_factory, user_id=uid, config=config, inputs={}, upstream={}
        )
    )


@pytest.mark.mysql
def test_auto_picks_oldest_group_with_candidates(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        uid = _uid(app)
        a1, a2 = _make_approved_article(client, "甲"), _make_approved_article(client, "乙")
        b1 = _make_approved_article(client, "丙")
        g1 = _make_group(app, uid, "早组", [a1, a2])
        _make_group(app, uid, "晚组", [b1])
        res = _run_node(app, uid, {})  # 自动模式
        assert res.output["group_id"] == g1
        assert res.output["article_ids"] == [a1, a2]  # 按 sort_order
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_only_approved_undistributed_subset(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        uid = _uid(app)
        a_ok = _make_approved_article(client, "已审未发")
        a_sent = _make_approved_article(client, "已审已发")
        a_pending = _make_approved_article(client, "未审")
        g = _make_group(app, uid, "混合组", [a_ok, a_sent, a_pending])
        _set_pending(app, a_pending)
        _mark_distributed(app, client, [a_sent])
        res = _run_node(app, uid, {"group_id": g})  # 手动选该组
        assert res.output["group_id"] == g
        assert res.output["article_ids"] == [a_ok]  # 只剩已审+未发
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_auto_skips_group_without_candidates(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        uid = _uid(app)
        a1 = _make_approved_article(client, "早-已发")
        b1 = _make_approved_article(client, "晚-未发")
        _make_group(app, uid, "早组", [a1])
        g2 = _make_group(app, uid, "晚组", [b1])
        _mark_distributed(app, client, [a1])  # 早组全部已分发 → 无候选
        res = _run_node(app, uid, {})
        assert res.output["group_id"] == g2  # 跳过 g1，选 g2
        assert res.output["article_ids"] == [b1]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_auto_empty_when_no_candidate_group(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        uid = _uid(app)
        a1 = _make_approved_article(client, "已发")
        _make_group(app, uid, "组", [a1])
        _mark_distributed(app, client, [a1])
        res = _run_node(app, uid, {})
        assert res.output["group_id"] is None
        assert res.output["article_ids"] == []
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_manual_invalid_group_raises(monkeypatch):
    from server.app.shared.errors import ValidationError

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        with pytest.raises(ValidationError):
            _run_node(app, uid, {"group_id": 999999})
    finally:
        app.cleanup()
