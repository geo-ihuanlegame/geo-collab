"""审计日志：辅助函数 + 查询 API + 端到端钩子测试（对应 plan.md A.8 八用例）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from server.app.core.security import create_access_token
from server.app.modules.audit.models import AuditLog
from server.app.modules.audit.service import _redact, add_audit_entry
from server.app.modules.system.models import Platform, User
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _make_operator_client(test_app, username: str = "op1") -> tuple[TestClient, int]:
    with test_app.session_factory() as db:
        user = User(username=username, role="operator", is_active=True, must_change_password=False)
        user.set_password("pass1234")
        db.add(user)
        db.commit()
        db.refresh(user)
        uid = user.id
        token = create_access_token(uid, user.role)
    client = TestClient(test_app.client.app)
    client.cookies["access_token"] = token
    return client, uid


def _seed_test_account(test_app, *, display_name: str = "测试账号") -> int:
    """直接写入数据库，跳过浏览器登录流程，返回 account_id。"""
    from server.app.modules.accounts.models import Account

    with test_app.session_factory() as db:
        admin = db.query(User).filter(User.username == "testadmin").one()
        platform = Platform(code="toutiao", name="头条号", base_url="https://mp.toutiao.com")
        account = Account(
            user_id=admin.id,
            platform=platform,
            display_name=display_name,
            state_path="browser_states/toutiao/seed/storage_state.json",
            status="valid",
        )
        db.add(platform)
        db.add(account)
        db.commit()
        return account.id


# ============== 1. 辅助函数直接写入 ==============


def test_add_audit_entry_writes_row(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            admin = db.query(User).filter(User.username == "testadmin").one()
            admin_id = admin.id  # 先捞出来，避免会话关闭后对象脱管
            add_audit_entry(
                db,
                user=admin,
                action="test.helper",
                target_type="user",
                target_id=admin_id,
                payload={"foo": "bar"},
            )

        with test_app.session_factory() as db:
            row = db.query(AuditLog).filter(AuditLog.action == "test.helper").one()
            assert row.user_id == admin_id
            assert row.username == "testadmin"
            assert row.target_type == "user"
            assert row.target_id == str(admin_id)
            assert row.payload_json == {"foo": "bar"}
            assert row.created_at is not None
    finally:
        test_app.cleanup()


# ============== 2. _redact 脱敏 ==============


def test_audit_helper_redacts_password_payload(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        # 纯函数级单测
        payload = {
            "username": "alice",
            "password": "s3cret",
            "new_password": "s3cret2",
            "old_password": "old",
            "api_key": "ak-xyz",
            "feishu_app_secret": "fs-xyz",
            "nested": {"refresh_token": "rt-xyz", "ok": 1},
            "list_with_secret": [{"access_token": "at-xyz"}, {"keep": "this"}],
        }
        out = _redact(payload)
        assert out["username"] == "alice"
        assert out["password"] == "***"
        assert out["new_password"] == "***"
        assert out["old_password"] == "***"
        assert out["api_key"] == "***"
        assert out["feishu_app_secret"] == "***"
        assert out["nested"]["refresh_token"] == "***"
        assert out["nested"]["ok"] == 1
        assert out["list_with_secret"][0]["access_token"] == "***"
        assert out["list_with_secret"][1]["keep"] == "this"

        # 通过辅助函数走端到端：数据库中应当落盘脱敏后的值
        with test_app.session_factory() as db:
            admin = db.query(User).filter(User.username == "testadmin").one()
            add_audit_entry(
                db,
                user=admin,
                action="test.redact",
                target_type="user",
                payload={"username": "alice", "password": "shouldbe***"},
            )
        with test_app.session_factory() as db:
            row = db.query(AuditLog).filter(AuditLog.action == "test.redact").one()
            assert row.payload_json["username"] == "alice"
            assert row.payload_json["password"] == "***"
    finally:
        test_app.cleanup()


# ============== 3. 故障注入：审计失败不影响主流程 ==============


def test_audit_failure_does_not_break_main_flow(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        account_id = _seed_test_account(test_app, display_name="不能 audit 的账号")

        # 让辅助函数内部抛异常：拦截 AuditLog 的构造
        from server.app.modules.audit import service as audit_service

        class BadAuditLog:
            def __init__(self, *_args, **_kwargs):
                raise RuntimeError("synthetic audit failure")

        monkeypatch.setattr(audit_service, "AuditLog", BadAuditLog)

        # DELETE 应当仍成功，审计异常被辅助函数吞掉
        resp = test_app.client.delete(f"/api/accounts/{account_id}")
        assert resp.status_code in (200, 204)

        # 数据库里没有 account.delete 的审计行（写入被吞了）
        with test_app.session_factory() as db:
            count = db.query(AuditLog).filter(AuditLog.action == "account.delete").count()
            assert count == 0

            # 账号确实软删了（业务流程不受影响）
            from server.app.modules.accounts.models import Account

            acc = db.query(Account).filter(Account.id == account_id).one()
            assert acc.is_deleted is True
    finally:
        test_app.cleanup()


# ============== 4. 登录走真实端点 → audit 行 ==============


def test_login_creates_audit_entry(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        anon = TestClient(test_app.client.app)

        # 成功登录
        resp = anon.post("/api/auth/login", json={"username": "testadmin", "password": "testadmin"})
        assert resp.status_code == 200

        with test_app.session_factory() as db:
            rows = db.query(AuditLog).filter(AuditLog.action == "user.login").all()
            assert len(rows) == 1
            row = rows[0]
            assert row.user_id is not None
            assert row.username == "testadmin"
            assert row.payload_json["success"] is True
            assert row.payload_json["username"] == "testadmin"

        # 失败也记
        resp = anon.post("/api/auth/login", json={"username": "testadmin", "password": "wrong"})
        assert resp.status_code == 401

        with test_app.session_factory() as db:
            rows = (
                db.query(AuditLog)
                .filter(AuditLog.action == "user.login")
                .order_by(AuditLog.id)
                .all()
            )
            assert len(rows) == 2
            assert rows[1].payload_json["success"] is False
            assert rows[1].user_id is None  # 失败不归属用户
            assert rows[1].payload_json["username"] == "testadmin"
    finally:
        test_app.cleanup()


# ============== 5. DELETE /accounts → audit 行 ==============


def test_account_delete_creates_audit_entry(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        account_id = _seed_test_account(test_app, display_name="待删除的账号")

        resp = test_app.client.delete(f"/api/accounts/{account_id}")
        assert resp.status_code in (200, 204)

        with test_app.session_factory() as db:
            row = db.query(AuditLog).filter(AuditLog.action == "account.delete").one()
            assert row.target_type == "account"
            assert row.target_id == str(account_id)
            assert row.payload_json["display_name"] == "待删除的账号"
            assert row.user_id is not None
    finally:
        test_app.cleanup()


# ============== 6/7/8. GET /api/audit-logs ==============


def _seed_audit_rows(test_app) -> None:
    """种 8 条审计行（5 个 account.* + 3 个 user.*）。"""
    with test_app.session_factory() as db:
        admin = db.query(User).filter(User.username == "testadmin").one()
        base = datetime(2026, 1, 1, 12, 0, 0)
        for i in range(5):
            db.add(
                AuditLog(
                    user_id=admin.id,
                    username="testadmin",
                    action=f"account.test{i}",
                    target_type="account",
                    target_id=str(100 + i),
                    created_at=base + timedelta(seconds=i),
                )
            )
        for i in range(3):
            db.add(
                AuditLog(
                    user_id=admin.id,
                    username="testadmin",
                    action=f"user.test{i}",
                    target_type="user",
                    target_id=str(200 + i),
                    created_at=base + timedelta(seconds=10 + i),
                )
            )
        db.commit()


def test_list_endpoint_requires_admin(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        op_client, _ = _make_operator_client(test_app)
        resp = op_client.get("/api/audit-logs")
        assert resp.status_code == 403

        # 匿名访问 401
        anon = TestClient(test_app.client.app)
        resp = anon.get("/api/audit-logs")
        assert resp.status_code == 401
    finally:
        test_app.cleanup()


def test_list_endpoint_filters_by_action_prefix(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _seed_audit_rows(test_app)
        resp = test_app.client.get("/api/audit-logs", params={"action_prefix": "account."})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 5
        for it in data["items"]:
            assert it["action"].startswith("account.")

        # 按 target_type 过滤
        resp = test_app.client.get("/api/audit-logs", params={"target_type": "user"})
        data = resp.json()
        assert len(data["items"]) == 3
        for it in data["items"]:
            assert it["target_type"] == "user"
    finally:
        test_app.cleanup()


def test_list_endpoint_cursor_pagination(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _seed_audit_rows(test_app)

        # 第一页：limit=3
        r1 = test_app.client.get("/api/audit-logs", params={"limit": 3})
        assert r1.status_code == 200
        page1 = r1.json()
        assert len(page1["items"]) == 3
        assert page1["next_cursor"] is not None

        # 第二页
        r2 = test_app.client.get(
            "/api/audit-logs", params={"limit": 3, "cursor": page1["next_cursor"]}
        )
        page2 = r2.json()
        assert len(page2["items"]) == 3
        assert page2["next_cursor"] is not None

        # 第三页（剩 2 条）
        r3 = test_app.client.get(
            "/api/audit-logs", params={"limit": 3, "cursor": page2["next_cursor"]}
        )
        page3 = r3.json()
        assert len(page3["items"]) == 2
        assert page3["next_cursor"] is None  # 已无更多

        # 三页 ID 不重复
        all_ids = (
            [it["id"] for it in page1["items"]]
            + [it["id"] for it in page2["items"]]
            + [it["id"] for it in page3["items"]]
        )
        assert len(set(all_ids)) == 8
    finally:
        test_app.cleanup()
