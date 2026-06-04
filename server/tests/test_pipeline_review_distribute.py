import pytest

from server.tests.utils import build_test_app


def _make_article(client, title="文章") -> int:
    resp = client.post(
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
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.mysql
def test_mark_pending_and_group_sets_pending_and_groups(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.articles.service import mark_pending_and_group

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        a1 = _make_article(client, "甲")
        a2 = _make_article(client, "乙")
        # 新建文章默认 approved；helper 应翻成 pending 并成组
        with test_app.session_factory() as db:
            uid = db.query(Article).first().user_id
        gid = mark_pending_and_group(
            test_app.session_factory, article_ids=[a1, a2], user_id=uid, base_name="测试组"
        )
        assert gid is not None
        with test_app.session_factory() as db:
            assert db.get(Article, a1).review_status == "pending"
            assert db.get(Article, a2).review_status == "pending"
            grp = db.get(ArticleGroup, gid)
            assert grp is not None and grp.name == "测试组"
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert {it.article_id for it in items} == {a1, a2}
    finally:
        test_app.cleanup()
