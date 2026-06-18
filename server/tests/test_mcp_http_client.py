import httpx
import pytest

from server.mcp.http_client import ApiError, GeoApiClient


def test_get_attaches_token_header(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-xyz")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True, "data": [1, 2]})

    transport = httpx.MockTransport(handler)
    client = GeoApiClient(base_url="http://test", transport=transport, token="secret-xyz")
    resp = client.get("/api/articles", params={"limit": 5})

    assert resp == {"ok": True, "data": [1, 2]}
    assert captured["headers"]["x-mcp-token"] == "secret-xyz"


def test_get_returns_error_on_4xx(monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"detail": "bad request"})

    client = GeoApiClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        token="t",
    )
    with pytest.raises(ApiError) as exc:
        client.get("/api/articles")
    assert "400" in str(exc.value)
    assert "bad request" in str(exc.value)


def test_post_json_body_and_header(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = request.read().decode()
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"ok": True})

    client = GeoApiClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        token="t",
    )
    client.post("/api/articles/score", json={"article_ids": [1, 2]})

    assert (
        '"article_ids": [1, 2]'
        in captured["body"].replace(" ", "").replace("[", "[ ").replace("]", " ]")
        or '"article_ids":[1,2]' in captured["body"]
    )
    assert "application/json" in captured["content_type"]
