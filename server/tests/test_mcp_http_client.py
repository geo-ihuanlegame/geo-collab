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


def test_mcp_config_internal_api_url_defaults_to_localhost(monkeypatch):
    """internal_api_url 缺失时回退到 127.0.0.1:8000，不复用 api_base_url。"""
    from server.mcp.config import McpConfig

    monkeypatch.setenv("GEO_MCP_TOKEN", "dummy-token-for-test")
    monkeypatch.setenv("GEO_API_BASE_URL", "https://geo.example.com")
    monkeypatch.delenv("GEO_MCP_INTERNAL_API_URL", raising=False)
    cfg = McpConfig()
    assert cfg.api_base_url == "https://geo.example.com"
    assert cfg.internal_api_url == "http://127.0.0.1:8000"


def test_mcp_config_internal_api_url_respects_env(monkeypatch):
    """显式设 GEO_MCP_INTERNAL_API_URL 时尊重该值。"""
    from server.mcp.config import McpConfig

    monkeypatch.setenv("GEO_MCP_TOKEN", "dummy-token-for-test")
    monkeypatch.setenv("GEO_MCP_INTERNAL_API_URL", "http://localhost:8123")
    cfg = McpConfig()
    assert cfg.internal_api_url == "http://localhost:8123"
