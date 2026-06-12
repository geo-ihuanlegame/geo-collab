import pytest

from server.tests.utils import build_test_app


def _make_cat(client, name, bucket, kind=None):
    body = {"name": name, "bucket_name": bucket}
    if kind is not None:
        body["kind"] = kind
    r = client.post("/api/image-library/categories", json=body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.mysql
def test_category_kind_create_default_and_filter(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        client = app.client
        # 测试环境无 MinIO：把建桶打成 no-op
        monkeypatch.setattr(
            "server.app.modules.image_library.router.minio_store.ensure_bucket",
            lambda *a, **k: None,
        )
        m = _make_cat(client, "主推游戏A", "main-a", "main")
        c = _make_cat(client, "陪衬游戏B", "comp-b", "companion")
        d = _make_cat(client, "默认C", "def-c")  # 省略 kind → 默认 companion
        assert m["kind"] == "main"
        assert c["kind"] == "companion"
        assert d["kind"] == "companion"

        main_ids = {x["id"] for x in client.get("/api/image-library/categories?kind=main").json()}
        comp_ids = {
            x["id"] for x in client.get("/api/image-library/categories?kind=companion").json()
        }
        all_ids = {x["id"] for x in client.get("/api/image-library/categories").json()}

        assert main_ids == {m["id"]}
        assert {c["id"], d["id"]} <= comp_ids and m["id"] not in comp_ids
        assert {m["id"], c["id"], d["id"]} <= all_ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_category_kind_reclassify(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        client = app.client
        monkeypatch.setattr(
            "server.app.modules.image_library.router.minio_store.ensure_bucket",
            lambda *a, **k: None,
        )
        c = _make_cat(client, "栏目X", "x-bucket", "companion")
        r = client.patch(f"/api/image-library/categories/{c['id']}", json={"kind": "main"})
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "main"
    finally:
        app.cleanup()
