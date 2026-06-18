"""图片库栏目硬删 + 删除预览测试。

§测试 清单：
- 删非空栏目：204、桶被清空+删除、图片记录级联删、栏目删除
- FK 清理：引用本栏目的 articles.stock_category_id 置 NULL（文章不被删）
- M2M 清理：article_stock_categories join 行随栏目级联删，文章存活
- MinIO best-effort：empty_bucket 抛错仍删 DB 记录
- 删不存在栏目 → 404
- delete-preview：有/无引用计数正确、软删文章不计、prefix 不误中、404
"""

import pytest

from server.app.modules.articles.models import Article
from server.app.modules.image_library.models import StockCategory, StockImage
from server.app.modules.system.models import User
from server.tests.utils import build_test_app


def _patch_minio(monkeypatch, calls=None):
    """无 MinIO：建桶/上传 no-op；清桶/删桶记录调用到 calls（若传）。"""
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.ensure_bucket",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.upload_image",
        lambda *a, **k: None,
    )

    def _empty(bucket):
        if calls is not None:
            calls.setdefault("empty", []).append(bucket)

    def _remove(bucket):
        if calls is not None:
            calls.setdefault("remove", []).append(bucket)

    monkeypatch.setattr("server.app.modules.image_library.router.minio_store.empty_bucket", _empty)
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.remove_bucket", _remove
    )


def _insert_category(db, name, bucket, kind="companion"):
    cat = StockCategory(name=name, bucket_name=bucket, kind=kind)
    db.add(cat)
    db.flush()
    return cat


def _insert_image(db, category_id, filename):
    img = StockImage(
        category_id=category_id, minio_key=f"key-{filename}", filename=filename, tags=[]
    )
    db.add(img)
    db.flush()
    return img


def _user_id(db):
    return db.query(User).first().id


