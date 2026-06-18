"""reconcile_duplicate_into_canonical + 共享账号鉴权判定 的 MySQL 测试（build_test_app）。

覆盖设计稿 §4a / §6：加成员、干净 dup 软删、有历史 dup 标 merged_into、幂等重调、
owner / 已是成员不重复授予；user_can_use_account / user_can_manage_account。
"""

import pytest

from server.app.modules.accounts.models import Account, AccountMember
from server.app.modules.accounts.service import (
    reconcile_duplicate_into_canonical,
    user_can_manage_account,
    user_can_use_account,
)
from server.app.modules.audit.models import AuditLog
from server.app.modules.system.models import Platform, User
from server.app.modules.tasks.models import (
    PublishRecord,
    PublishTask,
    PublishTaskAccount,
)
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _make_platform(db) -> Platform:
    platform = Platform(
        code="toutiao", name="头条号", base_url="https://mp.toutiao.com", enabled=True
    )
    db.add(platform)
    db.flush()
    return platform


def _make_user(db, username: str, role: str = "operator") -> User:
    user = User(username=username, role=role, is_active=True, must_change_password=False)
    user.set_password("pw-123456")
    db.add(user)
    db.flush()
    return user


def _make_account(
    db, *, platform_id: int, user_id: int, platform_user_id: str | None, name: str
) -> Account:
    account = Account(
        user_id=user_id,
        platform_id=platform_id,
        display_name=name,
        platform_user_id=platform_user_id,
        status="valid",
        state_path=f"browser_states/toutiao/{name}/storage_state.json",
    )
    db.add(account)
    db.flush()
    return account


def _make_record_for(db, account: Account) -> PublishRecord:
    """给账号挂一条 PublishRecord（连带最小 article + task），代表「有历史」。"""
    from server.app.modules.articles.models import Article

    article = Article(user_id=account.user_id, title="t", status="ready")
    db.add(article)
    db.flush()
    task = PublishTask(
        user_id=account.user_id,
        name="task",
        task_type="single",
        platform_id=account.platform_id,
        article_id=article.id,
    )
    db.add(task)
    db.flush()
    record = PublishRecord(
        task_id=task.id,
        article_id=article.id,
        platform_id=account.platform_id,
        account_id=account.id,
        status="succeeded",
    )
    db.add(record)
    db.flush()
    return record


# ── reconcile：加成员 + 干净 dup 软删 ─────────────────────────────────────────


def test_reconcile_adds_member_and_softdeletes_clean_dup(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "owner1")
            other = _make_user(db, "other1")
            canonical = _make_account(
                db, platform_id=platform.id, user_id=owner.id, platform_user_id="111", name="c"
            )
            dup = _make_account(
                db, platform_id=platform.id, user_id=other.id, platform_user_id=None, name="d"
            )
            db.commit()
            canonical_id, dup_id, other_id = canonical.id, dup.id, other.id

            reconcile_duplicate_into_canonical(db, dup, canonical, granted_via="login_dedup")
            db.commit()

            member = (
                db.query(AccountMember)
                .filter(
                    AccountMember.account_id == canonical_id,
                    AccountMember.user_id == other_id,
                )
                .one_or_none()
            )
            assert member is not None
            assert member.granted_via == "login_dedup"

            refreshed_dup = db.get(Account, dup_id)
            assert refreshed_dup.is_deleted is True
            assert refreshed_dup.merged_into is None
            assert refreshed_dup.platform_user_id is None  # 槽位释放

            audits = db.query(AuditLog).filter(AuditLog.action == "account.dedup_merge").all()
            assert len(audits) == 1
    finally:
        test_app.cleanup()


# ── reconcile：有历史 dup → merged_into，不软删 ───────────────────────────────


def test_reconcile_dup_with_history_sets_merged_into(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "owner2")
            other = _make_user(db, "other2")
            canonical = _make_account(
                db, platform_id=platform.id, user_id=owner.id, platform_user_id="222", name="c2"
            )
            dup = _make_account(
                db, platform_id=platform.id, user_id=other.id, platform_user_id=None, name="d2"
            )
            _make_record_for(db, dup)
            db.commit()
            canonical_id, dup_id = canonical.id, dup.id

            reconcile_duplicate_into_canonical(db, dup, canonical, granted_via="backfill_merge")
            db.commit()

            refreshed_dup = db.get(Account, dup_id)
            assert refreshed_dup.is_deleted is False
            assert refreshed_dup.merged_into == canonical_id
    finally:
        test_app.cleanup()


