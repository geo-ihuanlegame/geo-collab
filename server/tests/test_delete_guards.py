from server.app.modules.accounts import get_or_create_platform
from server.app.modules.accounts.models import Account
from server.app.modules.articles.models import Article
from server.app.modules.tasks.models import PublishRecord, PublishTask, PublishTaskAccount, TaskLog
from server.tests.utils import build_test_app

ACTIVE_STATUSES = ["pending", "running", "waiting_manual_publish", "waiting_user_input"]


def _create_article(client, title: str = "Test Article") -> int:
    resp = client.post(
        "/api/articles",
        json={"title": title, "content_json": {"type": "doc", "content": []}},
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def _create_account(test_app, account_key: str, display_name: str = "Test Account") -> int:
    client = test_app.client
    state_dir = test_app.data_dir / "browser_states" / "toutiao" / account_key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    resp = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": display_name, "account_key": account_key, "use_browser": False},
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def _create_task_and_record(test_app, article_id: int, account_id: int, record_status: str) -> int:
    """Create PublishTask + PublishRecord with given status via session. Returns record_id."""
    db = test_app.session_factory()
    try:
        platform = get_or_create_platform(db, "toutiao", "头条号", "https://mp.toutiao.com")
        task = PublishTask(
            user_id=1,
            name="test-task",
            task_type="single",
            status="pending",
            platform_id=platform.id,
            article_id=article_id,
        )
        db.add(task)
        db.flush()
        db.add(PublishTaskAccount(task_id=task.id, account_id=account_id, sort_order=0))

        record = PublishRecord(
            task_id=task.id,
            article_id=article_id,
            platform_id=platform.id,
            account_id=account_id,
            status=record_status,
        )
        db.add(record)
        db.commit()
        return record.id
    finally:
        db.close()


class TestDeleteArticleGuard:
    def test_pending_record_blocks_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-pending", "Acc")
            _create_task_and_record(test_app, article_id, account_id, "pending")

            resp = client.delete(f"/api/articles/{article_id}")
            assert resp.status_code == 400
            assert "未完成发布记录" in resp.json()["detail"]
            assert "文章" in resp.json()["detail"]
        finally:
            test_app.cleanup()

    def test_running_record_blocks_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-running", "Acc")
            _create_task_and_record(test_app, article_id, account_id, "running")

            resp = client.delete(f"/api/articles/{article_id}")
            assert resp.status_code == 400
            assert "未完成发布记录" in resp.json()["detail"]
        finally:
            test_app.cleanup()

    def test_waiting_manual_publish_record_blocks_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-waiting", "Acc")
            _create_task_and_record(test_app, article_id, account_id, "waiting_manual_publish")

            resp = client.delete(f"/api/articles/{article_id}")
            assert resp.status_code == 400
            assert "未完成发布记录" in resp.json()["detail"]
        finally:
            test_app.cleanup()

    def test_no_active_records_allows_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            # No tasks/records at all

            resp = client.delete(f"/api/articles/{article_id}")
            assert resp.status_code == 204

            # Verify article is gone
            assert client.get(f"/api/articles/{article_id}").status_code == 404
            db = test_app.session_factory()
            try:
                deleted_article = db.get(Article, article_id)
                assert deleted_article is not None
                assert bool(deleted_article.is_deleted) is True
                assert deleted_article.deleted_at is not None
            finally:
                db.close()
        finally:
            test_app.cleanup()

    def test_succeeded_record_does_not_block_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-succeeded", "Acc")
            record_id = _create_task_and_record(test_app, article_id, account_id, "succeeded")

            resp = client.delete(f"/api/articles/{article_id}")
            assert resp.status_code == 204

            db = test_app.session_factory()
            try:
                remaining = db.get(PublishRecord, record_id)
                assert remaining is not None, (
                    "Historical succeeded records are retained after article soft delete"
                )
            finally:
                db.close()
        finally:
            test_app.cleanup()

    def test_failed_record_does_not_block_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-failed", "Acc")
            record_id = _create_task_and_record(test_app, article_id, account_id, "failed")

            resp = client.delete(f"/api/articles/{article_id}")
            assert resp.status_code == 204

            db = test_app.session_factory()
            try:
                remaining = db.get(PublishRecord, record_id)
                assert remaining is not None, (
                    "Historical failed records are retained after article soft delete"
                )
            finally:
                db.close()
        finally:
            test_app.cleanup()


class TestDeleteAccountGuard:
    def test_pending_record_blocks_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-pending", "Acc")
            _create_task_and_record(test_app, article_id, account_id, "pending")

            resp = client.delete(f"/api/accounts/{account_id}")
            assert resp.status_code == 400
            assert "未完成发布记录" in resp.json()["detail"]
            assert "账号" in resp.json()["detail"]
        finally:
            test_app.cleanup()

    def test_running_record_blocks_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-running", "Acc")
            _create_task_and_record(test_app, article_id, account_id, "running")

            resp = client.delete(f"/api/accounts/{account_id}")
            assert resp.status_code == 400
            assert "未完成发布记录" in resp.json()["detail"]
        finally:
            test_app.cleanup()

    def test_waiting_manual_publish_record_blocks_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-waiting", "Acc")
            _create_task_and_record(test_app, article_id, account_id, "waiting_manual_publish")

            resp = client.delete(f"/api/accounts/{account_id}")
            assert resp.status_code == 400
            assert "未完成发布记录" in resp.json()["detail"]
        finally:
            test_app.cleanup()

    def test_no_active_records_allows_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            account_id = _create_account(test_app, "acc-clean", "Clean Acc")
            # No tasks/records

            resp = client.delete(f"/api/accounts/{account_id}")
            assert resp.status_code == 204

            # There is no GET /api/accounts/{id} route; verify soft-deletion via the DB.
            db = test_app.session_factory()
            try:
                deleted_account = db.get(Account, account_id)
                assert deleted_account is not None
                assert bool(deleted_account.is_deleted) is True
                assert deleted_account.deleted_at is not None
            finally:
                db.close()
        finally:
            test_app.cleanup()

    def test_succeeded_record_does_not_block_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-succeeded", "Acc")
            record_id = _create_task_and_record(test_app, article_id, account_id, "succeeded")

            resp = client.delete(f"/api/accounts/{account_id}")
            assert resp.status_code == 204

            db = test_app.session_factory()
            try:
                remaining = db.get(PublishRecord, record_id)
                assert remaining is not None, (
                    "Historical succeeded records are retained after account soft delete"
                )
                assert bool(db.get(Account, account_id).is_deleted) is True
            finally:
                db.close()
        finally:
            test_app.cleanup()

    def test_failed_record_does_not_block_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-failed", "Acc")
            record_id = _create_task_and_record(test_app, article_id, account_id, "failed")

            resp = client.delete(f"/api/accounts/{account_id}")
            assert resp.status_code == 204

            db = test_app.session_factory()
            try:
                remaining = db.get(PublishRecord, record_id)
                assert remaining is not None, (
                    "Historical failed records are retained after account soft delete"
                )
            finally:
                db.close()
        finally:
            test_app.cleanup()

    def test_record_logs_are_retained_after_account_deletion(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            article_id = _create_article(client)
            account_id = _create_account(test_app, "acc-with-log", "Acc")
            record_id = _create_task_and_record(test_app, article_id, account_id, "succeeded")
            db = test_app.session_factory()
            try:
                record = db.get(PublishRecord, record_id)
                assert record is not None
                db.add(
                    TaskLog(
                        task_id=record.task_id, record_id=record_id, level="info", message="done"
                    )
                )
                db.commit()
            finally:
                db.close()

            resp = client.delete(f"/api/accounts/{account_id}")
            assert resp.status_code == 204

            db = test_app.session_factory()
            try:
                assert db.get(PublishRecord, record_id) is not None
                assert db.query(TaskLog).filter(TaskLog.record_id == record_id).count() == 1
                assert (
                    db.query(PublishTaskAccount)
                    .filter(PublishTaskAccount.account_id == account_id)
                    .count()
                    == 1
                )
            finally:
                db.close()
        finally:
            test_app.cleanup()
