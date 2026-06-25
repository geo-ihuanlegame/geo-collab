"""MCP 接入端点测试：/api/mcp/status (user JWT) + /api/mcp/health (MCP token)。

T5 阶段覆盖 `/status`：configured 标志、tools_count、suggested_base_url 形态、
未登录 401。`/health` 端点的测试在 T6 加。
"""

from __future__ import annotations

import pytest

from server.app.core import config as core_config
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def test_status_returns_configured_true_when_token_set(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "test-token-abc")
    core_config.get_settings.cache_clear()
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.get("/api/mcp/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["configured"] is True
        assert body["tools_count"] == 20
        assert body["suggested_base_url"].startswith("http")
        assert not body["suggested_base_url"].endswith("/")
    finally:
        test_app.cleanup()
        core_config.get_settings.cache_clear()


def test_status_returns_configured_false_when_token_empty(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "")
    core_config.get_settings.cache_clear()
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.get("/api/mcp/status")
        assert resp.status_code == 200, resp.text
        assert resp.json()["configured"] is False
    finally:
        test_app.cleanup()
        core_config.get_settings.cache_clear()


def test_status_requires_auth(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "test-token-abc")
    core_config.get_settings.cache_clear()
    test_app = build_test_app(monkeypatch)
    try:
        # 临时清空登录 cookie 模拟未登录
        test_app.client.cookies.clear()
        resp = test_app.client.get("/api/mcp/status")
        # cookie-based auth missing → 401
        assert resp.status_code == 401, resp.text
    finally:
        test_app.cleanup()
        core_config.get_settings.cache_clear()


def test_health_ok_with_correct_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-xyz")
    core_config.get_settings.cache_clear()
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.get("/api/mcp/health", headers={"X-MCP-Token": "secret-xyz"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
    finally:
        test_app.cleanup()
        core_config.get_settings.cache_clear()


def test_health_401_with_wrong_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-xyz")
    core_config.get_settings.cache_clear()
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.get("/api/mcp/health", headers={"X-MCP-Token": "wrong-token"})
        assert resp.status_code == 401, resp.text
    finally:
        test_app.cleanup()
        core_config.get_settings.cache_clear()


def test_health_401_with_no_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-xyz")
    core_config.get_settings.cache_clear()
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.get("/api/mcp/health")
        assert resp.status_code == 401, resp.text
    finally:
        test_app.cleanup()
        core_config.get_settings.cache_clear()


def test_health_401_when_token_disabled(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "")
    core_config.get_settings.cache_clear()
    test_app = build_test_app(monkeypatch)
    try:
        # Even with a token in header, server-side empty config rejects all
        resp = test_app.client.get("/api/mcp/health", headers={"X-MCP-Token": "anything"})
        assert resp.status_code == 401, resp.text
    finally:
        test_app.cleanup()
        core_config.get_settings.cache_clear()
