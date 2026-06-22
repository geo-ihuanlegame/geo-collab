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
