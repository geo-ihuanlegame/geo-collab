from server.app.modules.accounts.models import Account
from server.app.modules.tasks.drivers.toutiao import PublishFillResult, ToutiaoPublishError
from server.tests.utils import build_test_app


def _execute_and_wait(client, task_id: int, max_wait: float = 5.0) -> dict:
    """后台执行任务并轮询直到完成（含 started_at 确认）。"""
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


class FakePublisher:
    def __init__(
        self, result: PublishFillResult | None = None, error: ToutiaoPublishError | None = None
    ):
        self.result = result or PublishFillResult(
            url="https://mp.toutiao.com/article/123456",
            title="test article",
            message="发布成功: https://mp.toutiao.com/article/123456",
        )
        self.error = error

    def _runner(self, article, account, *, stop_before_publish=False):
        if self.error is not None:
            raise self.error
        return self.result


def write_storage_state(data_dir, account_key: str) -> None:
    state_dir = data_dir / "browser_states" / "toutiao" / account_key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")


def create_account(client, data_dir, account_key: str, display_name: str) -> int:
    write_storage_state(data_dir, account_key)
    response = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": display_name, "account_key": account_key, "use_browser": False},
    )
    assert response.status_code == 200
    return response.json()["id"]


def create_article(
    client, title: str, *, plain_text: str = "", cover_asset_id: str | None = None
) -> int:
    response = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": {"type": "doc", "content": []},
            "plain_text": plain_text,
            "cover_asset_id": cover_asset_id,
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def _upload_cover_image(client) -> str:
    from io import BytesIO

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


