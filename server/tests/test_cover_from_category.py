"""image_library.cover.set_random_cover_from_category 的行为单测。

封面来源是 MinIO 上的 StockImage，测试里 monkeypatch get_object_bytes 返回固定 PNG 字节，
不依赖真实 MinIO（沿用 web_fallback 测试的打桩思路）。需要 DB（@pytest.mark.mysql）。
"""

import base64

import pytest

from server.tests.utils import build_test_app

# 1x1 透明 PNG，供 store._create_asset 正常算尺寸/派生
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _make_category(app, name, bucket, kind="main"):
    from server.app.modules.image_library.models import StockCategory

    with app.session_factory() as db:
        cat = StockCategory(name=name, bucket_name=bucket, kind=kind)
        db.add(cat)
        db.commit()
        db.refresh(cat)
        return cat.id


def _make_stock_image(app, category_id, minio_key="img1.png", filename="img1.png"):
    from server.app.modules.image_library.models import StockImage

    with app.session_factory() as db:
        img = StockImage(category_id=category_id, minio_key=minio_key, filename=filename)
        db.add(img)
        db.commit()
        db.refresh(img)
        return img.id


def _make_article(client):
    r = client.post(
        "/api/articles",
        json={
            "title": "封面测试",
            "content_json": {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "正文"}]}],
            },
            "content_html": "<p>正文</p>",
            "plain_text": "正文",
            "word_count": 2,
            "status": "draft",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _uid(app):
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        return db.query(User).first().id


@pytest.mark.mysql
def test_cover_set_when_article_has_no_cover(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        cat = _make_category(app, "主推A", "main-a")
        _make_stock_image(app, cat)
        aid = _make_article(app.client)
        uid = _uid(app)
        monkeypatch.setattr(
            "server.app.modules.image_library.store.get_object_bytes",
            lambda bucket, key: _PNG_1x1,
        )

        from server.app.modules.articles.models import Article
        from server.app.modules.image_library.cover import set_random_cover_from_category

        with app.session_factory() as db:
            article = db.get(Article, aid)
            result = set_random_cover_from_category(db, article, cat, uid)
            db.commit()
            assert result.status == "set"
            assert article.cover_asset_id is not None
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_cover_skipped_when_already_has_cover(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        cat = _make_category(app, "主推A", "main-a")
        _make_stock_image(app, cat)
        aid = _make_article(app.client)
        uid = _uid(app)
        monkeypatch.setattr(
            "server.app.modules.image_library.store.get_object_bytes",
            lambda bucket, key: _PNG_1x1,
        )

        from server.app.modules.articles.models import Article
        from server.app.modules.image_library.cover import set_random_cover_from_category

        with app.session_factory() as db:
            article = db.get(Article, aid)
            set_random_cover_from_category(db, article, cat, uid)
            db.commit()
            first_cover = article.cover_asset_id
        assert first_cover is not None

        with app.session_factory() as db:
            article = db.get(Article, aid)
            result = set_random_cover_from_category(db, article, cat, uid)
            db.commit()
            assert result.status == "skipped_existing"
            assert article.cover_asset_id == first_cover
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_cover_no_image_when_category_empty(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        cat = _make_category(app, "主推A", "main-a")  # 故意不建 StockImage
        aid = _make_article(app.client)
        uid = _uid(app)

        def _must_not_fetch(bucket, key):
            raise AssertionError("空栏目不应触发 MinIO 取图")

        monkeypatch.setattr(
            "server.app.modules.image_library.store.get_object_bytes", _must_not_fetch
        )

        from server.app.modules.articles.models import Article
        from server.app.modules.image_library.cover import set_random_cover_from_category

        with app.session_factory() as db:
            article = db.get(Article, aid)
            result = set_random_cover_from_category(db, article, cat, uid)
            db.commit()
            assert result.status == "no_image"
            assert article.cover_asset_id is None
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_cover_error_on_fetch_failure(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        cat = _make_category(app, "主推A", "main-a")
        _make_stock_image(app, cat)
        aid = _make_article(app.client)
        uid = _uid(app)

        def _boom(bucket, key):
            raise RuntimeError("minio down")

        monkeypatch.setattr("server.app.modules.image_library.store.get_object_bytes", _boom)

        from server.app.modules.articles.models import Article
        from server.app.modules.image_library.cover import set_random_cover_from_category

        with app.session_factory() as db:
            article = db.get(Article, aid)
            result = set_random_cover_from_category(db, article, cat, uid)
            db.commit()
            assert result.status == "error"
            assert "minio down" in (result.error or "")
            assert article.cover_asset_id is None
    finally:
        app.cleanup()
