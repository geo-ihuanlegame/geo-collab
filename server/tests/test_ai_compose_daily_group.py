import uuid

import pytest

from server.tests.utils import build_test_app


def _make_article(client, title="文章"):
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


def _uid(app):
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        return db.query(User).first().id


def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    db = session_factory()
    try:
        art = create_article(
            db,
            user_id,
            ArticleCreate(
                title=f"A-{uuid.uuid4().hex[:6]}",
                content_json={"type": "doc", "content": []},
                content_html="<p>x</p>",
                plain_text="x",
                word_count=1,
                client_request_id=str(uuid.uuid4()),
            ),
        )
        db.commit()
        return art.id
    finally:
        db.close()


def _make_tpl(app, uid, enabled=True):
    from server.app.modules.prompt_templates.models import PromptTemplate

    with app.session_factory() as db:
        t = PromptTemplate(
            name="模板",
            content="写: {{question}}",
            scope="generation",
            user_id=uid,
            is_enabled=enabled,
        )
        db.add(t)
        db.commit()
        return t.id


def _ctx(app, uid, config, inputs, upstream=None):
    from server.app.modules.pipelines.nodes.base import NodeRunContext

    return NodeRunContext(
        session_factory=app.session_factory,
        user_id=uid,
        config=config,
        inputs=inputs,
        upstream=upstream or {},
    )


def _patch_generate(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt", _fake_generate
    )


@pytest.mark.mysql
def test_compose_flat_streams_into_today_group(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem

    _patch_generate(monkeypatch)
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose

        uid = _uid(app)
        t = _make_tpl(app, uid)
        ctx = _ctx(
            app,
            uid,
            {"prompt_template_ids": [t], "count": 3, "ai_engine": None, "daily_group": True},
            {"question_text": "q"},
        )
        res = run_ai_compose(ctx)
        ids = res.output["article_ids"]
        assert len(ids) == 3 and res.output["errors"] == []
        gid = res.output["group_id"]
        assert gid is not None
        with app.session_factory() as db:
            g = db.get(ArticleGroup, gid)
            assert g.user_id == uid and g.name.startswith("每日生成 · ")
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert {it.article_id for it in items} == set(ids)
            for aid in ids:
                assert db.get(Article, aid).review_status == "pending"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_compose_off_emits_no_group_id_and_creates_no_group(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup

    _patch_generate(monkeypatch)
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose

        uid = _uid(app)
        t = _make_tpl(app, uid)
        ctx = _ctx(
            app,
            uid,
            {"prompt_template_ids": [t], "count": 2, "ai_engine": None},
            {"question_text": "q"},
        )
        res = run_ai_compose(ctx)
        assert len(res.output["article_ids"]) == 2
        assert res.output.get("group_id") is None
        with app.session_factory() as db:
            cnt = (
                db.query(ArticleGroup)
                .filter(
                    ArticleGroup.user_id == uid,
                    ArticleGroup.is_deleted == False,  # noqa: E712
                )
                .count()
            )
            assert cnt == 0
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_compose_units_partial_failure_keeps_succeeded_in_group(monkeypatch):
    from server.app.modules.articles.models import ArticleGroupItem

    _patch_generate(monkeypatch)
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose

        uid = _uid(app)
        t_ok = _make_tpl(app, uid)
        units = [
            {
                "question_type": "A",
                "question_text": "1. qa",
                "allowed_prompt_template_ids": [t_ok],
                "article_count": 1,
            },
            {
                "question_type": "B",
                "question_text": "1. qb",
                "allowed_prompt_template_ids": [],
                "article_count": 1,
            },
        ]
        # 本节点 prompt_template_ids 留空 → B 无模板可回退 → 失败；A 用自带 t_ok → 成功
        ctx = _ctx(
            app,
            uid,
            {"prompt_template_ids": [], "count": 1, "ai_engine": None, "daily_group": True},
            {"generation_units": units},
        )
        res = run_ai_compose(ctx)
        assert len(res.output["article_ids"]) == 1
        assert len(res.output["errors"]) == 1
        gid = res.output["group_id"]
        assert gid is not None
        with app.session_factory() as db:
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert {it.article_id for it in items} == set(res.output["article_ids"])
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_compose_degrades_when_resolve_fails(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup

    _patch_generate(monkeypatch)
    monkeypatch.setattr(
        "server.app.modules.articles.service.resolve_or_create_daily_group", lambda *a, **k: None
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose

        uid = _uid(app)
        t = _make_tpl(app, uid)
        ctx = _ctx(
            app,
            uid,
            {"prompt_template_ids": [t], "count": 2, "ai_engine": None, "daily_group": True},
            {"question_text": "q"},
        )
        res = run_ai_compose(ctx)
        assert len(res.output["article_ids"]) == 2
        assert res.output.get("group_id") is None
        with app.session_factory() as db:
            cnt = (
                db.query(ArticleGroup)
                .filter(
                    ArticleGroup.user_id == uid,
                    ArticleGroup.is_deleted == False,  # noqa: E712
                )
                .count()
            )
            assert cnt == 0
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_compose_skipped_no_question_creates_no_group(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup

    _patch_generate(monkeypatch)
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose

        uid = _uid(app)
        t = _make_tpl(app, uid)
        # 无 question_text → 安静跳过；即使 daily_group=True 也不应建空组
        ctx = _ctx(
            app,
            uid,
            {"prompt_template_ids": [t], "count": 2, "ai_engine": None, "daily_group": True},
            {},
        )
        res = run_ai_compose(ctx)
        assert res.output.get("skipped")
        assert res.output.get("group_id") is None
        with app.session_factory() as db:
            cnt = (
                db.query(ArticleGroup)
                .filter(
                    ArticleGroup.user_id == uid,
                    ArticleGroup.is_deleted == False,  # noqa: E712
                )
                .count()
            )
            assert cnt == 0
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_node_types_ai_compose_has_daily_group_toggle(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        r = app.client.get("/api/pipelines/node-types")
        assert r.status_code == 200, r.text
        types = {nt["type"]: nt for nt in r.json()["node_types"]}
        fields = {f["key"]: f for f in types["ai_compose"]["config_schema"]}
        assert "daily_group" in fields
        assert fields["daily_group"]["type"] == "toggle"
        assert fields["daily_group"].get("default") is False
    finally:
        app.cleanup()
