from concurrent.futures import Future

import pytest

from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _seed_record(db, *, status="pending", username="op_cu"):
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


def test_commit_uncertain_marks_failure_kind(monkeypatch):
    """直接喂 _finish_record_future 一个抛 CommitUncertainError 的 future，断言落 failed+commit_uncertain。"""
    from server.app.modules.tasks.drivers.base import CommitUncertainError
    from server.app.modules.tasks.executor import _finish_record_future
    from server.app.modules.tasks.models import PublishRecord

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, rec = _seed_record(
                db, status="running"
            )  # _mark_record_failed 条件 UPDATE 要求 running
            db.commit()
            rid = rec.id

            fut: Future = Future()
            fut.set_exception(CommitUncertainError("提交后断网"))
            _finish_record_future(db, task, rid, fut)
            db.commit()

            refreshed = db.get(PublishRecord, rid)
            assert refreshed.status == "failed"
            assert refreshed.failure_kind == "commit_uncertain"
    finally:
        test_app.cleanup()


class _TimeoutFuture:
    """最小桩：.result() 抛 FutureTimeoutError，驱动 _finish_record_future 的超时分支。"""

    def result(self, *_args, **_kwargs):
        from concurrent.futures import TimeoutError as FutureTimeoutError

        raise FutureTimeoutError()


def test_finish_timeout_after_commit_marks_uncertain(monkeypatch):
    """C1 Layer 1：watchdog 超时分支若读到 commit_attempted_at 已置（发布线程独立 session 写入），
    落 failed 时必须打 failure_kind='commit_uncertain'，而非 None（否则逃逸两层守卫）。"""
    from server.app.modules.tasks import executor as ex
    from server.app.modules.tasks.executor import _finish_record_future
    from server.app.modules.tasks.models import PublishRecord

    monkeypatch.setattr(ex, "_stop_record_session", lambda _rid: None)

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            from server.app.core.time import utcnow

            task, rec = _seed_record(db, status="running")
            rec.commit_attempted_at = utcnow()  # 已跨提交点
            db.commit()
            rid = rec.id

            _finish_record_future(db, task, rid, _TimeoutFuture())
            db.commit()

            refreshed = db.get(PublishRecord, rid)
            assert refreshed.status == "failed"
            assert refreshed.failure_kind == "commit_uncertain"
    finally:
        test_app.cleanup()


def test_finish_timeout_without_commit_stays_unlabeled(monkeypatch):
    """对照：未跨提交点的超时仍是 failure_kind=None（可一键重试），不被误升级。"""
    from server.app.modules.tasks import executor as ex
    from server.app.modules.tasks.executor import _finish_record_future
    from server.app.modules.tasks.models import PublishRecord

    monkeypatch.setattr(ex, "_stop_record_session", lambda _rid: None)

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, rec = _seed_record(db, status="running")  # commit_attempted_at 留空
            db.commit()
            rid = rec.id

            _finish_record_future(db, task, rid, _TimeoutFuture())
            db.commit()

            refreshed = db.get(PublishRecord, rid)
            assert refreshed.status == "failed"
            assert refreshed.failure_kind is None
    finally:
        test_app.cleanup()
