from io import BytesIO

from server.tests.utils import build_test_app


_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _create_publishable_task(test_app) -> int:
    client = test_app.client
    cover = client.post("/api/assets", files={"file": ("cover.png", BytesIO(_PNG), "image/png")}).json()["id"]
    article = client.post(
        "/api/articles",
        json={
            "title": "Worker Claim Article",
            "content_json": {"type": "doc", "content": []},
            "plain_text": "body",
            "cover_asset_id": cover,
        },
    ).json()
    state_dir = test_app.data_dir / "browser_states" / "toutiao" / "worker-claim"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    account = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": "Worker Claim", "account_key": "worker-claim", "use_browser": False},
    ).json()
    task = client.post(
        "/api/tasks",
        json={
            "name": "worker claim",
            "task_type": "single",
            "article_id": article["id"],
            "accounts": [{"account_id": account["id"]}],
        },
    ).json()
    return task["id"]


def test_production_execute_leaves_task_for_worker_claim(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.tasks import router as task_routes
        from server.worker import executor

        monkeypatch.setattr(task_routes, "bg_session_factory", None)
        task_id = _create_publishable_task(test_app)

        response = test_app.client.post(f"/api/tasks/{task_id}/execute")
        assert response.status_code == 202
        assert response.json() == {"queued": True}

        with test_app.session_factory() as db:
            claimed = executor._claim_next_task(db)
            assert claimed is not None
            assert claimed.id == task_id
            assert claimed.worker_id == executor.WORKER_ID
            assert claimed.worker_heartbeat_at is not None
            executor._release_task_claim(db, task_id)
    finally:
        test_app.cleanup()
