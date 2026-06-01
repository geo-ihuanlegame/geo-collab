import base64
import hashlib

from server.app.modules.articles.models import Asset  # noqa: F401
from server.tests.utils import build_test_app

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6X8pJ8AAAAASUVORK5CYII="
)


def test_upload_asset_records_metadata_and_serves_file(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        response = client.post(
            "/api/assets",
            files={"file": ("cover.png", PNG_1X1, "image/png")},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["filename"] == "cover.png"
        assert payload["ext"] == ".png"
        assert payload["mime_type"] == "image/png"
        assert payload["size"] == len(PNG_1X1)
        assert payload["width"] == 1
        assert payload["height"] == 1
        assert payload["sha256"]
        assert payload["storage_key"].startswith("assets/")
        assert payload["url"] == f"/api/assets/{payload['id']}"
        assert (test_app.data_dir / payload["storage_key"]).exists()

        meta_response = client.get(f"/api/assets/{payload['id']}/meta")
        assert meta_response.status_code == 200
        assert meta_response.json()["sha256"] == payload["sha256"]

        file_response = client.get(f"/api/assets/{payload['id']}")
        assert file_response.status_code == 200
        assert file_response.content == PNG_1X1
        assert file_response.headers["content-type"].startswith("image/png")
    finally:
        test_app.cleanup()


def test_upload_empty_asset_is_rejected(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        response = client.post(
            "/api/assets",
            files={"file": ("empty.png", b"", "image/png")},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Uploaded file is empty"
    finally:
        test_app.cleanup()


def test_duplicate_upload_recreates_asset_when_original_file_is_missing(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        first = client.post(
            "/api/assets",
            files={"file": ("cover.png", PNG_1X1, "image/png")},
        ).json()
        (test_app.data_dir / first["storage_key"]).unlink()

        response = client.post(
            "/api/assets",
            files={"file": ("cover.png", PNG_1X1, "image/png")},
        )

        assert response.status_code == 200
        second = response.json()
        assert second["sha256"] == first["sha256"]
        assert second["id"] != first["id"]
        assert (test_app.data_dir / second["storage_key"]).exists()
        assert client.get(f"/api/assets/{second['id']}").status_code == 200
    finally:
        test_app.cleanup()


def test_thumbnail_request_falls_back_to_original_when_generation_fails(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        uploaded = client.post(
            "/api/assets",
            files={"file": ("cover.png", PNG_1X1, "image/png")},
        ).json()

        # Assets uploaded without thumbnail generation redirect to the original file
        response = client.get(f"/api/assets/{uploaded['id']}/thumbnail", follow_redirects=True)

        assert response.status_code == 200
        assert response.content == PNG_1X1
        assert response.headers["content-type"].startswith("image/png")
    finally:
        test_app.cleanup()


def test_chunked_upload_accepts_json_body(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        response = client.post(
            "/api/chunked-assets/upload-start",
            json={"total_size": len(PNG_1X1)},
        )
        assert response.status_code == 200
        upload_id = response.json()["upload_id"]

        chunk_response = client.post(
            f"/api/chunked-assets/upload-chunk/{upload_id}",
            params={"chunk_index": 0},
            files={"file": ("chunk_0", PNG_1X1, "application/octet-stream")},
        )
        assert chunk_response.status_code == 200

        complete_response = client.post(
            f"/api/chunked-assets/upload-complete/{upload_id}",
            json={"filename": "cover.png", "content_type": "image/png"},
        )
        assert complete_response.status_code == 200
        payload = complete_response.json()
        assert payload["filename"] == "cover.png"
        assert payload["sha256"] == hashlib.sha256(PNG_1X1).hexdigest()
    finally:
        test_app.cleanup()


def test_chunked_upload_preserves_unsupported_type_status(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    data = b"not an image"

    try:
        response = client.post(
            "/api/chunked-assets/upload-start",
            json={"total_size": len(data)},
        )
        assert response.status_code == 200
        upload_id = response.json()["upload_id"]

        chunk_response = client.post(
            f"/api/chunked-assets/upload-chunk/{upload_id}",
            params={"chunk_index": 0},
            files={"file": ("chunk_0", data, "application/octet-stream")},
        )
        assert chunk_response.status_code == 200

        complete_response = client.post(
            f"/api/chunked-assets/upload-complete/{upload_id}",
            json={"filename": "bad.txt", "content_type": "text/plain"},
        )
        assert complete_response.status_code == 415
        assert complete_response.json()["detail"] == "Unsupported file type"
    finally:
        test_app.cleanup()
