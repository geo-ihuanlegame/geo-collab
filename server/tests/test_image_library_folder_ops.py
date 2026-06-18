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
        "server.app.modules.image_library.router.minio_store.empty_bucket",
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


# 删非空栏目（硬删，204）与删不存在栏目（404）的覆盖已迁到 test_image_library_delete.py
# （test_delete_non_empty_category / test_delete_nonexistent_category_404）。
# 旧的 test_delete_nonempty_folder_409 断言「非空→409 拦截」与 #124 改成硬删后的行为矛盾，已删除。


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
