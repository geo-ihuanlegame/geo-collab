"""creator-ID 抽取 + 查重决议引擎的 MySQL 测试（build_test_app，无浏览器）。

覆盖设计稿 §4（worker 登录决议）/ §5（检测回填）/ §9（导入）：
- worker 首登无 canonical → 写 X + resolved=B
- worker 首登有 canonical → reconcile + resolved=C + B 合并 + finish 返回 C
- 并发 IntegrityError 兜底（两个首登同一 X → 第二个撞约束转 reconcile）
- 检测 NULL→X、NULL+canonical→合并、present-!=X 有/无历史
- 导入忽略包内 platform_user_id（浏览器账号导入后 platform_user_id 为 NULL，不撞唯一约束）

抽取一律 monkeypatch driver 抽取器 / 收尾 impl 注入 X，绝不起真实浏览器 / worker。
"""

import pytest

from server.app.modules.accounts import auth as auth_mod
from server.app.modules.accounts.auth import (
    BrowserCheckResult,
    _worker_finish_login_session,
    check_account,
)
from server.app.modules.accounts.models import Account, AccountLoginSession, AccountMember
from server.app.modules.accounts.schemas import AccountCheckRequest
from server.app.modules.audit.models import AuditLog
from server.app.modules.system.models import Platform, User
from server.tests.test_accounts_api import install_fake_driver, write_storage_state
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


# ── helpers ──────────────────────────────────────────────────────────────────


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


def _make_account(db, *, platform_id, user_id, platform_user_id, name) -> Account:
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


def _make_login_session(db, account_id, *, sid="sess-fin", worker_id="w1") -> AccountLoginSession:
    sess = AccountLoginSession(
        id=sid,
        account_id=account_id,
        platform_code="toutiao",
        account_key="demo",
        channel="chromium",
        status="finishing",
        worker_id=worker_id,
        browser_session_id="bs-1",
    )
    db.add(sess)
    db.flush()
    return sess


def _make_record_for(db, account: Account):
    from server.app.modules.articles.models import Article
    from server.app.modules.tasks.models import PublishRecord, PublishTask

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


def _patch_finish_impl(monkeypatch, *, logged_in=True, extracted):
    """让 worker 的 _finish_login_browser_impl 返回注入的抽取结果，跳过真实浏览器。"""

    def fake_impl(platform_code, account_key, state_path, session_id):
        return BrowserCheckResult(
            logged_in=logged_in,
            url="https://mp.toutiao.com/profile_v4/personal/info",
            title="头条号",
            extracted_platform_user_id=extracted,
        )

    monkeypatch.setattr(auth_mod, "_finish_login_browser_impl", fake_impl)


# ── worker 登录决议：首登无 canonical → 写 X + resolved=B ─────────────────────


def test_worker_first_login_no_canonical_writes_x_and_resolves_self(monkeypatch):
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        _patch_finish_impl(monkeypatch, extracted="1234567890")
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "o1")
            b = _make_account(
                db, platform_id=platform.id, user_id=owner.id, platform_user_id=None, name="demo"
            )
            db.commit()
            b_id = b.id
            sess = _make_login_session(db, b_id)
            db.commit()
            sid = sess.id

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, sid)
            _worker_finish_login_session(db, req)

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, sid)
            assert req.status == "finished"
            assert req.extracted_platform_user_id == "1234567890"
            assert req.resolved_account_id == b_id
            b = db.get(Account, b_id)
            assert b.platform_user_id == "1234567890"  # B 升为 canonical
            assert b.status == "valid"
    finally:
        test_app.cleanup()


# ── worker 登录决议：首登有 canonical → reconcile + resolved=C + B 合并 ────────


