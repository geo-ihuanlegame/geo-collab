import pytest

from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _seed_record(db, *, status="pending", username="op"):
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


def test_commit_marker_columns_roundtrip(monkeypatch):
    from server.app.core.time import utcnow
    from server.app.modules.tasks.models import PublishRecord

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            _task, rec = _seed_record(db)
            rec.commit_attempted_at = utcnow()
            rec.failure_kind = "commit_uncertain"
            record_id = rec.id
            db.commit()
            db.expunge_all()  # 清空 session 内存,强制下次从 DB 读
            reloaded = db.get(PublishRecord, record_id)
            assert reloaded.failure_kind == "commit_uncertain"
            assert reloaded.commit_attempted_at is not None
            # 双保险:确认两列是真实映射列,而非实例属性
            assert "commit_attempted_at" in PublishRecord.__table__.columns.keys()
            assert "failure_kind" in PublishRecord.__table__.columns.keys()
    finally:
        test_app.cleanup()
