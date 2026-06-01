from __future__ import annotations

from server.app.modules.accounts.models import Account
from server.app.modules.system.models import Platform
from server.app.modules.tasks.models import PublishRecord, PublishTask, PublishTaskAccount
from server.tests.utils import build_test_app


def _tiptap_doc() -> dict:
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]}


def _setup_platform_and_account(session) -> tuple[int, int]:
    platform = Platform(code="toutiao", name="头条号", base_url="https://mp.toutiao.com")
    account = Account(
        user_id=1,
        platform=platform,
        display_name="测试账号",
        platform_user_id="test-user",
        status="valid",
        state_path="browser_states/toutiao/test/storage_state.json",
    )
    session.add(platform)
    session.add(account)
    session.flush()
    return platform.id, account.id


def _create_article(client) -> dict:
    resp = client.post(
        "/api/articles",
        json={
            "title": "统计测试文章",
            "author": "CountTest",
            "content_json": _tiptap_doc(),
        },
    )
    assert resp.status_code == 200
    return resp.json()


def _create_task_with_record(session, article_id: int, platform_id: int, account_id: int, record_status: str) -> None:
    task = PublishTask(
        user_id=1,
        name="统计任务",
        task_type="single",
        status="succeeded",
        platform_id=platform_id,
        article_id=article_id,
        stop_before_publish=False,
    )
    task.accounts.append(PublishTaskAccount(account_id=account_id, sort_order=0))
    record = PublishRecord(
        task=task,
        article_id=article_id,
        platform_id=platform_id,
        account_id=account_id,
        status=record_status,
    )
    task.records.append(record)
    session.add(task)
    session.flush()


def test_published_count_only_counts_succeeded(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        session = test_app.session_factory()
        try:
            pid, aid = _setup_platform_and_account(session)
            _create_task_with_record(session, article_id, pid, aid, "succeeded")
            _create_task_with_record(session, article_id, pid, aid, "succeeded")
            session.commit()
        finally:
            session.close()

        resp = client.get("/api/articles")
        assert resp.status_code == 200
        results = resp.json()
        match = next(item for item in results if item["id"] == article_id)
        assert match["published_count"] == 2
    finally:
        test_app.cleanup()


def test_failed_records_not_counted(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        session = test_app.session_factory()
        try:
            pid, aid = _setup_platform_and_account(session)
            _create_task_with_record(session, article_id, pid, aid, "succeeded")
            _create_task_with_record(session, article_id, pid, aid, "failed")
            _create_task_with_record(session, article_id, pid, aid, "failed")
            session.commit()
        finally:
            session.close()

        resp = client.get("/api/articles")
        assert resp.status_code == 200
        results = resp.json()
        match = next(item for item in results if item["id"] == article_id)
        assert match["published_count"] == 1
    finally:
        test_app.cleanup()


def test_cancelled_records_not_counted(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        session = test_app.session_factory()
        try:
            pid, aid = _setup_platform_and_account(session)
            _create_task_with_record(session, article_id, pid, aid, "succeeded")
            _create_task_with_record(session, article_id, pid, aid, "cancelled")
            _create_task_with_record(session, article_id, pid, aid, "succeeded")
            session.commit()
        finally:
            session.close()

        resp = client.get("/api/articles")
        assert resp.status_code == 200
        results = resp.json()
        match = next(item for item in results if item["id"] == article_id)
        assert match["published_count"] == 2
    finally:
        test_app.cleanup()


def test_pending_records_not_counted(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        session = test_app.session_factory()
        try:
            pid, aid = _setup_platform_and_account(session)
            _create_task_with_record(session, article_id, pid, aid, "succeeded")
            _create_task_with_record(session, article_id, pid, aid, "pending")
            _create_task_with_record(session, article_id, pid, aid, "running")
            _create_task_with_record(session, article_id, pid, aid, "waiting_manual_publish")
            session.commit()
        finally:
            session.close()

        resp = client.get("/api/articles")
        assert resp.status_code == 200
        results = resp.json()
        match = next(item for item in results if item["id"] == article_id)
        assert match["published_count"] == 1
    finally:
        test_app.cleanup()
