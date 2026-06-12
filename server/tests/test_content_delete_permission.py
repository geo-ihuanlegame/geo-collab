"""内容删除权限下放：文章 / 文章分组的删除从 admin-only 放开给 operator。

契约（与账号删除 admin-only 不同，见 test_delete_guards / test_accounts_api）：
  - operator 可删**自己创建**的文章 / 分组（软删除）。
  - operator 删**别人**的文章 / 分组 → 404（沿用 `_verify_*_ownership`，不泄露存在性）。
  - admin 越权删任意人的内容仍 → 204（admin 绕过归属校验）。
  - 图片删除本就只要登录（get_current_user），不在本契约范围，另见 image_library 用例。
"""

from server.app.modules.articles.models import Article, ArticleGroup
from server.tests.utils import build_test_app, create_extra_user


def _create_article(client, title: str = "标题") -> int:
    resp = client.post(
        "/api/articles",
        json={"title": title, "content_json": {"type": "doc", "content": []}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_group(client, name: str = "分组") -> int:
    resp = client.post("/api/article-groups", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# ── 文章 ────────────────────────────────────────────────────────────────────


def test_operator_can_delete_own_article(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _uid, op = create_extra_user(test_app, "op_art_own", role="operator")
        article_id = _create_article(op, "operator 自己的文章")

        resp = op.delete(f"/api/articles/{article_id}")
        assert resp.status_code == 204

        with test_app.session_factory() as db:
            art = db.get(Article, article_id)
            assert art is not None
            assert bool(art.is_deleted) is True
            assert art.deleted_at is not None
    finally:
        test_app.cleanup()


def test_operator_cannot_delete_others_article(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        # admin（默认 client）创建文章 → 归属 admin
        article_id = _create_article(test_app.client, "admin 的文章")
        _uid, op = create_extra_user(test_app, "op_art_other", role="operator")

        resp = op.delete(f"/api/articles/{article_id}")
        assert resp.status_code == 404  # 归属校验：不泄露存在性

        with test_app.session_factory() as db:
            art = db.get(Article, article_id)
            assert bool(art.is_deleted) is False  # 未被删
    finally:
        test_app.cleanup()


def test_admin_can_still_delete_any_article(monkeypatch):
    """回归：放开后 admin 仍可越权删 operator 的文章（绕过归属校验）。"""
    test_app = build_test_app(monkeypatch)
    try:
        _uid, op = create_extra_user(test_app, "op_art_admin", role="operator")
        article_id = _create_article(op, "operator 的文章")

        resp = test_app.client.delete(f"/api/articles/{article_id}")
        assert resp.status_code == 204

        with test_app.session_factory() as db:
            assert bool(db.get(Article, article_id).is_deleted) is True
    finally:
        test_app.cleanup()


# ── 文章分组 ─────────────────────────────────────────────────────────────────


def test_operator_can_delete_own_group(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _uid, op = create_extra_user(test_app, "op_grp_own", role="operator")
        group_id = _create_group(op, "operator 自己的分组")

        resp = op.delete(f"/api/article-groups/{group_id}")
        assert resp.status_code == 204

        with test_app.session_factory() as db:
            grp = db.get(ArticleGroup, group_id)
            assert grp is not None
            assert bool(grp.is_deleted) is True
            assert grp.deleted_at is not None
    finally:
        test_app.cleanup()


def test_operator_cannot_delete_others_group(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        group_id = _create_group(test_app.client, "admin 的分组")
        _uid, op = create_extra_user(test_app, "op_grp_other", role="operator")

        resp = op.delete(f"/api/article-groups/{group_id}")
        assert resp.status_code == 404

        with test_app.session_factory() as db:
            assert bool(db.get(ArticleGroup, group_id).is_deleted) is False
    finally:
        test_app.cleanup()
