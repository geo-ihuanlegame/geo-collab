import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_create_run_when_factory_missing_fails_run_not_pending(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        pid = client.post("/api/pipelines", json={"name": "无 factory 流"}).json()["id"]
        snapshot = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "input",
                    "name": "源",
                    "node_index": 0,
                    "config": {"question_text": "x"},
                    "flow_meta": None,
                }
            ],
        }
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        client.post(f"/api/pipelines/{pid}/publish", json={})

        monkeypatch.setattr("server.app.modules.pipelines.router.bg_session_factory", None)
        resp = client.post(f"/api/pipelines/{pid}/runs")
        assert resp.status_code == 503, resp.text
        run_id = resp.json().get("run_id")
        assert run_id is not None, resp.text
        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["status"] == "failed"  # 不能卡 pending
    finally:
        test_app.cleanup()
