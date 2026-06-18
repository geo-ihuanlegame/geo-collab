from server.tests.utils import build_test_app


def test_feishu_notify_requires_mcp_token(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/system/feishu-notify",
            json={"title": "test", "message": "hello"},
        )
        assert r.status_code == 401
    finally:
        test_app.cleanup()


def test_feishu_notify_returns_sent_false_when_webhook_unset(monkeypatch):
    monkeypatch.setenv("GEO_FEISHU_WEBHOOK_URL", "")
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/system/feishu-notify",
            json={"title": "t", "message": "m", "level": "info"},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200
        assert r.json() == {"sent": False}  # webhook 未配置，返回 False
    finally:
        test_app.cleanup()
