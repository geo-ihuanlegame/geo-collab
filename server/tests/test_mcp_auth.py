from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from server.app.core.mcp_auth import require_mcp_token


def _app():
    app = FastAPI()

    @app.get("/probe", dependencies=[Depends(require_mcp_token)])
    def probe():
        return {"ok": True}

    return TestClient(app)


def test_missing_token_returns_401(monkeypatch):
    from server.app.core import config

    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-abc")
    config.get_settings.cache_clear()
    r = _app().get("/probe")
    assert r.status_code == 401


def test_wrong_token_returns_401(monkeypatch):
    from server.app.core import config

    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-abc")
    config.get_settings.cache_clear()
    r = _app().get("/probe", headers={"X-MCP-Token": "wrong"})
    assert r.status_code == 401


def test_correct_token_passes(monkeypatch):
    from server.app.core import config

    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-abc")
    config.get_settings.cache_clear()
    r = _app().get("/probe", headers={"X-MCP-Token": "secret-abc"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_empty_configured_token_rejects_all(monkeypatch):
    from server.app.core import config

    monkeypatch.setenv("GEO_MCP_TOKEN", "")
    config.get_settings.cache_clear()
    r = _app().get("/probe", headers={"X-MCP-Token": ""})
    assert r.status_code == 401  # 空配置等于禁用 MCP，绝不能放过任何请求


def test_verify_mcp_token_unconfigured(monkeypatch):
    from server.app.core.config import get_settings
    from server.app.core.mcp_auth import verify_mcp_token

    monkeypatch.setenv("GEO_MCP_TOKEN", "")
    get_settings.cache_clear()
    ok, detail = verify_mcp_token("any-token")
    assert ok is False
    assert detail == "MCP token not configured"


def test_verify_mcp_token_mismatch(monkeypatch):
    from server.app.core.config import get_settings
    from server.app.core.mcp_auth import verify_mcp_token

    monkeypatch.setenv("GEO_MCP_TOKEN", "real-token")
    get_settings.cache_clear()
    ok, detail = verify_mcp_token("wrong-token")
    assert ok is False
    assert detail == "invalid MCP token"


def test_verify_mcp_token_match(monkeypatch):
    from server.app.core.config import get_settings
    from server.app.core.mcp_auth import verify_mcp_token

    monkeypatch.setenv("GEO_MCP_TOKEN", "real-token")
    get_settings.cache_clear()
    ok, detail = verify_mcp_token("real-token")
    assert ok is True
    assert detail == ""
