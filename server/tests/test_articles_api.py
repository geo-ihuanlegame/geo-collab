import base64
import struct
import zlib

import pytest

from server.tests.utils import build_test_app


def _make_1x1_png(r: int, g: int, b: int) -> bytes:
    """Create a minimal 1x1 RGB PNG with the given pixel color."""
    def chunk(typ: bytes, data: bytes) -> bytes:
        c = typ + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b'IDAT', zlib.compress(b'\x00' + bytes([r, g, b])))
    iend = chunk(b'IEND', b'')
    return sig + ihdr + idat + iend


PNG_1X1 = _make_1x1_png(255, 255, 255)


def upload_asset(client, filename: str, data: bytes | None = None) -> str:
    response = client.post(
        "/api/assets",
        files={"file": (filename, data or PNG_1X1, "image/png")},
    )
    assert response.status_code == 200
    return response.json()["id"]


def tiptap_doc(*asset_ids: str) -> dict:
    content = [
        {"type": "paragraph", "content": [{"type": "text", "text": "hello"}]},
    ]
    for index, asset_id in enumerate(asset_ids):
        content.append(
            {
                "type": "image",
                "attrs": {
                    "assetId": asset_id,
                    "id": f"image-{index}",
                    "src": f"/api/assets/{asset_id}",
                },
            }
        )
    return {"type": "doc", "content": content}


@pytest.mark.mysql
def test_create_article_syncs_body_images_and_excludes_cover(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        cover_id = upload_asset(client, "cover.png", _make_1x1_png(255, 255, 255))
        image_1 = upload_asset(client, "body-1.png", _make_1x1_png(255, 0, 0))
        image_2 = upload_asset(client, "body-2.png", _make_1x1_png(0, 0, 255))

        response = client.post(
            "/api/articles",
            json={
                "title": "第一篇文章",
                "author": "Geo",
                "cover_asset_id": cover_id,
                "content_json": tiptap_doc(image_1, image_2),
                "content_html": "<p>hello</p>",
                "plain_text": "hello",
                "word_count": 5,
                "status": "ready",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["title"] == "第一篇文章"
        assert payload["cover_asset_id"] == cover_id
        assert [item["asset_id"] for item in payload["body_assets"]] == [image_1, image_2]
        assert [item["position"] for item in payload["body_assets"]] == [0, 1]
        assert cover_id not in [item["asset_id"] for item in payload["body_assets"]]

        detail = client.get(f"/api/articles/{payload['id']}")
        assert detail.status_code == 200
        assert detail.json()["content_json"]["type"] == "doc"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_update_article_rebuilds_body_image_order(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        image_1 = upload_asset(client, "body-1.png", _make_1x1_png(255, 0, 0))
        image_2 = upload_asset(client, "body-2.png", _make_1x1_png(0, 0, 255))

        created = client.post(
            "/api/articles",
            json={
                "title": "排序文章",
                "content_json": tiptap_doc(image_1, image_2),
            },
        ).json()

        response = client.put(
            f"/api/articles/{created['id']}",
            json={
                "content_json": tiptap_doc(image_2),
                "plain_text": "changed",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["plain_text"] == "changed"
        assert [item["asset_id"] for item in payload["body_assets"]] == [image_2]
        assert payload["body_assets"][0]["position"] == 0
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_article_cover_endpoint_does_not_touch_body_assets(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        image_1 = upload_asset(client, "body-1.png", _make_1x1_png(255, 0, 0))
        cover_id = upload_asset(client, "cover.png", _make_1x1_png(255, 255, 255))
        created = client.post(
            "/api/articles",
            json={
                "title": "封面文章",
                "content_json": tiptap_doc(image_1),
            },
        ).json()

        response = client.post(f"/api/articles/{created['id']}/cover", json={"cover_asset_id": cover_id})

        assert response.status_code == 200
        payload = response.json()
        assert payload["cover_asset_id"] == cover_id
        assert [item["asset_id"] for item in payload["body_assets"]] == [image_1]
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_article_crud_list_delete_and_missing_asset(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        missing_asset_response = client.post(
            "/api/articles",
            json={
                "title": "坏文章",
                "cover_asset_id": "missing",
                "content_json": tiptap_doc(),
            },
        )
        assert missing_asset_response.status_code == 400

        created = client.post(
            "/api/articles",
            json={
                "title": "可删除文章",
                "author": "Geo",
                "content_json": tiptap_doc(),
            },
        ).json()

        list_response = client.get("/api/articles", params={"q": "删除"})
        assert list_response.status_code == 200
        assert [item["id"] for item in list_response.json()] == [created["id"]]

        delete_response = client.delete(f"/api/articles/{created['id']}")
        assert delete_response.status_code == 204
        assert client.get(f"/api/articles/{created['id']}").status_code == 404
    finally:
        test_app.cleanup()