def test_reconcile_dup_with_task_binding_sets_merged_into(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "owner3")
            other = _make_user(db, "other3")
            canonical = _make_account(
                db, platform_id=platform.id, user_id=owner.id, platform_user_id="333", name="c3"
            )
            dup = _make_account(
                db, platform_id=platform.id, user_id=other.id, platform_user_id=None, name="d3"
            )
            # 只有任务绑定、无 PublishRecord：仍算「有历史」→ merged_into
            from server.app.modules.articles.models import Article

            article = Article(user_id=other.id, title="t3", status="ready")
            db.add(article)
            db.flush()
            task = PublishTask(
                user_id=other.id,
                name="task3",
                task_type="single",
                platform_id=platform.id,
                article_id=article.id,
            )
            db.add(task)
            db.flush()
            db.add(PublishTaskAccount(task_id=task.id, account_id=dup.id, sort_order=0))
            db.commit()
            canonical_id, dup_id = canonical.id, dup.id

            reconcile_duplicate_into_canonical(db, dup, canonical, granted_via="login_dedup")
            db.commit()

            refreshed_dup = db.get(Account, dup_id)
            assert refreshed_dup.is_deleted is False
            assert refreshed_dup.merged_into == canonical_id
    finally:
        test_app.cleanup()


# ── reconcile：幂等重调是 no-op ───────────────────────────────────────────────


def test_reconcile_is_idempotent_on_recall(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "owner4")
            other = _make_user(db, "other4")
            canonical = _make_account(
                db, platform_id=platform.id, user_id=owner.id, platform_user_id="444", name="c4"
            )
            dup = _make_account(
                db, platform_id=platform.id, user_id=other.id, platform_user_id=None, name="d4"
            )
            db.commit()
            canonical_id, dup_id, other_id = canonical.id, dup.id, other.id

            reconcile_duplicate_into_canonical(db, dup, canonical, granted_via="login_dedup")
            db.commit()
            reconcile_duplicate_into_canonical(db, dup, canonical, granted_via="login_dedup")
            db.commit()
            reconcile_duplicate_into_canonical(db, dup, canonical, granted_via="login_dedup")
            db.commit()

            members = (
                db.query(AccountMember)
                .filter(
                    AccountMember.account_id == canonical_id,
                    AccountMember.user_id == other_id,
                )
                .all()
            )
            assert len(members) == 1  # 不重复加成员

            refreshed_dup = db.get(Account, dup_id)
            assert refreshed_dup.is_deleted is True

            # 幂等重调不追加审计行：3 次调用 → 恰好 1 条 account.dedup_merge 记录
            audits = (
                db.query(AuditLog)
                .filter(
                    AuditLog.action == "account.dedup_merge",
                    AuditLog.target_id == canonical_id,
                )
                .all()
            )
            assert len(audits) == 1
    finally:
        test_app.cleanup()


# ── reconcile：dup.user_id 已是 owner / 已是成员 → 不重复加成员 ────────────────


def test_reconcile_no_member_when_dup_user_is_owner(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "owner5")
            # 同一用户的两行（同一物理账号被同一用户重复登记）
            canonical = _make_account(
                db, platform_id=platform.id, user_id=owner.id, platform_user_id="555", name="c5"
            )
            dup = _make_account(
                db, platform_id=platform.id, user_id=owner.id, platform_user_id=None, name="d5"
            )
            db.commit()
            canonical_id = canonical.id

            reconcile_duplicate_into_canonical(db, dup, canonical, granted_via="login_dedup")
            db.commit()

            members = db.query(AccountMember).filter(AccountMember.account_id == canonical_id).all()
            assert members == []  # owner 不进成员表
    finally:
        test_app.cleanup()


def test_reconcile_no_duplicate_when_already_member(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "owner6")
            other = _make_user(db, "other6")
            canonical = _make_account(
                db, platform_id=platform.id, user_id=owner.id, platform_user_id="666", name="c6"
            )
            dup = _make_account(
                db, platform_id=platform.id, user_id=other.id, platform_user_id=None, name="d6"
            )
            # 预先把 other 加成员（manual）
            db.add(AccountMember(account_id=canonical.id, user_id=other.id, granted_via="manual"))
            db.commit()
            canonical_id, other_id = canonical.id, other.id

            reconcile_duplicate_into_canonical(db, dup, canonical, granted_via="login_dedup")
            db.commit()

            members = (
                db.query(AccountMember)
                .filter(
                    AccountMember.account_id == canonical_id,
                    AccountMember.user_id == other_id,
                )
                .all()
            )
            assert len(members) == 1
            assert members[0].granted_via == "manual"  # 既有授予来源不被覆盖
    finally:
        test_app.cleanup()


# ── 鉴权判定 ──────────────────────────────────────────────────────────────────


def test_user_can_use_and_manage_account(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "owner7")
            member = _make_user(db, "member7")
            stranger = _make_user(db, "stranger7")
            admin = _make_user(db, "admin7", role="admin")
            account = _make_account(
                db, platform_id=platform.id, user_id=owner.id, platform_user_id="777", name="c7"
            )
            db.add(
                AccountMember(account_id=account.id, user_id=member.id, granted_via="login_dedup")
            )
            db.commit()

            # use：owner / member / admin 可，stranger 不可
            assert user_can_use_account(db, account, owner) is True
            assert user_can_use_account(db, account, member) is True
            assert user_can_use_account(db, account, admin) is True
            assert user_can_use_account(db, account, stranger) is False

            # manage：owner / admin 可，member / stranger 不可
            assert user_can_manage_account(account, owner) is True
            assert user_can_manage_account(account, admin) is True
            assert user_can_manage_account(account, member) is False
            assert user_can_manage_account(account, stranger) is False
    finally:
        test_app.cleanup()