def test_worker_first_login_with_canonical_reconciles_and_resolves_c(monkeypatch):
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        _patch_finish_impl(monkeypatch, extracted="2222222222")
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner_c = _make_user(db, "ownerC")
            owner_b = _make_user(db, "ownerB")
            c = _make_account(
                db,
                platform_id=platform.id,
                user_id=owner_c.id,
                platform_user_id="2222222222",
                name="canon",
            )
            b = _make_account(
                db, platform_id=platform.id, user_id=owner_b.id, platform_user_id=None, name="demo"
            )
            db.commit()
            c_id, b_id, owner_b_id = c.id, b.id, owner_b.id
            sess = _make_login_session(db, b_id)
            db.commit()
            sid = sess.id

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, sid)
            _worker_finish_login_session(db, req)

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, sid)
            assert req.status == "finished"
            assert req.resolved_account_id == c_id  # 决议指向 canonical C

            # B 干净（无历史）→ 软删 + 释放槽位
            b = db.get(Account, b_id)
            assert b.is_deleted is True
            assert b.platform_user_id is None

            # owner_b 加成员到 C
            member = (
                db.query(AccountMember)
                .filter(AccountMember.account_id == c_id, AccountMember.user_id == owner_b_id)
                .one_or_none()
            )
            assert member is not None
            assert member.granted_via == "login_dedup"

            audits = db.query(AuditLog).filter(AuditLog.action == "account.dedup_merge").all()
            assert len(audits) == 1
    finally:
        test_app.cleanup()


def test_worker_first_login_with_canonical_b_has_history_sets_merged_into(monkeypatch):
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        _patch_finish_impl(monkeypatch, extracted="3333333333")
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner_c = _make_user(db, "ownerC2")
            owner_b = _make_user(db, "ownerB2")
            c = _make_account(
                db,
                platform_id=platform.id,
                user_id=owner_c.id,
                platform_user_id="3333333333",
                name="canon2",
            )
            b = _make_account(
                db, platform_id=platform.id, user_id=owner_b.id, platform_user_id=None, name="demo"
            )
            _make_record_for(db, b)  # B 有历史
            db.commit()
            c_id, b_id = c.id, b.id
            sess = _make_login_session(db, b_id)
            db.commit()
            sid = sess.id

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, sid)
            _worker_finish_login_session(db, req)

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, sid)
            assert req.resolved_account_id == c_id
            b = db.get(Account, b_id)
            assert b.is_deleted is False  # 有历史不软删
            assert b.merged_into == c_id
    finally:
        test_app.cleanup()


# ── 并发 IntegrityError 兜底：两个首登同一 X → 第二个撞约束转 reconcile ────────


def test_worker_concurrent_first_login_integrity_fallback(monkeypatch):
    """模拟并发：B 决议时另一账号 A 已抢先 claim 同一 X 但尚未在 B 的 session 中可见。

    用「先建一个已持有 X 的 canonical，但让决议的初次 SELECT 看不到它」难以直接构造；
    改用判别性更强的方式：让 _select_active_canonical 第一次返回 None（模拟竞态窗口），
    claim 时 DB 唯一约束撞 IntegrityError，回退重查拿到真正的赢家 → reconcile。
    """
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        _patch_finish_impl(monkeypatch, extracted="4444444444")
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner_a = _make_user(db, "ownerA")
            owner_b = _make_user(db, "ownerB3")
            # A 已经是持有 X 的真实 canonical（DB 唯一约束的真实赢家）
            a = _make_account(
                db,
                platform_id=platform.id,
                user_id=owner_a.id,
                platform_user_id="4444444444",
                name="winnerA",
            )
            b = _make_account(
                db, platform_id=platform.id, user_id=owner_b.id, platform_user_id=None, name="demo"
            )
            db.commit()
            a_id, b_id, owner_b_id = a.id, b.id, owner_b.id
            sess = _make_login_session(db, b_id)
            db.commit()
            sid = sess.id

        # 让初次 canonical SELECT 返回 None（模拟竞态：赢家此刻不可见），
        # 强制走 claim 分支 → DB 唯一约束撞 IntegrityError → 回退重查（真实查询拿到 A）。
        real_select = auth_mod._select_active_canonical
        calls = {"n": 0}

        def flaky_select(db, account, platform_user_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # 第一次假装看不到赢家
            return real_select(db, account, platform_user_id)

        monkeypatch.setattr(auth_mod, "_select_active_canonical", flaky_select)

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, sid)
            _worker_finish_login_session(db, req)

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, sid)
            assert req.status == "finished"
            assert req.resolved_account_id == a_id  # 撞约束后回退到真正赢家 A
            b = db.get(Account, b_id)
            assert b.platform_user_id is None  # B 未抢到 X
            assert b.is_deleted is True  # B 干净 → 软删并入
            member = (
                db.query(AccountMember)
                .filter(AccountMember.account_id == a_id, AccountMember.user_id == owner_b_id)
                .one_or_none()
            )
            assert member is not None
    finally:
        test_app.cleanup()


