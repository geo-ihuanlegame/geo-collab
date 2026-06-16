"""登录会话状态轮询 + 取消/中止链路测试。

此前裸奔的取消侧路径（与近期 DB 连接池/长连接事故相关）：
- GET  /{account_id}/login-session/{id}/status   前端轮询端点
- DELETE /{account_id}/login-session/{id}         中止端点
- auth.stop_account_login_session                 中止入口（找命令行 → 交给 worker 取消）
- auth._cancel_login_browser_via_worker           取消状态机（pending 直接 cancelled / active 请求取消并等 worker）
- auth._worker_cancel_login_session               worker 侧收尾（停浏览器 → cancelled → 回滚账号状态 → 释放锁）

这些测试用 use_browser=False + 手插 AccountLoginSession 行的方式覆盖，不起真实浏览器/worker。
"""

from server.tests.test_accounts_api import install_fake_driver, write_storage_state
from server.tests.utils import build_test_app


def _make_account(test_app, key="demo", display="login-cancel") -> dict:
    write_storage_state(test_app.data_dir, key)
    return test_app.client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": display, "account_key": key, "use_browser": False},
    ).json()


def _insert_session(test_app, account_id, *, sid, status, **extra):
    from server.app.modules.accounts.models import AccountLoginSession

    with test_app.session_factory() as db:
        sess = AccountLoginSession(
            id=sid,
            account_id=account_id,
            platform_code="toutiao",
            account_key="demo",
            channel="chromium",
            status=status,
            **extra,
        )
        db.add(sess)
        db.commit()
    return sid


# ── 状态轮询端点 ─────────────────────────────────────────────────────────────


def test_status_endpoint_returns_session_fields(monkeypatch):
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        account = _make_account(test_app)
        _insert_session(
            test_app,
            account["id"],
            sid="sess-poll-01",
            status="queued",
            queue_reason="账号正在被其它登录占用",
            novnc_url="http://127.0.0.1:6080/vnc.html",
        )
        resp = test_app.client.get(
            f"/api/accounts/{account['id']}/login-session/sess-poll-01/status"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["queue_reason"] == "账号正在被其它登录占用"
        assert body["novnc_url"] == "http://127.0.0.1:6080/vnc.html"
    finally:
        test_app.cleanup()


def test_status_endpoint_404_for_unknown_session(monkeypatch):
    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        account = _make_account(test_app)
        resp = test_app.client.get(
            f"/api/accounts/{account['id']}/login-session/does-not-exist/status"
        )
        assert resp.status_code == 404
    finally:
        test_app.cleanup()


# ── 取消状态机：_cancel_login_browser_via_worker ─────────────────────────────


def test_cancel_pending_session_goes_straight_to_cancelled(monkeypatch):
    """pending/queued 会话还没起浏览器，取消直接置 cancelled、清 queue_reason，无需等 worker。"""
    from server.app.modules.accounts.auth import _cancel_login_browser_via_worker
    from server.app.modules.accounts.models import AccountLoginSession

    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        account = _make_account(test_app)
        _insert_session(
            test_app, account["id"], sid="sess-pend", status="pending", queue_reason="排队中"
        )
        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, "sess-pend")
            _cancel_login_browser_via_worker(db, req)

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, "sess-pend")
            assert req.status == "cancelled"
            assert req.queue_reason is None
    finally:
        test_app.cleanup()


def test_cancel_terminal_session_is_noop(monkeypatch):
    """已终态（finished）的会话不被取消改写。"""
    from server.app.modules.accounts.auth import _cancel_login_browser_via_worker
    from server.app.modules.accounts.models import AccountLoginSession

    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        account = _make_account(test_app)
        _insert_session(test_app, account["id"], sid="sess-fin", status="finished")
        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, "sess-fin")
            _cancel_login_browser_via_worker(db, req)

        with test_app.session_factory() as db:
            assert db.get(AccountLoginSession, "sess-fin").status == "finished"
    finally:
        test_app.cleanup()


