from io import BytesIO

from server.app.modules.tasks.drivers.toutiao import PublishFillResult
from server.tests.utils import build_test_app


class FakePublisher:
    def __init__(self, result=None):
        self.result = result or PublishFillResult(
            url="https://mp.toutiao.com/article/123456",
            title="test article",
            message="发布成功: https://mp.toutiao.com/article/123456",
        )

    def __call__(self, article, account, *, stop_before_publish=False):
        return self.result


def _execute_and_wait(client, task_id: int, max_wait: float = 5.0) -> dict:
    resp = client.post(f"/api/tasks/{task_id}/execute")
    assert resp.status_code == 202
    assert resp.json() == {"queued": True}
    import time as _time

    deadline = _time.time() + max_wait
    while _time.time() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] not in ("pending", "running", "queued"):
            if task["status"] in ("succeeded", "failed", "partial_failed", "cancelled"):
                return task
        _time.sleep(0.05)
    raise AssertionError(
        f"Task {task_id} did not complete within {max_wait}s (last status: {task.get('status', '?')})"
    )


def _write_storage_state(data_dir, account_key: str) -> None:
    state_dir = data_dir / "browser_states" / "toutiao" / account_key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")


def _create_account(client, data_dir, account_key: str, display_name: str) -> int:
    _write_storage_state(data_dir, account_key)
    resp = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": display_name, "account_key": account_key, "use_browser": False},
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def _create_article(
    client,
    title: str,
    *,
    plain_text: str = "",
    cover_asset_id: str | None = None,
    content_json: dict | None = None,
) -> int:
    resp = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": content_json or {"type": "doc", "content": []},
            "plain_text": plain_text,
            "cover_asset_id": cover_asset_id,
        },
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def _upload_cover_image(client) -> str:
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    resp = client.post(
        "/api/assets",
        files={"file": ("cover.png", BytesIO(png_bytes), "image/png")},
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def test_empty_body_fails_publish(monkeypatch):
    """Article with empty body -> record fails with '正文' in error message."""
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_id = _create_article(client, "Test Article", plain_text="", cover_asset_id=cover_id)
        account_id = _create_account(client, test_app.data_dir, "account-x", "Account X")

        task = client.post(
            "/api/tasks",
            json={
                "name": "empty body test",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        executed = _execute_and_wait(client, task["id"])

        assert executed["status"] == "failed"
        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records[0]["status"] == "failed"
        assert "正文" in records[0]["error_message"]
    finally:
        test_app.cleanup()


def test_image_only_body_is_publishable(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(),
        )
        cover_id = _upload_cover_image(client)
        body_image_id = _upload_cover_image(client)
        article_id = _create_article(
            client,
            "Image Body Article",
            plain_text="",
            cover_asset_id=cover_id,
            content_json={
                "type": "doc",
                "content": [
                    {
                        "type": "image",
                        "attrs": {
                            "assetId": body_image_id,
                            "src": f"/api/assets/{body_image_id}?token=old",
                        },
                    }
                ],
            },
        )
        account_id = _create_account(client, test_app.data_dir, "account-img", "Account Img")

        task = client.post(
            "/api/tasks",
            json={
                "name": "image body test",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        executed = _execute_and_wait(client, task["id"])

        assert executed["status"] == "succeeded"
        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records[0]["status"] == "succeeded"
    finally:
        test_app.cleanup()


def test_no_cover_fails_publish(monkeypatch):
    """Article without cover -> record fails with '封面' in error message."""
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(),
        )
        article_id = _create_article(
            client, "Test Article", plain_text="Some body text", cover_asset_id=None
        )
        account_id = _create_account(client, test_app.data_dir, "account-y", "Account Y")

        task = client.post(
            "/api/tasks",
            json={
                "name": "no cover test",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        executed = _execute_and_wait(client, task["id"])

        assert executed["status"] == "failed"
        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records[0]["status"] == "failed"
        assert "封面" in records[0]["error_message"]
    finally:
        test_app.cleanup()


def test_empty_title_fails_publish(monkeypatch):
    """Article with empty title -> record fails with '标题' in error message."""
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_id = _create_article(
            client, "Will Be Cleared", plain_text="Some body text", cover_asset_id=cover_id
        )
        account_id = _create_account(client, test_app.data_dir, "account-z", "Account Z")

        with test_app.session_factory() as db:
            from server.app.modules.articles.models import Article

            article = db.get(Article, article_id)
            assert article is not None
            article.title = ""
            db.commit()

        task = client.post(
            "/api/tasks",
            json={
                "name": "empty title test",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        executed = _execute_and_wait(client, task["id"])

        assert executed["status"] == "failed"
        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records[0]["status"] == "failed"
        assert "标题" in records[0]["error_message"]
    finally:
        test_app.cleanup()
