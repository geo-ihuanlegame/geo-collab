"""MCP HTTP transport mount + 中间件测试。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.app.core.config import get_settings
from server.app.core.mcp_auth import McpTokenMiddleware


def _app_with_middleware() -> FastAPI:
    """裸 FastAPI app + McpTokenMiddleware,挂一个 echo endpoint。"""
    app = FastAPI()
    app.add_middleware(McpTokenMiddleware)

    @app.post("/echo")
    async def echo() -> dict:
        return {"ok": True}

    return app


def test_middleware_blocks_request_without_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "right-token")
    get_settings.cache_clear()
    client = TestClient(_app_with_middleware())
    resp = client.post("/echo")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid MCP token"


def test_middleware_blocks_request_with_wrong_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "right-token")
    get_settings.cache_clear()
    client = TestClient(_app_with_middleware())
    resp = client.post("/echo", headers={"X-MCP-Token": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid MCP token"


def test_middleware_passes_request_with_correct_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "right-token")
    get_settings.cache_clear()
    client = TestClient(_app_with_middleware())
    resp = client.post("/echo", headers={"X-MCP-Token": "right-token"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_middleware_blocks_request_when_no_token_configured(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "")
    get_settings.cache_clear()
    client = TestClient(_app_with_middleware())
    resp = client.post("/echo", headers={"X-MCP-Token": "anything"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "MCP token not configured"


@pytest.mark.mysql
def test_mcp_endpoint_mounted_with_auth(monkeypatch):
    """create_app() 起的 app 里 /mcp 路径存在 + 走 McpTokenMiddleware。"""
    from server.tests.utils import build_test_app  # noqa: PLC0415

    monkeypatch.setenv("GEO_MCP_TOKEN", "real-token")
    get_settings.cache_clear()

    test_app = build_test_app(monkeypatch)
    try:
        client = test_app.client
        # 不带 token POST /mcp/ — middleware 应拦截。
        # streamable HTTP endpoint 路径是 /mcp/(含尾 slash);不带尾 slash 的 /mcp 会
        # 307 redirect 到 /mcp/ 再走 middleware,两条都应 401。
        resp_unauth = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert resp_unauth.status_code == 401
        detail = resp_unauth.json()["detail"]
        assert detail in ("invalid MCP token", "MCP token not configured")
    finally:
        test_app.cleanup()