def test_cancel_active_session_requests_cancel_then_waits_for_worker(monkeypatch):
    """active 会话浏览器已起：取消先置 cancel_requested + commit，再委托给 worker 等待。

    判别性：monkeypatch 掉 worker 等待，捕获等待发起时的状态——若实现没先把状态推进到
    cancel_requested 就去等，worker 永远收不到取消信号。
    """
    from server.app.modules.accounts import auth as auth_mod
    from server.app.modules.accounts.auth import _cancel_login_browser_via_worker
    from server.app.modules.accounts.models import AccountLoginSession

    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        account = _make_account(test_app)
        _insert_session(test_app, account["id"], sid="sess-act", status="active", worker_id="w1")

        captured = {}

        def fake_wait(db, request_id, desired_statuses, timeout_seconds, timeout_message):
            row = db.get(AccountLoginSession, request_id)
            captured["status_at_wait"] = row.status
            captured["desired"] = set(desired_statuses)
            return row

        monkeypatch.setattr(auth_mod, "_wait_for_account_login_request", fake_wait)

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, "sess-act")
            _cancel_login_browser_via_worker(db, req)

        assert captured["status_at_wait"] == "cancel_requested"
        assert captured["desired"] == {"cancelled"}
        # cancel_requested 已落库（worker 进程靠它认领取消命令）
        with test_app.session_factory() as db:
            assert db.get(AccountLoginSession, "sess-act").status == "cancel_requested"
    finally:
        test_app.cleanup()


# ── DELETE 端点：端到端中止 ──────────────────────────────────────────────────


def test_delete_endpoint_aborts_pending_session_and_audits(monkeypatch):
    """DELETE 中止 pending 会话 → 204，会话置 cancelled，并落 account.login_session.abort 审计。"""
    from server.app.modules.accounts.models import AccountLoginSession
    from server.app.modules.audit.models import AuditLog

    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        account = _make_account(test_app)
        _insert_session(test_app, account["id"], sid="sess-del", status="pending")
        resp = test_app.client.delete(f"/api/accounts/{account['id']}/login-session/sess-del")
        assert resp.status_code == 204, resp.text

        with test_app.session_factory() as db:
            assert db.get(AccountLoginSession, "sess-del").status == "cancelled"
            audit = (
                db.query(AuditLog).filter(AuditLog.action == "account.login_session.abort").first()
            )
            assert audit is not None
    finally:
        test_app.cleanup()


# ── worker 侧收尾：_worker_cancel_login_session ──────────────────────────────


def test_worker_cancel_rolls_back_account_status_and_releases_lock(monkeypatch):
    """worker 取消 active 会话：置 cancelled、把账号 status 从 unknown 回滚到 previous_status、释放 login 锁。

    browser_session_id 留空以跳过真实停浏览器（无 Xvfb 环境）。
    """
    from server.app.modules.accounts import browser
    from server.app.modules.accounts.auth import _worker_cancel_login_session
    from server.app.modules.accounts.models import (
        Account,
        AccountLoginSession,
        BrowserProfileLock,
    )
    from server.app.modules.accounts.service import profile_key_from_state_path

    test_app = build_test_app(monkeypatch)
    install_fake_driver(monkeypatch)
    try:
        account = _make_account(test_app)
        _insert_session(
            test_app,
            account["id"],
            sid="sess-wcancel",
            status="active",
            worker_id="w1",
            previous_status="valid",
            browser_session_id=None,
        )
        with test_app.session_factory() as db:
            acc = db.get(Account, account["id"])
            acc.status = "unknown"
            db.commit()
            profile_key = profile_key_from_state_path(acc.state_path)

        # worker 持有该会话的 login 锁
        assert (
            browser.try_acquire_profile_lock(
                profile_key, owner_kind="login", owner_id="sess-wcancel"
            )
            is True
        )

        with test_app.session_factory() as db:
            req = db.get(AccountLoginSession, "sess-wcancel")
            _worker_cancel_login_session(db, req)

        with test_app.session_factory() as db:
            assert db.get(AccountLoginSession, "sess-wcancel").status == "cancelled"
            # 账号状态回滚到登录前
            assert db.get(Account, account["id"]).status == "valid"
            # login 锁释放 → 账号可重新登录
            assert db.get(BrowserProfileLock, profile_key) is None
    finally:
        test_app.cleanup()