# ── worker：X 为空 → 不查重，resolved=B，platform_user_id 不变 ─────────────────


def test_worker_empty_extracted_keeps_null_and_resolves_self(monkeypatch):
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        _patch_finish_impl(monkeypatch, extracted=None)
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "oEmpty")
            b = _make_account(
                db, platform_id=platform.id, user_id=owner.id, platform_user_id=None, name="demo"
            )
            db.commit()
            b_id = b.id
            sess = _make_login_session(db, b_id)
            db.commit()
            sid = sess.id

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, sid)
            _worker_finish_login_session(db, req)

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, sid)
            assert req.status == "finished"
            assert req.resolved_account_id == b_id
            assert db.get(Account, b_id).platform_user_id is None
    finally:
        test_app.cleanup()


# ── 检测路径（§5）：用 check_account + monkeypatch _check_account_in_browser ────


def _setup_detection(test_app, monkeypatch, *, logged_in=True, extracted):
    """让 check_account 跳过真实浏览器 + 锁，直接返回注入的 (logged_in, extracted)。"""
    monkeypatch.setattr(
        auth_mod,
        "_run_in_plain_thread",
        lambda fn: (logged_in, extracted),
    )
    # 跳过 profile 锁（无 Xvfb 环境）
    monkeypatch.setattr(
        "server.app.modules.accounts.browser.try_acquire_profile_lock",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "server.app.modules.accounts.browser.release_profile_lock",
        lambda *a, **k: None,
    )


def test_detection_null_backfills_x(monkeypatch):
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        _setup_detection(test_app, monkeypatch, extracted="5555555555")
        write_storage_state(test_app.data_dir, "det1")
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "oDet1")
            acc = Account(
                user_id=owner.id,
                platform_id=platform.id,
                display_name="det1",
                platform_user_id=None,
                status="valid",
                state_path="browser_states/toutiao/det1/storage_state.json",
            )
            db.add(acc)
            db.commit()
            acc_id = acc.id

        with test_app.session_factory() as db:
            acc = db.get(Account, acc_id)
            result = check_account(db, acc, AccountCheckRequest(use_browser=True))
            assert result.id == acc_id

        with test_app.session_factory() as db:
            assert db.get(Account, acc_id).platform_user_id == "5555555555"
    finally:
        test_app.cleanup()


def test_detection_null_with_canonical_merges(monkeypatch):
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        _setup_detection(test_app, monkeypatch, extracted="6666666666")
        write_storage_state(test_app.data_dir, "det2")
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner_c = _make_user(db, "oDetC")
            owner_self = _make_user(db, "oDetSelf")
            c = _make_account(
                db,
                platform_id=platform.id,
                user_id=owner_c.id,
                platform_user_id="6666666666",
                name="detcanon",
            )
            acc = Account(
                user_id=owner_self.id,
                platform_id=platform.id,
                display_name="det2",
                platform_user_id=None,
                status="valid",
                state_path="browser_states/toutiao/det2/storage_state.json",
            )
            db.add(acc)
            db.commit()
            c_id, acc_id, owner_self_id = c.id, acc.id, owner_self.id

        with test_app.session_factory() as db:
            acc = db.get(Account, acc_id)
            result = check_account(db, acc, AccountCheckRequest(use_browser=True))
            assert result.id == c_id  # 返回 canonical

        with test_app.session_factory() as db:
            acc = db.get(Account, acc_id)
            assert acc.is_deleted is True  # 无历史 → 软删
            member = (
                db.query(AccountMember)
                .filter(AccountMember.account_id == c_id, AccountMember.user_id == owner_self_id)
                .one_or_none()
            )
            assert member is not None
            assert member.granted_via == "backfill_merge"
    finally:
        test_app.cleanup()


