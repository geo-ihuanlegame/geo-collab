"""POST /api/articles/{id}/ai-illustrate MCP endpoint 鉴权 + 调度集成测试.

mock service 层 illustrate_one，只测 endpoint 把 payload 正确翻成
IllustrateOptions + 返回 IllustrateResult 的字段映射.
"""

from __future__ import annotations

import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_ai_illustrate_endpoint_requires_mcp_token(monkeypatch):
    """不带 X-MCP-Token → 401."""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/articles/1/ai-illustrate",
            json={"main_category_id": 1},
        )
        assert r.status_code == 401
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_endpoint_returns_result_when_authed(monkeypatch):
    """带 token + mock service 返指定 IllustrateResult → 响应 4 字段完整映射."""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        from server.app.modules.articles.ai_illustrate_svc import IllustrateResult

        called: dict = {}

        def fake_illustrate_one(*, article_id, main_category_id, user_id, options, session_factory):
            called["article_id"] = article_id
            called["main_category_id"] = main_category_id
            called["set_cover"] = options.set_cover
            called["include_companion"] = options.include_companion
            return IllustrateResult(
                article_id=article_id,
                images_inserted=5,
                cover_status="set",
                cover_error=None,
                format_error=None,
            )

        monkeypatch.setattr(
            "server.app.modules.articles.router.illustrate_one", fake_illustrate_one
        )

        r = test_app.client.post(
            "/api/articles/123/ai-illustrate",
            json={
                "main_category_id": 42,
                "include_companion": False,
                "set_cover": True,
            },
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["images_inserted"] == 5
        assert body["cover_status"] == "set"
        assert body["cover_error"] is None
        assert body["format_error"] is None
        assert called["article_id"] == 123
        assert called["main_category_id"] == 42
        assert called["include_companion"] is False
    finally:
        test_app.cleanup()