@pytest.mark.mysql
def test_delete_non_empty_category(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        calls = {}
        _patch_minio(monkeypatch, calls)
        with app.session_factory() as db:
            cat = _insert_category(db, "待删栏目", "del-bucket", "companion")
            _insert_image(db, cat.id, "a.jpg")
            _insert_image(db, cat.id, "b.jpg")
            db.commit()
            cat_id = cat.id

        r = app.client.delete(f"/api/image-library/categories/{cat_id}")
        assert r.status_code == 204, r.text

        with app.session_factory() as db:
            assert db.get(StockCategory, cat_id) is None
            assert db.query(StockImage).filter(StockImage.category_id == cat_id).count() == 0
        # MinIO 清桶 + 删桶都被调用
        assert calls.get("empty") == ["del-bucket"]
        assert calls.get("remove") == ["del-bucket"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_category_nulls_article_fk(monkeypatch):
    """引用本栏目的 articles.stock_category_id 被置 NULL，文章本身不删。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            uid = _user_id(db)
            cat = _insert_category(db, "FK栏目", "fk-bucket", "main")
            db.flush()
            art = Article(user_id=uid, title="引用了主推栏目", stock_category_id=cat.id)
            db.add(art)
            db.commit()
            cat_id = cat.id
            art_id = art.id

        r = app.client.delete(f"/api/image-library/categories/{cat_id}")
        assert r.status_code == 204, r.text

        with app.session_factory() as db:
            refreshed = db.get(Article, art_id)
            assert refreshed is not None  # 文章没被删
            assert refreshed.stock_category_id is None  # FK 被置空
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_category_clears_m2m(monkeypatch):
    """article_stock_categories join 行随栏目级联删（ON DELETE CASCADE），文章存活。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            uid = _user_id(db)
            cat = _insert_category(db, "M2M栏目", "m2m-bucket", "companion")
            db.flush()
            art = Article(user_id=uid, title="多对多关联")
            art.stock_categories.append(cat)
            db.add(art)
            db.commit()
            cat_id = cat.id
            art_id = art.id

        r = app.client.delete(f"/api/image-library/categories/{cat_id}")
        assert r.status_code == 204, r.text

        with app.session_factory() as db:
            refreshed = db.get(Article, art_id)
            assert refreshed is not None
            assert all(c.id != cat_id for c in refreshed.stock_categories)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_category_minio_error_still_deletes_db(monkeypatch):
    """empty_bucket 抛错时仍删 DB 记录（best-effort 不阻断）。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)

        def _boom(bucket):
            raise RuntimeError("minio down")

        monkeypatch.setattr(
            "server.app.modules.image_library.router.minio_store.empty_bucket", _boom
        )
        with app.session_factory() as db:
            cat = _insert_category(db, "MinIO炸栏目", "boom-bucket", "companion")
            _insert_image(db, cat.id, "x.jpg")
            db.commit()
            cat_id = cat.id

        r = app.client.delete(f"/api/image-library/categories/{cat_id}")
        assert r.status_code == 204, r.text
        with app.session_factory() as db:
            assert db.get(StockCategory, cat_id) is None
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_category_remove_bucket_error_still_deletes_db(monkeypatch):
    """remove_bucket 抛错时仍删 DB 记录（best-effort 不阻断）。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)

        def _boom(bucket):
            raise RuntimeError("minio remove_bucket down")

        monkeypatch.setattr(
            "server.app.modules.image_library.router.minio_store.remove_bucket", _boom
        )
        with app.session_factory() as db:
            cat = _insert_category(db, "MinIO删桶炸栏目", "remove-boom-bucket", "companion")
            _insert_image(db, cat.id, "y.jpg")
            db.commit()
            cat_id = cat.id

        r = app.client.delete(f"/api/image-library/categories/{cat_id}")
        assert r.status_code == 204, r.text
        with app.session_factory() as db:
            assert db.get(StockCategory, cat_id) is None
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_nonexistent_category_404(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        r = app.client.delete("/api/image-library/categories/999999")
        assert r.status_code == 404, r.text
    finally:
        app.cleanup()


def _insert_article_html(db, uid, title, html, *, is_deleted=False):
    art = Article(user_id=uid, title=title, content_html=html, is_deleted=is_deleted)
    db.add(art)
    db.flush()
    return art


@pytest.mark.mysql
def test_delete_preview_counts_references(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            uid = _user_id(db)
            cat = _insert_category(db, "预览栏目", "preview-bucket", "companion")
            other = _insert_category(db, "别的栏目", "other-bucket", "companion")
            img1 = _insert_image(db, cat.id, "p1.jpg")
            img2 = _insert_image(db, cat.id, "p2.jpg")
            other_img = _insert_image(db, other.id, "o1.jpg")
            db.flush()
            # 引用本栏目 img1 —— 计入
            _insert_article_html(
                db,
                uid,
                "用了图1",
                f'<p><img src="/api/stock-images/{img1.id}/file"></p>',
            )
            # 引用本栏目 img2 —— 计入（另一篇）
            _insert_article_html(
                db,
                uid,
                "用了图2",
                f'<img src="/api/stock-images/{img2.id}/file">',
            )
            # 引用别的栏目的图 —— 不计入
            _insert_article_html(
                db,
                uid,
                "用了别栏目",
                f'<img src="/api/stock-images/{other_img.id}/file">',
            )
            # 软删文章引用 img1 —— 不计入
            _insert_article_html(
                db,
                uid,
                "软删的",
                f'<img src="/api/stock-images/{img1.id}/file">',
                is_deleted=True,
            )
            # prefix 不误中：引用 "{img1.id}9"（不存在的 id），不应误判为 img1
            _insert_article_html(
                db,
                uid,
                "prefix干扰",
                f'<img src="/api/stock-images/{img1.id}9/file">',
            )
            db.commit()
            cat_id = cat.id

        r = app.client.get(f"/api/image-library/categories/{cat_id}/delete-preview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["image_count"] == 2
        assert body["referenced_article_count"] == 2
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_preview_zero_references(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "无引用栏目", "noref-bucket", "companion")
            _insert_image(db, cat.id, "n1.jpg")
            db.commit()
            cat_id = cat.id

        r = app.client.get(f"/api/image-library/categories/{cat_id}/delete-preview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["image_count"] == 1
        assert body["referenced_article_count"] == 0
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_preview_404(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        r = app.client.get("/api/image-library/categories/999999/delete-preview")
        assert r.status_code == 404, r.text
    finally:
        app.cleanup()