def test_detection_present_neq_x_with_history_unchanged(monkeypatch):
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        _setup_detection(test_app, monkeypatch, extracted="7777777777")
        write_storage_state(test_app.data_dir, "det3")
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "oDet3")
            acc = Account(
                user_id=owner.id,
                platform_id=platform.id,
                display_name="det3",
                platform_user_id="9999999999",  # 已有不同身份
                status="valid",
                state_path="browser_states/toutiao/det3/storage_state.json",
            )
            db.add(acc)
            db.flush()
            _make_record_for(db, acc)  # 有历史
            db.commit()
            acc_id = acc.id

        with test_app.session_factory() as db:
            acc = db.get(Account, acc_id)
            result = check_account(db, acc, AccountCheckRequest(use_browser=True))
            assert result.id == acc_id  # 不改身份、仍是自己

        with test_app.session_factory() as db:
            # 身份冲突有历史 → 不自动改写，platform_user_id 保持原值
            assert db.get(Account, acc_id).platform_user_id == "9999999999"
    finally:
        test_app.cleanup()


def test_detection_present_neq_x_without_history_reidentifies(monkeypatch):
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        _setup_detection(test_app, monkeypatch, extracted="8888888888")
        write_storage_state(test_app.data_dir, "det4")
        with test_app.session_factory() as db:
            platform = _make_platform(db)
            owner = _make_user(db, "oDet4")
            acc = Account(
                user_id=owner.id,
                platform_id=platform.id,
                display_name="det4",
                platform_user_id="1010101010",  # 旧身份、但无历史/成员/任务
                status="valid",
                state_path="browser_states/toutiao/det4/storage_state.json",
            )
            db.add(acc)
            db.commit()
            acc_id = acc.id

        with test_app.session_factory() as db:
            acc = db.get(Account, acc_id)
            result = check_account(db, acc, AccountCheckRequest(use_browser=True))
            assert result.id == acc_id

        with test_app.session_factory() as db:
            # 干净行 → 重新识别为新 X
            assert db.get(Account, acc_id).platform_user_id == "8888888888"
    finally:
        test_app.cleanup()


# ── 导入（§9）：浏览器账号导入忽略包内 platform_user_id ────────────────────────


def test_import_ignores_package_platform_user_id(monkeypatch):
    """两个用户导入「同一个带 platform_user_id 的浏览器账号包」不应撞唯一约束：

    导入一律把 platform_user_id 置 NULL，故第二次导入也不会与第一次的行冲突。
    """
    from server.app.modules.accounts.auth import import_accounts_auth_package

    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        with test_app.session_factory() as db:
            _make_platform(db)
            u1 = _make_user(db, "imp1")
            u2 = _make_user(db, "imp2")
            db.commit()
            u1_id, u2_id = u1.id, u2.id

        zip_bytes = _build_auth_zip(platform_user_id="1212121212")

        with test_app.session_factory() as db:
            r1 = import_accounts_auth_package(db, u1_id, zip_bytes)
            db.commit()
            assert "导入测试账号" in r1["imported"]

        # 第二个用户导入同一个包：不应撞 uq_accounts_platform_user
        with test_app.session_factory() as db:
            r2 = import_accounts_auth_package(db, u2_id, zip_bytes)
            db.commit()
            assert "导入测试账号" in r2["imported"]

        with test_app.session_factory() as db:
            accounts = db.query(Account).filter(Account.display_name == "导入测试账号").all()
            assert len(accounts) == 2
            assert all(a.platform_user_id is None for a in accounts)  # 包内 ID 被忽略
    finally:
        test_app.cleanup()


def _build_auth_zip(*, platform_user_id: str) -> bytes:
    import io
    import json
    import zipfile

    manifest = {
        "schema_version": 1,
        "accounts": [
            {
                "id": 999,
                "platform_code": "toutiao",
                "platform_name": "头条号",
                "platform_base_url": "https://mp.toutiao.com",
                "display_name": "导入测试账号",
                "platform_user_id": platform_user_id,
                "status": "valid",
                "state_path": "browser_states/toutiao/imported/storage_state.json",
                "last_checked_at": None,
                "last_login_at": None,
                "note": None,
            }
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(
            "accounts/toutiao-999/storage_state.json",
            '{"cookies":[{"name":"x","value":"y","expires":-1}],"origins":[]}',
        )
    return buf.getvalue()