def test_create_single_task_generates_one_publish_record(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article_id = create_article(client, "Article A")
        account_id = create_account(client, test_app.data_dir, "account-a", "Account A")

        response = client.post(
            "/api/tasks",
            json={
                "name": "single publish",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        )

        assert response.status_code == 200
        task = response.json()
        assert task["task_type"] == "single"
        assert task["status"] == "pending"
        assert task["article_id"] == article_id
        assert task["group_id"] is None
        assert task["record_count"] == 1
        assert task["accounts"] == [
            {
                "account_id": account_id,
                "sort_order": 0,
                "display_name": "Account A",
                "status": "valid",
            }
        ]

        detail = client.get(f"/api/tasks/{task['id']}")
        assert detail.status_code == 200
        assert detail.json()["id"] == task["id"]

        records = client.get(f"/api/tasks/{task['id']}/records")
        assert records.status_code == 200
        assert records.json() == [
            {
                "id": records.json()[0]["id"],
                "task_id": task["id"],
                "article_id": article_id,
                "platform_id": task["platform_id"],
                "account_id": account_id,
                "status": "pending",
                "queue_reason": None,
                "publish_url": None,
                "error_message": None,
                "retry_of_record_id": None,
                "started_at": None,
                "finished_at": None,
                "lease_until": None,
                "remote_browser_session_id": None,
                "novnc_url": None,
            }
        ]

        listed = client.get("/api/tasks")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [task["id"]]
    finally:
        test_app.cleanup()


def test_create_group_task_generates_records_in_group_order_and_account_order(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article_1 = create_article(client, "Article A")
        article_2 = create_article(client, "Article B")
        article_3 = create_article(client, "Article C")
        account_1 = create_account(client, test_app.data_dir, "account-a", "Account A")
        account_2 = create_account(client, test_app.data_dir, "account-b", "Account B")

        group = client.post("/api/article-groups", json={"name": "Batch"}).json()
        update = client.put(
            f"/api/article-groups/{group['id']}/items",
            json={
                "items": [
                    {"article_id": article_2, "sort_order": 20},
                    {"article_id": article_1, "sort_order": 10},
                    {"article_id": article_3, "sort_order": 30},
                ]
            },
        )
        assert update.status_code == 200

        response = client.post(
            "/api/tasks",
            json={
                "name": "group publish",
                "task_type": "group_round_robin",
                "group_id": group["id"],
                "accounts": [
                    {"account_id": account_2, "sort_order": 20},
                    {"account_id": account_1, "sort_order": 10},
                ],
                "stop_before_publish": False,
            },
        )

        assert response.status_code == 200
        task = response.json()
        assert task["record_count"] == 3
        assert [item["account_id"] for item in task["accounts"]] == [account_1, account_2]

        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert [record["article_id"] for record in records] == [article_1, article_2, article_3]
        assert [record["account_id"] for record in records] == [account_1, account_2, account_1]
    finally:
        test_app.cleanup()


def test_group_assignment_preview_matches_created_records(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article_1 = create_article(client, "Article A")
        article_2 = create_article(client, "Article B")
        article_3 = create_article(client, "Article C")
        article_4 = create_article(client, "Article D")
        account_1 = create_account(client, test_app.data_dir, "account-a", "Account A")
        account_2 = create_account(client, test_app.data_dir, "account-b", "Account B")
        account_3 = create_account(client, test_app.data_dir, "account-c", "Account C")

        group = client.post("/api/article-groups", json={"name": "Round Robin Batch"}).json()
        update = client.put(
            f"/api/article-groups/{group['id']}/items",
            json={
                "items": [
                    {"article_id": article_3, "sort_order": 30},
                    {"article_id": article_1, "sort_order": 10},
                    {"article_id": article_4, "sort_order": 40},
                    {"article_id": article_2, "sort_order": 20},
                ]
            },
        )
        assert update.status_code == 200

        payload = {
            "name": "group publish",
            "task_type": "group_round_robin",
            "group_id": group["id"],
            "accounts": [
                {"account_id": account_3, "sort_order": 30},
                {"account_id": account_1, "sort_order": 10},
                {"account_id": account_2, "sort_order": 20},
            ],
        }
        preview = client.post("/api/tasks/preview", json=payload)

        assert preview.status_code == 200
        assert preview.json()["article_count"] == 4
        assert preview.json()["account_count"] == 3
        assert [(item["article_id"], item["account_id"]) for item in preview.json()["items"]] == [
            (article_1, account_1),
            (article_2, account_2),
            (article_3, account_3),
            (article_4, account_1),
        ]

        created = client.post("/api/tasks", json={**payload, "stop_before_publish": False})
        assert created.status_code == 200
        records = client.get(f"/api/tasks/{created.json()['id']}/records").json()
        assert [(record["article_id"], record["account_id"]) for record in records] == [
            (item["article_id"], item["account_id"]) for item in preview.json()["items"]
        ]
        assert len({record["article_id"] for record in records}) == len(records)
    finally:
        test_app.cleanup()


def test_create_task_rejects_invalid_or_expired_account(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article_id = create_article(client, "Article A")
        account_id = create_account(client, test_app.data_dir, "account-a", "Account A")

        with test_app.session_factory() as db:
            account = db.get(Account, account_id)
            assert account is not None
            account.status = "expired"
            db.commit()

        expired = client.post(
            "/api/tasks",
            json={
                "name": "single publish",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
            },
        )
        assert expired.status_code == 400
        assert "expired" in expired.json()["detail"]

        missing = client.post(
            "/api/tasks",
            json={
                "name": "single publish",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": 9999}],
            },
        )
        assert missing.status_code == 400
        assert "Account not found" in missing.json()["detail"]
    finally:
        test_app.cleanup()


def test_execute_single_task_auto_succeeds(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher()._runner,
        )
        cover_id = _upload_cover_image(client)
        article_id = create_article(
            client, "Article A", plain_text="Article body text", cover_asset_id=cover_id
        )
        account_id = create_account(client, test_app.data_dir, "account-a", "Account A")
        task = client.post(
            "/api/tasks",
            json={
                "name": "single publish",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
            },
        ).json()
        assert task["stop_before_publish"] is False

        executed = _execute_and_wait(client, task["id"])

        assert executed["status"] == "succeeded"
        assert executed["started_at"] is not None
        assert executed["finished_at"] is not None

        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records[0]["status"] == "succeeded"
        assert records[0]["publish_url"] == "https://mp.toutiao.com/article/123456"
        assert records[0]["finished_at"] is not None

        logs = client.get(f"/api/tasks/{task['id']}/logs").json()
        assert any(log["level"] == "info" and "发布成功" in log["message"] for log in logs)
    finally:
        test_app.cleanup()


def test_execute_task_records_publish_diagnostics(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    def runner(article, account, *, stop_before_publish=False):
        from server.app.shared.diagnostics import record_publish_diagnostic

        record_publish_diagnostic("probe step elapsed_ms=12", level="warn")
        return PublishFillResult(
            url="https://mp.toutiao.com/article/diagnostic",
            title=article.title,
            message="published",
        )

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: runner,
        )
        cover_id = _upload_cover_image(client)
        article_id = create_article(
            client, "Diagnostic Article", plain_text="Article body", cover_asset_id=cover_id
        )
        account_id = create_account(
            client, test_app.data_dir, "account-diagnostic", "Account Diagnostic"
        )
        task = client.post(
            "/api/tasks",
            json={
                "name": "diagnostic publish",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
            },
        ).json()

        executed = _execute_and_wait(client, task["id"])

        assert executed["status"] == "succeeded"
        logs = client.get(f"/api/tasks/{task['id']}/logs").json()
        assert any(
            "[publish diagnostic] probe step elapsed_ms=12" in log["message"] for log in logs
        )
    finally:
        test_app.cleanup()


def test_execute_group_task_auto_completes_all_records(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher()._runner,
        )
        cover_id = _upload_cover_image(client)
        article_1 = create_article(
            client, "Article A", plain_text="Body A", cover_asset_id=cover_id
        )
        article_2 = create_article(
            client, "Article B", plain_text="Body B", cover_asset_id=cover_id
        )
        article_3 = create_article(
            client, "Article C", plain_text="Body C", cover_asset_id=cover_id
        )
        account_id = create_account(client, test_app.data_dir, "account-a", "Account A")

        group = client.post("/api/article-groups", json={"name": "Batch"}).json()
        client.put(
            f"/api/article-groups/{group['id']}/items",
            json={
                "items": [
                    {"article_id": article_1, "sort_order": 10},
                    {"article_id": article_2, "sort_order": 20},
                    {"article_id": article_3, "sort_order": 30},
                ]
            },
        )
        task = client.post(
            "/api/tasks",
            json={
                "name": "group publish",
                "task_type": "group_round_robin",
                "group_id": group["id"],
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        executed = _execute_and_wait(client, task["id"])

        assert executed["status"] == "succeeded"

        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert [record["status"] for record in records] == ["succeeded", "succeeded", "succeeded"]
        assert all(record["publish_url"] is not None for record in records)
    finally:
        test_app.cleanup()


def test_cancel_pending_task_before_execute(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article_id = create_article(client, "Article A")
        account_id = create_account(client, test_app.data_dir, "account-a", "Account A")
        task = client.post(
            "/api/tasks",
            json={
                "name": "single publish",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        cancelled = client.post(f"/api/tasks/{task['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["finished_at"] is not None

        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records[0]["status"] == "cancelled"
    finally:
        test_app.cleanup()


def test_execute_task_records_publisher_failure_with_screenshot(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: (
                FakePublisher(
                    error=ToutiaoPublishError("Toutiao title field not found", screenshot=b"png")
                )._runner
            ),
        )
        cover_id = _upload_cover_image(client)
        article_id = create_article(
            client, "Article A", plain_text="Article body", cover_asset_id=cover_id
        )
        account_id = create_account(client, test_app.data_dir, "account-a", "Account A")
        task = client.post(
            "/api/tasks",
            json={
                "name": "single publish",
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
        assert records[0]["error_message"].startswith("Toutiao title field not found")

        logs = client.get(f"/api/tasks/{task['id']}/logs").json()
        error_log = next(log for log in logs if log["level"] == "error")
        assert error_log["screenshot_asset_id"] is not None
        assert client.get(f"/api/assets/{error_log['screenshot_asset_id']}/meta").status_code == 200
    finally:
        test_app.cleanup()


def test_publisher_failure_in_group_task_auto_advances_to_next_record(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        call_count = {"n": 0}

        def make_publisher(record):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FakePublisher(error=ToutiaoPublishError("Fill failed"))._runner
            return FakePublisher()._runner

        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record", make_publisher
        )

        cover_id = _upload_cover_image(client)
        article_1 = create_article(
            client, "Article A", plain_text="Body A", cover_asset_id=cover_id
        )
        article_2 = create_article(
            client, "Article B", plain_text="Body B", cover_asset_id=cover_id
        )
        account_id = create_account(client, test_app.data_dir, "account-a", "Account A")

        group = client.post("/api/article-groups", json={"name": "G"}).json()
        client.put(
            f"/api/article-groups/{group['id']}/items",
            json={
                "items": [
                    {"article_id": article_1, "sort_order": 10},
                    {"article_id": article_2, "sort_order": 20},
                ]
            },
        )

        task = client.post(
            "/api/tasks",
            json={
                "name": "group publish",
                "task_type": "group_round_robin",
                "group_id": group["id"],
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        executed = _execute_and_wait(client, task["id"])
        assert executed["status"] == "partial_failed"

        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records[0]["status"] == "failed"
        assert records[1]["status"] == "succeeded"
    finally:
        test_app.cleanup()


def test_retry_failed_record_creates_pending_record_and_resets_task(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(error=ToutiaoPublishError("Fill failed"))._runner,
        )
        article_id = create_article(client, "Article A")
        account_id = create_account(client, test_app.data_dir, "account-a", "Account A")
        task = client.post(
            "/api/tasks",
            json={
                "name": "single publish",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()
        _execute_and_wait(client, task["id"])

        task_before = client.get(f"/api/tasks/{task['id']}").json()
        assert task_before["status"] == "failed"
        records_before = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records_before[0]["status"] == "failed"

        retried = client.post(f"/api/publish-records/{records_before[0]['id']}/retry")
        assert retried.status_code == 200
        assert retried.json()["retry_of_record_id"] == records_before[0]["id"]

        # Wait for auto-execute to complete (FakePublisher always fails)
        import time as _time

        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            task_status = client.get(f"/api/tasks/{task['id']}").json()
            if task_status["status"] in ("failed", "partial_failed", "succeeded", "cancelled"):
                break
            _time.sleep(0.1)

        task_after = client.get(f"/api/tasks/{task['id']}").json()
        assert task_after["status"] == "failed"

        records_after = client.get(f"/api/tasks/{task['id']}/records").json()
        assert len(records_after) == 2
        assert records_after[0]["status"] == "failed"
        assert records_after[1]["status"] == "failed"
    finally:
        test_app.cleanup()


def test_retry_record_cannot_create_duplicate_retry_chain(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(error=ToutiaoPublishError("Fill failed"))._runner,
        )
        article_id = create_article(client, "Article A")
        account_id = create_account(client, test_app.data_dir, "account-a", "Account A")
        task = client.post(
            "/api/tasks",
            json={
                "name": "single publish",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()
        _execute_and_wait(client, task["id"])

        original = client.get(f"/api/tasks/{task['id']}/records").json()[0]
        retried = client.post(f"/api/publish-records/{original['id']}/retry")
        assert retried.status_code == 200

        duplicate_retry = client.post(f"/api/publish-records/{original['id']}/retry")
        assert duplicate_retry.status_code == 400
        assert "already has retry record" in duplicate_retry.json()["detail"]

        # Poll for auto-execute completion instead of POST execute
        import time as _time

        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            task_status = client.get(f"/api/tasks/{task['id']}").json()
            if task_status["status"] in ("failed", "partial_failed", "succeeded", "cancelled"):
                break
            _time.sleep(0.1)

        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert len(records) == 2
        assert records[1]["status"] == "failed"

        retry_of_retry = client.post(f"/api/publish-records/{records[1]['id']}/retry")
        assert retry_of_retry.status_code == 400
        assert "Retry records cannot be retried again" in retry_of_retry.json()["detail"]

        records_after = client.get(f"/api/tasks/{task['id']}/records").json()
        assert len(records_after) == 2
    finally:
        test_app.cleanup()
