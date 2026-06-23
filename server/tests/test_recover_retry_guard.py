from datetime import timedelta

import pytest

from server.app.shared.errors import ClientError
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _seed_record(db, *, status="pending", username="op_rrg"):
    """最小造一条 PublishRecord（连带 user/platform/account/article/task），返回 (task, record)。"""
    from server.app.modules.accounts.models import Account
    from server.app.modules.articles.models import Article
    from server.app.modules.system.models import Platform, User
    from server.app.modules.tasks.models import PublishRecord, PublishTask

    user = User(username=username, role="operator", is_active=True, must_change_password=False)
    user.set_password("pw-123456")
    db.add(user)
    db.flush()
    platform = Platform(
        code="toutiao", name="头条号", base_url="https://mp.toutiao.com", enabled=True
    )
    db.add(platform)
    db.flush()
    account = Account(
        user_id=user.id,
        platform_id=platform.id,
        display_name="acc",
        platform_user_id=None,
        status="valid",
        state_path="browser_states/toutiao/acc/storage_state.json",
    )
    db.add(account)
    db.flush()
    article = Article(user_id=user.id, title="t", status="ready")
    db.add(article)
    db.flush()
    task = PublishTask(
        user_id=user.id,
        name="task",
        task_type="single",
        platform_id=platform.id,
        article_id=article.id,
    )
    db.add(task)
    db.flush()
    record = PublishRecord(
        task_id=task.id,
        article_id=article.id,
        platform_id=platform.id,
        account_id=account.id,
        status=status,
    )
    db.add(record)
    db.flush()
    return task, record


def test_recover_does_not_repend_commit_uncertain(monkeypatch):
    from server.app.core.time import utcnow
    from server.app.modules.tasks.service import recover_stuck_records

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            _task, rec = _seed_record(db, status="running")
            rec.lease_until = utcnow() - timedelta(minutes=5)
            rec.commit_attempted_at = utcnow() - timedelta(minutes=4)  # 已跨提交点
            db.commit()
            recover_stuck_records(db)
            db.refresh(rec)
            assert rec.status == "failed"
            assert rec.failure_kind == "commit_uncertain"  # 不回 pending
    finally:
        test_app.cleanup()


def test_retry_blocks_commit_uncertain_without_force(monkeypatch):
    from server.app.modules.tasks.service import retry_record

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            _task, rec = _seed_record(db, status="failed")
            rec.failure_kind = "commit_uncertain"
            db.commit()
            with pytest.raises(ClientError):
                retry_record(db, rec)  # force 默认 False → 拦截
            # force=True 放行（不抛）
            new_rec = retry_record(db, rec, force=True)
            assert new_rec.retry_of_record_id == rec.id
    finally:
        test_app.cleanup()


def test_retry_blocks_commit_attempted_even_without_failure_kind(monkeypatch):
    """C1 Layer-2 兜底：watchdog 超时漏标 failure_kind，但已跨提交点（commit_attempted_at
    非空）的记录仍必须被默认拦截，避免一键重发导致重复发布；force=True 放行。"""
    from server.app.core.time import utcnow
    from server.app.modules.tasks.service import retry_record

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            _task, rec = _seed_record(db, status="failed")
            rec.failure_kind = None  # watchdog 漏标
            rec.commit_attempted_at = utcnow()  # 但已跨提交点
            db.commit()
            with pytest.raises(ClientError):
                retry_record(db, rec)  # force 默认 False → 拦截
            new_rec = retry_record(db, rec, force=True)
            assert new_rec.retry_of_record_id == rec.id
    finally:
        test_app.cleanup()
