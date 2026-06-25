"""headless 发布撞 UserInputRequired 的处置：标 failed + 置账号 expired，不进 waiting_user_input。"""

from concurrent.futures import Future

import pytest

from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _seed_record(db, *, status="running", username="op_headless"):
    from server.app.modules.accounts.models import Account
    from server.app.modules.articles.models import Article
    from server.app.modules.system.models import Platform, User
    from server.app.modules.tasks.models import PublishRecord, PublishTask

    user = User(username=username, role="operator", is_active=True, must_change_password=False)
    user.set_password("pw-123456")
    db.add(user)
    db.flush()
    platform = Platform(code="toutiao", name="头条号", base_url="https://mp.toutiao.com", enabled=True)
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
        user_id=user.id, name="task", task_type="single",
        platform_id=platform.id, article_id=article.id,
    )
    db.add(task)
    db.flush()
    record = PublishRecord(
        task_id=task.id, article_id=article.id, platform_id=platform.id,
        account_id=account.id, status=status,
    )
    db.add(record)
    db.flush()
    return task, record, account.id


def test_headless_login_required_marks_failed_and_expires_account(monkeypatch):
    from server.app.core.config import get_settings
    from server.app.modules.accounts.models import Account
    from server.app.modules.tasks import executor as ex
    from server.app.modules.tasks.drivers.toutiao import ToutiaoUserInputRequired
    from server.app.modules.tasks.executor import _finish_record_future
    from server.app.modules.tasks.models import PublishRecord

    monkeypatch.setattr(ex, "_stop_record_session", lambda _rid: None)
    monkeypatch.setenv("GEO_PUBLISH_BROWSER_HEADLESS", "true")
    get_settings.cache_clear()

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, rec, account_id = _seed_record(db, status="running")
            db.commit()
            rid = rec.id

            fut: Future = Future()
            fut.set_exception(ToutiaoUserInputRequired("登录态失效", error_type="login_required"))
            _finish_record_future(db, task, rid, fut)
            db.commit()

            refreshed = db.get(PublishRecord, rid)
            assert refreshed.status == "failed"
            assert refreshed.failure_kind == "login_required"
            assert db.get(Account, account_id).status == "expired"
    finally:
        test_app.cleanup()
        get_settings.cache_clear()


def test_headless_captcha_marks_failed_but_keeps_account_status(monkeypatch):
    """captcha 可能是瞬时挑战：标 failed，但不连坐账号 status。"""
    from server.app.core.config import get_settings
    from server.app.modules.accounts.models import Account
    from server.app.modules.tasks import executor as ex
    from server.app.modules.tasks.drivers.toutiao import ToutiaoUserInputRequired
    from server.app.modules.tasks.executor import _finish_record_future
    from server.app.modules.tasks.models import PublishRecord

    monkeypatch.setattr(ex, "_stop_record_session", lambda _rid: None)
    monkeypatch.setenv("GEO_PUBLISH_BROWSER_HEADLESS", "true")
    get_settings.cache_clear()

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, rec, account_id = _seed_record(db, status="running")
            db.commit()
            rid = rec.id

            fut: Future = Future()
            fut.set_exception(ToutiaoUserInputRequired("需要验证码", error_type="captcha_required"))
            _finish_record_future(db, task, rid, fut)
            db.commit()

            assert db.get(PublishRecord, rid).status == "failed"
            assert db.get(Account, account_id).status == "valid"
    finally:
        test_app.cleanup()
        get_settings.cache_clear()


def test_headed_user_input_still_waits(monkeypatch):
    """回退保险：headed 模式下 UserInputRequired 仍进 waiting_user_input。"""
    from server.app.core.config import get_settings
    from server.app.modules.tasks import executor as ex
    from server.app.modules.tasks.drivers.toutiao import ToutiaoUserInputRequired
    from server.app.modules.tasks.executor import _finish_record_future
    from server.app.modules.tasks.models import PublishRecord

    monkeypatch.setattr(ex, "_stop_record_session", lambda _rid: None)
    monkeypatch.setenv("GEO_PUBLISH_BROWSER_HEADLESS", "false")
    get_settings.cache_clear()

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, rec, _account_id = _seed_record(db, status="running")
            db.commit()
            rid = rec.id

            fut: Future = Future()
            fut.set_exception(ToutiaoUserInputRequired("需要扫码", error_type="qr_scan_required"))
            _finish_record_future(db, task, rid, fut)
            db.commit()

            assert db.get(PublishRecord, rid).status == "waiting_user_input"
    finally:
        test_app.cleanup()
        get_settings.cache_clear()
