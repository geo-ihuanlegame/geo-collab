import pytest

from server.tests.utils import build_test_app


def _patch_minio(monkeypatch):
    """测试环境无 MinIO：建桶/删桶/上传全部打成 no-op。"""
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.ensure_bucket",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.remove_bucket",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.upload_image",
        lambda *a, **k: None,
    )


def _make_cat(client, name="文件夹A", bucket="folder-a", kind="main"):
    r = client.post(
        "/api/image-library/categories",
        json={"name": name, "bucket_name": bucket, "kind": kind},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _upload(client, category_id):
    r = client.post(
        f"/api/image-library/images?category_id={category_id}",
        files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")},
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.mysql
def test_delete_empty_folder_succeeds(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        client = app.client
        cat = _make_cat(client)
        r = client.delete(f"/api/image-library/categories/{cat['id']}")
        assert r.status_code == 204, r.text
        # 已不在列表里
        ids = {x["id"] for x in client.get("/api/image-library/categories").json()}
        assert cat["id"] not in ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_nonempty_folder_409(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        client = app.client
        cat = _make_cat(client)
        _upload(client, cat["id"])
        r = client.delete(f"/api/image-library/categories/{cat['id']}")
        assert r.status_code == 409, r.text
        # 仍然存在
        ids = {x["id"] for x in client.get("/api/image-library/categories").json()}
        assert cat["id"] in ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_missing_folder_404(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        client = app.client
        r = client.delete("/api/image-library/categories/999999")
        assert r.status_code == 404, r.text
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_create_folder_without_bucket_auto_slugs(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        client = app.client
        # 只给名字，不给 bucket_name
        r = client.post(
            "/api/image-library/categories",
            json={"name": "餐厅养成记", "kind": "main"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # 后端自动派生了非空 bucket（拼音 slug），且符合 S3 命名（小写字母数字，3~63）
        assert body["bucket_name"]
        assert body["bucket_name"].isalnum() and body["bucket_name"].islower()
        assert 3 <= len(body["bucket_name"]) <= 63
        assert body["kind"] == "main"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_create_folder_with_explicit_bucket_still_works(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        client = app.client
        cat = _make_cat(client, name="显式桶", bucket="explicit-bucket", kind="companion")
        assert cat["bucket_name"] == "explicit-bucket"
    finally:
        app.cleanup()
