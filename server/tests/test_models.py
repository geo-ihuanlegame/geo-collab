import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.db.base import Base
from server.app.modules.accounts.models import Account
from server.app.modules.articles.models import (
    Article,
    ArticleBodyAsset,
    ArticleGroup,
    ArticleGroupItem,
    Asset,
)
from server.app.modules.system.models import Platform, User
from server.app.modules.tasks.models import PublishRecord, PublishTask, PublishTaskAccount, TaskLog
from server.tests.utils import build_test_engine


@pytest.mark.mysql
def test_core_model_tables_are_declared():
    expected_tables = {
        "platforms",
        "accounts",
        "assets",
        "articles",
        "article_body_assets",
        "article_groups",
        "article_group_items",
        "publish_tasks",
        "publish_task_accounts",
        "publish_records",
        "task_logs",
        "users",
    }

    assert expected_tables.issubset(Base.metadata.tables.keys())


@pytest.mark.mysql
def test_core_model_relationships_round_trip_in_mysql():
    engine = build_test_engine()
    try:
        with Session(engine) as session:
            # 所有核心对象都用 user_id=1，需先建对应 User，否则 MySQL 外键约束失败
            owner = User(
                id=1, username="owner1", role="operator", is_active=True, must_change_password=False
            )
            owner.set_password("password1")
            session.add(owner)
            session.flush()

            platform = Platform(code="toutiao", name="Toutiao", base_url="https://mp.toutiao.com")
            cover = Asset(
                id="asset-cover",
                user_id=1,
                filename="cover.png",
                ext=".png",
                mime_type="image/png",
                size=100,
                sha256="a" * 64,
                storage_key="assets/2026/05/cover.png",
            )
            body_image = Asset(
                id="asset-body",
                user_id=1,
                filename="body.png",
                ext=".png",
                mime_type="image/png",
                size=120,
                sha256="b" * 64,
                storage_key="assets/2026/05/body.png",
            )
            article = Article(
                user_id=1,
                title="Test article",
                author="Geo",
                cover_asset=cover,
                content_json="{}",
                content_html="<p>Body</p>",
                plain_text="Body",
                word_count=2,
                status="ready",
                body_assets=[
                    ArticleBodyAsset(asset=body_image, position=0, editor_node_id="node-1")
                ],
            )
            group = ArticleGroup(
                user_id=1,
                name="Test group",
                items=[ArticleGroupItem(article=article, sort_order=1)],
            )
            account = Account(
                user_id=1,
                platform=platform,
                display_name="Test account",
                platform_user_id="toutiao-user",
                status="valid",
                state_path="browser_states/toutiao/1/storage_state.json",
                last_login_at=utcnow(),
            )
            task = PublishTask(
                user_id=1,
                name="Test task",
                task_type="single",
                status="pending",
                platform=platform,
                article=article,
                group=None,
                accounts=[PublishTaskAccount(account=account, sort_order=0)],
            )
            record = PublishRecord(
                task=task, article=article, platform=platform, account=account, status="pending"
            )
            task.records.append(record)
            task.logs.append(TaskLog(record=record, level="info", message="created"))

            session.add(group)
            session.add(task)
            session.commit()

            stored_task = session.query(PublishTask).one()
            assert stored_task.platform.code == "toutiao"
            assert stored_task.accounts[0].account.display_name == "Test account"
            assert stored_task.records[0].article.body_assets[0].position == 0
            assert stored_task.logs[0].message == "created"
    finally:
        engine.dispose()


@pytest.mark.mysql
def test_user_password_hashing_and_verification():
    engine = build_test_engine()
    try:
        with Session(engine) as session:
            user = User(username="testuser", role="operator")
            user.set_password("secret123")
            session.add(user)
            session.commit()

            stored = session.query(User).filter(User.username == "testuser").one()
            assert stored.username == "testuser"
            assert stored.role == "operator"
            assert stored.is_active is True
            assert stored.must_change_password is True
            assert stored.password_hash != "secret123"
            assert stored.check_password("secret123") is True
            assert stored.check_password("wrong") is False
    finally:
        engine.dispose()


@pytest.mark.mysql
def test_database_constraints_exist_in_mysql():
    engine = build_test_engine()
    try:
        inspector = inspect(engine)

        account_checks = {
            constraint["name"] for constraint in inspector.get_check_constraints("accounts")
        }
        article_checks = {
            constraint["name"] for constraint in inspector.get_check_constraints("articles")
        }
        task_checks = {
            constraint["name"] for constraint in inspector.get_check_constraints("publish_tasks")
        }
        record_checks = {
            constraint["name"] for constraint in inspector.get_check_constraints("publish_records")
        }

        assert "ck_accounts_status" in account_checks
        assert "ck_articles_status" in article_checks
        assert "ck_publish_tasks_task_type" in task_checks
        assert "ck_publish_tasks_status" in task_checks
        assert "ck_publish_records_status" in record_checks
    finally:
        engine.dispose()
