from server.tests.utils import build_test_app


def test_system_status_returns_runtime_info(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        response = client.get("/api/system/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["service"] == "ok"
        assert payload["directories_ready"] is True
        assert payload["article_count"] >= 0
        assert payload["account_count"] >= 0
        assert payload["task_count"] >= 0
        assert "browser_ready" in payload
    finally:
        test_app.cleanup()

