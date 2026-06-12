import threading
import time as _time

import pytest

from server.app.modules.tasks.drivers.toutiao import (
    PublishFillResult,
    ToutiaoPublishError,
    ToutiaoUserInputRequired,
)
from server.app.modules.tasks.models import PublishRecord
from server.tests.utils import build_test_app


def _execute_and_wait(client, task_id: int, max_wait: float = 5.0) -> dict:
    resp = client.post(f"/api/tasks/{task_id}/execute")
    assert resp.status_code == 202
    assert resp.json() == {"queued": True}
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
    def __init__(self, result=None, error=None):
        self.result = result or PublishFillResult(
            url="https://example.com/article/1",
            title="test",
            message="发布成功: https://example.com/article/1",
        )
        self.error = error

    def __call__(self, article, account, *, stop_before_publish=False):
        if self.error is not None:
            raise self.error
        return self.result


def _create_article(
    client, title: str, *, plain_text: str = "", cover_asset_id: str | None = None
) -> int:
    resp = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": {"type": "doc", "content": []},
            "plain_text": plain_text,
            "cover_asset_id": cover_asset_id,
        },
    )
    assert resp.status_code == 200
    return resp.json()["id"]


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


# ---------------------------------------------------------------------------
# 测试 1：stop_before_publish=True 时记录进入 waiting_manual_publish
# ---------------------------------------------------------------------------
@pytest.mark.mysql
def test_stop_before_publish_enters_waiting_state(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_id = _create_article(
            client, "Article A", plain_text="Body A", cover_asset_id=cover_id
        )
        account_id = _create_account(client, test_app.data_dir, "account-a", "Account A")
        task = client.post(
            "/api/tasks",
            json={
                "name": "manual publish task",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": True,
            },
        ).json()

        resp = client.post(f"/api/tasks/{task['id']}/execute")
        assert resp.status_code == 202
        assert resp.json() == {"queued": True}

        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            records = client.get(f"/api/tasks/{task['id']}/records").json()
            if records[0]["status"] == "waiting_manual_publish":
                break
            _time.sleep(0.05)
        else:
            raise AssertionError("Record did not enter waiting_manual_publish")

        assert records[0]["publish_url"] is None
        task_detail = client.get(f"/api/tasks/{task['id']}").json()
        assert task_detail["status"] == "running"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_user_input_required_pauses_record(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    class NeedsUserInputPublisher:
        def __init__(self):
            self.ready = threading.Event()

        def __call__(self, article, account, *, stop_before_publish=False):
            self.ready.set()
            raise ToutiaoUserInputRequired("login verification required")

    try:
        publisher = NeedsUserInputPublisher()
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: publisher,
        )
        cover_id = _upload_cover_image(client)
        article_id = _create_article(
            client, "Article Needs Input", plain_text="Body", cover_asset_id=cover_id
        )
        account_id = _create_account(
            client, test_app.data_dir, "account-needs-input", "Needs Input"
        )
        task = client.post(
            "/api/tasks",
            json={
                "name": "user input task",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        resp = client.post(f"/api/tasks/{task['id']}/execute")
        assert resp.status_code == 202

        assert publisher.ready.wait(timeout=5.0), "Publisher never signalled ready"

        deadline = _time.time() + 3.0
        records = []
        while _time.time() < deadline:
            records = client.get(f"/api/tasks/{task['id']}/records").json()
            if records and records[0]["status"] == "waiting_user_input":
                break
            _time.sleep(0.05)
        else:
            raise AssertionError("Record did not enter waiting_user_input")

        assert "login verification required" in records[0]["error_message"]
        assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "running"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_resolve_user_input_requeues_and_continues(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    attempts = 0

    class LoginThenSuccessPublisher:
        def __call__(self, article, account, *, stop_before_publish=False):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ToutiaoUserInputRequired("scan login required")
            return PublishFillResult(
                url=f"https://example.com/article/{article.id}",
                title=article.title,
                message=f"published {article.id}",
            )

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: LoginThenSuccessPublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_id = _create_article(
            client, "Article Resume", plain_text="Body", cover_asset_id=cover_id
        )
        account_id = _create_account(client, test_app.data_dir, "account-resume", "Resume Account")
        task = client.post(
            "/api/tasks",
            json={
                "name": "resume user input",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        resp = client.post(f"/api/tasks/{task['id']}/execute")
        assert resp.status_code == 202

        deadline = _time.time() + 5.0
        record = None
        while _time.time() < deadline:
            record = client.get(f"/api/tasks/{task['id']}/records").json()[0]
            if record["status"] == "waiting_user_input":
                break
            _time.sleep(0.05)
        else:
            raise AssertionError("Record did not pause for user input")

        resume = client.post(f"/api/publish-records/{record['id']}/resolve-user-input")
        assert resume.status_code == 200
        assert resume.json()["status"] == "pending"

        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            task_detail = client.get(f"/api/tasks/{task['id']}").json()
            if task_detail["status"] == "succeeded":
                break
            _time.sleep(0.05)
        else:
            raise AssertionError("Task did not resume after user input")

        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records[0]["status"] == "succeeded"
        assert attempts == 2
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# 测试 2：manual_confirm_record() 传 outcome=succeeded
# ---------------------------------------------------------------------------
@pytest.mark.mysql
def test_manual_confirm_succeeded(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_id = _create_article(
            client, "Article A", plain_text="Body A", cover_asset_id=cover_id
        )
        account_id = _create_account(client, test_app.data_dir, "account-a", "Account A")
        task = client.post(
            "/api/tasks",
            json={
                "name": "manual confirm succ",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": True,
            },
        ).json()

        resp = client.post(f"/api/tasks/{task['id']}/execute")
        assert resp.status_code == 202

        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            records = client.get(f"/api/tasks/{task['id']}/records").json()
            if records[0]["status"] == "waiting_manual_publish":
                break
            _time.sleep(0.05)
        assert records[0]["status"] == "waiting_manual_publish"

        confirm = client.post(
            f"/api/publish-records/{records[0]['id']}/manual-confirm",
            json={"outcome": "succeeded", "publish_url": "https://example.com/article/1"},
        )
        assert confirm.status_code == 200
        assert confirm.json()["status"] == "succeeded"
        assert confirm.json()["publish_url"] == "https://example.com/article/1"
        assert confirm.json()["error_message"] is None

        task_detail = client.get(f"/api/tasks/{task['id']}").json()
        assert task_detail["status"] == "succeeded"
        assert task_detail["finished_at"] is not None
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# 测试 3：manual_confirm_record() 传 outcome=failed
# ---------------------------------------------------------------------------
@pytest.mark.mysql
def test_manual_confirm_failed(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_id = _create_article(
            client, "Article A", plain_text="Body A", cover_asset_id=cover_id
        )
        account_id = _create_account(client, test_app.data_dir, "account-a", "Account A")
        task = client.post(
            "/api/tasks",
            json={
                "name": "manual confirm fail",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": True,
            },
        ).json()

        resp = client.post(f"/api/tasks/{task['id']}/execute")
        assert resp.status_code == 202

        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            records = client.get(f"/api/tasks/{task['id']}/records").json()
            if records[0]["status"] == "waiting_manual_publish":
                break
            _time.sleep(0.05)
        assert records[0]["status"] == "waiting_manual_publish"

        confirm = client.post(
            f"/api/publish-records/{records[0]['id']}/manual-confirm",
            json={"outcome": "failed", "error_message": "Manual rejection"},
        )
        assert confirm.status_code == 200
        assert confirm.json()["status"] == "failed"
        assert confirm.json()["error_message"] == "Manual rejection"

        task_detail = client.get(f"/api/tasks/{task['id']}").json()
        assert task_detail["status"] == "failed"
        assert task_detail["finished_at"] is not None
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# 测试 4：manual_confirm_record() 不会因 Playwright 执行阻塞 HTTP 请求
#         （下一条记录不会同步执行）
# ---------------------------------------------------------------------------
@pytest.mark.mysql
def test_manual_confirm_does_not_block_with_next_record(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_1 = _create_article(
            client, "Article 1", plain_text="Body 1", cover_asset_id=cover_id
        )
        article_2 = _create_article(
            client, "Article 2", plain_text="Body 2", cover_asset_id=cover_id
        )
        account_id = _create_account(client, test_app.data_dir, "account-a", "Account A")

        group = client.post("/api/article-groups", json={"name": "Group"}).json()
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
                "name": "group stop before",
                "task_type": "group_round_robin",
                "group_id": group["id"],
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": True,
            },
        ).json()

        resp = client.post(f"/api/tasks/{task['id']}/execute")
        assert resp.status_code == 202

        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            records = client.get(f"/api/tasks/{task['id']}/records").json()
            if (
                len(records) == 2
                and records[0]["status"] == "waiting_manual_publish"
                and records[1]["status"] == "pending"
            ):
                break
            _time.sleep(0.05)
        assert records[0]["status"] == "waiting_manual_publish"
        assert records[1]["status"] == "pending"

        # 直接单测 manual_confirm_record 服务函数。
        from server.app.modules.tasks import manual_confirm_record

        db2 = test_app.session_factory()
        try:
            rec1 = db2.get(PublishRecord, records[0]["id"])
            # 将第 1 条记录恢复到等待态，供直接单测使用。
            rec1.status = "waiting_manual_publish"
            rec1.finished_at = None
            rec1.publish_url = None
            db2.commit()
            manual_confirm_record(db2, rec1, "succeeded", "https://example.com/ok", None)
            # 确认确实落到 record 1 上（旧版本只调用不断言，等于空跑）
            assert rec1.status == "succeeded"
            assert rec1.publish_url == "https://example.com/ok"
            assert rec1.finished_at is not None
            assert rec1.queue_reason is None
            # 非阻塞契约：确认 record 1 不应带动 record 2 同步执行，它仍 pending
            rec2 = db2.get(PublishRecord, records[1]["id"])
            assert rec2.status == "pending"
        finally:
            db2.close()
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# 测试 5：非 ToutiaoPublishError 异常会把记录标为 failed，
#         清空 lease_until，并写入 TaskLog
# ---------------------------------------------------------------------------
def test_unexpected_exception_marks_record_failed(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(error=ValueError("Something unexpected broke")),
        )
        cover_id = _upload_cover_image(client)
        article_id = _create_article(
            client, "Article A", plain_text="Article body", cover_asset_id=cover_id
        )
        account_id = _create_account(client, test_app.data_dir, "account-a", "Account A")
        task = client.post(
            "/api/tasks",
            json={
                "name": "unexpected error task",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        _execute_and_wait(client, task["id"])

        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records[0]["status"] == "failed"
        assert "Something unexpected broke" in records[0]["error_message"]
        assert records[0]["lease_until"] is None

        logs = client.get(f"/api/tasks/{task['id']}/logs").json()
        error_logs = [log for log in logs if log["level"] == "error"]
        assert len(error_logs) >= 1
        assert any("Something unexpected broke" in log["message"] for log in error_logs)

        task_detail = client.get(f"/api/tasks/{task['id']}").json()
        assert task_detail["status"] == "failed"
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# 测试 6：执行终态任务返回 409，不会入队
# ---------------------------------------------------------------------------
def test_execute_terminal_task_returns_409(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_id = _create_article(
            client, "Article A", plain_text="Body A", cover_asset_id=cover_id
        )
        account_id = _create_account(client, test_app.data_dir, "account-a", "Account A")
        task = client.post(
            "/api/tasks",
            json={
                "name": "terminal test",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        _execute_and_wait(client, task["id"])
        task_detail = client.get(f"/api/tasks/{task['id']}").json()
        assert task_detail["status"] == "succeeded"

        # 尝试执行已经成功的任务。
        resp = client.post(f"/api/tasks/{task['id']}/execute")
        assert resp.status_code == 409
        assert "terminal" in resp.json()["detail"].lower()
        assert "queued" not in resp.json()

        # 同时覆盖失败任务。
        task2 = client.post(
            "/api/tasks",
            json={
                "name": "fail then execute",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(error=ToutiaoPublishError("fail")),
        )
        _execute_and_wait(client, task2["id"])
        assert client.get(f"/api/tasks/{task2['id']}").json()["status"] == "failed"

        resp2 = client.post(f"/api/tasks/{task2['id']}/execute")
        assert resp2.status_code == 409
        assert "terminal" in resp2.json()["detail"].lower()

        # 覆盖已取消任务。
        task3 = client.post(
            "/api/tasks",
            json={
                "name": "cancel then execute",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()
        client.post(f"/api/tasks/{task3['id']}/cancel")
        assert client.get(f"/api/tasks/{task3['id']}").json()["status"] == "cancelled"

        resp3 = client.post(f"/api/tasks/{task3['id']}/execute")
        assert resp3.status_code == 409
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# 测试 7：同一任务并发执行时只有一条路径成功
# ---------------------------------------------------------------------------
def test_concurrent_execute_only_one_succeeds(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        from server.app.modules.tasks import executor as tasks_mod

        cover_id = _upload_cover_image(client)
        article_id = _create_article(
            client, "Concurrent Article", plain_text="Body", cover_asset_id=cover_id
        )
        account_id = _create_account(client, test_app.data_dir, "account-c", "Concurrent Account")
        task = client.post(
            "/api/tasks",
            json={
                "name": "concurrent execute",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        # 预先放入一把已持有的锁，模拟正在执行。
        fake_lock = threading.Lock()
        fake_lock.acquire()
        tasks_mod._task_locks[task["id"]] = fake_lock

        # 第一次执行：后台线程会因拿不到锁而失败。
        resp1 = client.post(f"/api/tasks/{task['id']}/execute")
        assert resp1.status_code == 202

        _time.sleep(0.3)

        # 任务应仍为 pending（锁被占用，执行未成功）。
        task_status = client.get(f"/api/tasks/{task['id']}").json()
        assert task_status["status"] == "pending"

        # 释放锁后再次执行，应能成功。
        fake_lock.release()
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: FakePublisher(),
        )

        resp2 = client.post(f"/api/tasks/{task['id']}/execute")
        assert resp2.status_code == 202

        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            task_detail = client.get(f"/api/tasks/{task['id']}").json()
            if task_detail["status"] not in ("pending", "running"):
                assert task_detail["status"] == "succeeded"
                break
            _time.sleep(0.05)
        else:
            raise AssertionError("Task did not complete within timeout")
    finally:
        test_app.cleanup()


def _create_group_task(
    client,
    name: str,
    article_ids: list[int],
    account_ids: list[int],
    *,
    stop_before_publish: bool = False,
) -> dict:
    group = client.post("/api/article-groups", json={"name": name}).json()
    client.put(
        f"/api/article-groups/{group['id']}/items",
        json={
            "items": [
                {"article_id": article_id, "sort_order": index}
                for index, article_id in enumerate(article_ids)
            ]
        },
    )
    return client.post(
        "/api/tasks",
        json={
            "name": name,
            "task_type": "group_round_robin",
            "group_id": group["id"],
            "accounts": [
                {"account_id": account_id, "sort_order": index}
                for index, account_id in enumerate(account_ids)
            ],
            "stop_before_publish": stop_before_publish,
        },
    ).json()


def test_group_task_runs_different_accounts_concurrently_with_cap(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    active = 0
    max_active = 0
    entered = 0
    cap = 5  # MAX_CONCURRENT_RECORDS
    lock = threading.Lock()
    # 确定性证明峰值并发能达到 `cap`：前 `cap` 条抢到槽位的记录会在屏障处会合，
    # 只有当这 `cap` 条同时在途时屏障才放行。旧的 `sleep(0.25)` 方案只是寄希望于
    # 线程自然重叠，在 CI 调度抖动下会不稳定。
    barrier = threading.Barrier(cap)

    class SlowPublisher:
        def __call__(self, article, account, *, stop_before_publish=False):
            nonlocal active, max_active, entered
            with lock:
                active += 1
                max_active = max(max_active, active)
                index = entered
                entered += 1
            if index < cap:
                # 如果并发上限真的回归（永远少于 `cap` 条并发），这里会超时，
                # max_active 保持小于 cap，最终断言失败。
                try:
                    barrier.wait(timeout=5)
                except threading.BrokenBarrierError:
                    pass
            with lock:
                active -= 1
            return PublishFillResult(
                url=f"https://example.com/article/{article.id}",
                title=article.title,
                message=f"published {article.id}",
            )

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: SlowPublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_ids = [
            _create_article(
                client, f"Article {index}", plain_text=f"Body {index}", cover_asset_id=cover_id
            )
            for index in range(6)
        ]
        account_ids = [
            _create_account(client, test_app.data_dir, f"account-{index}", f"Account {index}")
            for index in range(6)
        ]
        task = _create_group_task(client, "concurrent cap", article_ids, account_ids)

        task_detail = _execute_and_wait(client, task["id"], max_wait=10.0)

        assert task_detail["status"] == "succeeded"
        assert max_active == 5
        assert all(
            record["status"] == "succeeded"
            for record in client.get(f"/api/tasks/{task['id']}/records").json()
        )
    finally:
        test_app.cleanup()


def test_group_task_serializes_records_for_same_account(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    active_by_account: dict[int, int] = {}
    max_for_account: dict[int, int] = {}
    lock = threading.Lock()

    class SlowPublisher:
        def __call__(self, article, account, *, stop_before_publish=False):
            with lock:
                active_by_account[account.id] = active_by_account.get(account.id, 0) + 1
                max_for_account[account.id] = max(
                    max_for_account.get(account.id, 0), active_by_account[account.id]
                )
            _time.sleep(0.15)
            with lock:
                active_by_account[account.id] -= 1
            return PublishFillResult(
                url=f"https://example.com/article/{article.id}",
                title=article.title,
                message=f"published {article.id}",
            )

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: SlowPublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_ids = [
            _create_article(
                client,
                f"Serial Article {index}",
                plain_text=f"Body {index}",
                cover_asset_id=cover_id,
            )
            for index in range(3)
        ]
        account_id = _create_account(client, test_app.data_dir, "serial-account", "Serial Account")
        task = _create_group_task(client, "same account serial", article_ids, [account_id])

        task_detail = _execute_and_wait(client, task["id"], max_wait=10.0)

        assert task_detail["status"] == "succeeded"
        assert max_for_account == {account_id: 1}
    finally:
        test_app.cleanup()


def test_failed_record_does_not_block_next_record(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    class MixedPublisher:
        def __call__(self, article, account, *, stop_before_publish=False):
            if article.title == "fail first":
                raise ToutiaoPublishError("boom")
            return PublishFillResult(
                url=f"https://example.com/article/{article.id}",
                title=article.title,
                message=f"published {article.id}",
            )

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: MixedPublisher(),
        )
        cover_id = _upload_cover_image(client)
        first = _create_article(client, "fail first", plain_text="Body 1", cover_asset_id=cover_id)
        second = _create_article(
            client, "publish second", plain_text="Body 2", cover_asset_id=cover_id
        )
        account_id = _create_account(
            client, test_app.data_dir, "continue-account", "Continue Account"
        )
        task = _create_group_task(client, "continue after failure", [first, second], [account_id])

        task_detail = _execute_and_wait(client, task["id"], max_wait=10.0)
        records = client.get(f"/api/tasks/{task['id']}/records").json()

        assert task_detail["status"] == "partial_failed"
        assert [record["status"] for record in records] == ["failed", "succeeded"]
        assert records[0]["error_message"].startswith("boom")
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# 测试：执行与取消并发竞争不会留下损坏状态
# ---------------------------------------------------------------------------
class SlowFakePublisher:
    def __call__(self, article, account, *, stop_before_publish=False):
        _time.sleep(2)
        return PublishFillResult(
            url="https://mp.toutiao.com/article/race",
            title=article.title,
            message="ok",
        )


def test_execute_and_cancel_race_does_not_leave_corrupt_state(monkeypatch):
    """验证同时执行和取消不会导致状态损坏。"""
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            lambda record: SlowFakePublisher(),
        )
        cover_id = _upload_cover_image(client)
        article_id = _create_article(
            client, "Race Test", plain_text="body", cover_asset_id=cover_id
        )
        account_id = _create_account(client, test_app.data_dir, "race-acct", "Race Acct")
        task = client.post(
            "/api/tasks",
            json={
                "name": "race task",
                "task_type": "single",
                "article_id": article_id,
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        execute_resp = client.post(f"/api/tasks/{task['id']}/execute")
        assert execute_resp.status_code == 202
        deadline = _time.time() + 2.0
        while _time.time() < deadline:
            running_records = client.get(f"/api/tasks/{task['id']}/records").json()
            if running_records[0]["status"] == "running":
                break
            _time.sleep(0.05)
        assert client.get(f"/api/tasks/{task['id']}/records").json()[0]["status"] == "running"

        cancel_resp = client.post(f"/api/tasks/{task['id']}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["cancel_requested"] is True

        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            task_final = client.get(f"/api/tasks/{task['id']}").json()
            if task_final["status"] in ("cancelled", "failed", "partial_failed", "succeeded"):
                break
            _time.sleep(0.05)

        task_final = client.get(f"/api/tasks/{task['id']}").json()
        assert task_final["status"] == "cancelled"

        records = client.get(f"/api/tasks/{task['id']}/records").json()
        assert records[0]["status"] == "succeeded"
    finally:
        test_app.cleanup()
