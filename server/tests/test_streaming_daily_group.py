import uuid

import pytest

from server.tests.utils import build_test_app


# ---- 共享 helper（本文件后续任务的测试都复用） ----
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
    """与 generate_article_from_prompt 同签名：建一篇文章、返回 id。"""
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


# ---- Task 1: service helper ----
@pytest.mark.mysql
def test_resolve_new_then_reuse_with_next_sort(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup, ArticleGroupItem
    from server.app.modules.articles.service import resolve_or_create_daily_group

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        NAME = "每日生成 · 2026-06-15"
        # 首建 → (gid, 0)
        res1 = resolve_or_create_daily_group(app.session_factory, user_id=uid, group_name=NAME)
        assert res1 is not None
        gid, start = res1
        assert start == 0
        # 塞两个 item（sort_order 0,1），再 resolve → 复用同组、next_start=2
        with app.session_factory() as db:
            db.add(
                ArticleGroupItem(group_id=gid, article_id=_make_article(app.client), sort_order=0)
            )
            db.add(
                ArticleGroupItem(group_id=gid, article_id=_make_article(app.client), sort_order=1)
            )
            db.commit()
        res2 = resolve_or_create_daily_group(app.session_factory, user_id=uid, group_name=NAME)
        assert res2 == (gid, 2)
        with app.session_factory() as db:
            cnt = (
                db.query(ArticleGroup)
                .filter(
                    ArticleGroup.user_id == uid,
                    ArticleGroup.is_deleted == False,  # noqa: E712
                )
                .count()
            )
            assert cnt == 1  # 复用、没新建
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_resolve_revives_soft_deleted(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup
    from server.app.modules.articles.service import resolve_or_create_daily_group

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        NAME = "每日生成 · 2026-06-15"
        with app.session_factory() as db:
            g = ArticleGroup(user_id=uid, name=NAME, is_deleted=True)
            db.add(g)
            db.commit()
            old_gid = g.id
        res = resolve_or_create_daily_group(app.session_factory, user_id=uid, group_name=NAME)
        assert res is not None
        gid, start = res
        assert gid == old_gid and start == 0  # 复活同一行、空成员
        with app.session_factory() as db:
            assert db.get(ArticleGroup, gid).is_deleted is False
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_append_marks_pending_inserts_item_and_leaves_group_row_untouched(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.articles.service import (
        append_article_to_group_pending,
        resolve_or_create_daily_group,
    )

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        gid, _ = resolve_or_create_daily_group(
            app.session_factory, user_id=uid, group_name="每日生成 · 2026-06-15"
        )
        with app.session_factory() as db:
            g = db.get(ArticleGroup, gid)
            ver0, upd0 = g.version, g.updated_at
        aid = _make_article(app.client)

        ok = append_article_to_group_pending(
            app.session_factory, group_id=gid, article_id=aid, sort_order=5
        )
        assert ok is True
        with app.session_factory() as db:
            assert db.get(Article, aid).review_status == "pending"  # 标待审
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert len(items) == 1 and items[0].article_id == aid and items[0].sort_order == 5
            g = db.get(ArticleGroup, gid)
            assert g.version == ver0 and g.updated_at == upd0  # 组行未被动（防死锁的关键）
        # 重复追加同一篇 → 去重、不报错
        ok2 = append_article_to_group_pending(
            app.session_factory, group_id=gid, article_id=aid, sort_order=9
        )
        assert ok2 is True
        with app.session_factory() as db:
            cnt = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).count()
            assert cnt == 1
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_resolve_reuses_after_concurrent_create(monkeypatch):
    """resolve 内首次 flush 前另一会话抢先建好同名组 → 撞唯一约束后回查复用（覆盖 IntegrityError/
    OperationalError 同一 except 分支）。"""
    from server.app.modules.articles import service as svc
    from server.app.modules.articles.models import ArticleGroup

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        NAME = "每日生成 · 2026-06-15"
        real_factory = app.session_factory
        state = {"injected": False, "concurrent_gid": None}

        class _HookSession:
            def __init__(self, inner):
                object.__setattr__(self, "_inner", inner)

            def __getattr__(self, name):
                return getattr(object.__getattribute__(self, "_inner"), name)

            def flush(self, *args, **kwargs):
                inner = object.__getattribute__(self, "_inner")
                if not state["injected"]:
                    state["injected"] = True
                    with real_factory() as other:  # 模拟并发：另一会话抢先建组
                        g = ArticleGroup(user_id=uid, name=NAME)
                        other.add(g)
                        other.commit()
                        state["concurrent_gid"] = g.id
                return inner.flush(*args, **kwargs)  # 本会话重复插入在此自然撞 IntegrityError

        def hook_factory():
            return _HookSession(real_factory())

        res = svc.resolve_or_create_daily_group(hook_factory, user_id=uid, group_name=NAME)
        assert res is not None
        gid, start = res
        assert gid == state["concurrent_gid"] and start == 0  # 回查复用了并发建的组
    finally:
        app.cleanup()
