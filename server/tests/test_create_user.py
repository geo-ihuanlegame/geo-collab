"""POST /api/auth/users（管理员创建用户）测试。

该端点此前零覆盖。它是权限敏感的写端点，含两段 admin 校验（JWT role 声明 + 回库核对
caller.role/is_active）、用户名冲突 409、新用户默认 must_change_password=True。
鉴权写法是「手动 verify_token + 自开 SessionLocal」，build_test_app 已把 SessionLocal
monkeypatch 到测试库，故该路径走的是测试 schema。
"""

from fastapi.testclient import TestClient

from server.app.core.security import create_access_token
from server.app.modules.audit.models import AuditLog
from server.app.modules.system.models import User
from server.tests.utils import build_test_app, create_extra_user


def test_admin_creates_user_defaults_to_must_change_password(monkeypatch):
    """管理员建用户成功：返回新用户、is_active=True、must_change_password=True，且能登录到。"""
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.post(
            "/api/auth/users",
            json={"username": "newbie", "password": "initpass123", "role": "operator"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["username"] == "newbie"
        assert body["role"] == "operator"
        assert body["is_active"] is True
        assert body["must_change_password"] is True

        with test_app.session_factory() as db:
            created = db.query(User).filter(User.username == "newbie").one()
            assert created.must_change_password is True
            assert created.check_password("initpass123")
    finally:
        test_app.cleanup()


def test_create_user_writes_audit_entry(monkeypatch):
    """建用户落审计 user.create，target 指向新用户。"""
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.post(
            "/api/auth/users",
            json={"username": "audited", "password": "initpass123"},
        )
        new_id = resp.json()["id"]
        with test_app.session_factory() as db:
            entry = (
                db.query(AuditLog)
                .filter(AuditLog.action == "user.create", AuditLog.target_id == str(new_id))
                .first()
            )
            assert entry is not None
            assert entry.target_type == "user"
    finally:
        test_app.cleanup()


def test_duplicate_username_returns_409(monkeypatch):
    """用户名已存在 → 409，不创建第二个。"""
    test_app = build_test_app(monkeypatch)
    try:
        first = test_app.client.post(
            "/api/auth/users", json={"username": "dup", "password": "initpass123"}
        )
        assert first.status_code == 200, first.text
        again = test_app.client.post(
            "/api/auth/users", json={"username": "dup", "password": "otherpass123"}
        )
        assert again.status_code == 409
        with test_app.session_factory() as db:
            assert db.query(User).filter(User.username == "dup").count() == 1
    finally:
        test_app.cleanup()


def test_operator_cannot_create_user(monkeypatch):
    """非管理员（operator JWT）被第一段 role 校验挡下 → 403，不建用户。"""
    test_app = build_test_app(monkeypatch)
    try:
        _, op_client = create_extra_user(test_app, "op_caller", role="operator")
        resp = op_client.post(
            "/api/auth/users", json={"username": "ghost", "password": "initpass123"}
        )
        assert resp.status_code == 403
        with test_app.session_factory() as db:
            assert db.query(User).filter(User.username == "ghost").count() == 0
    finally:
        test_app.cleanup()


def test_anonymous_cannot_create_user(monkeypatch):
    """无 token → 401。"""
    test_app = build_test_app(monkeypatch)
    try:
        anon = TestClient(test_app.client.app)
        resp = anon.post("/api/auth/users", json={"username": "x", "password": "initpass123"})
        assert resp.status_code == 401
    finally:
        test_app.cleanup()


def test_token_admin_but_db_demoted_is_rejected(monkeypatch):
    """JWT role=admin 但回库 caller.role 已被降为 operator → 第二段校验拦下 403。

    专门覆盖「回库二次核对」分支（防 token 签发后用户被降权/禁用仍能建号）。
    若实现只信 JWT role 声明、跳过回库核对，本断言即红。
    """
    test_app = build_test_app(monkeypatch)
    try:
        # 造一个 admin 用户 + admin 声明的 token，随后在库里把它降为 operator
        with test_app.session_factory() as db:
            u = User(username="was_admin", role="admin", is_active=True, must_change_password=False)
            u.set_password("pass1234")
            db.add(u)
            db.commit()
            db.refresh(u)
            uid = u.id
        token = create_access_token(uid, "admin")  # JWT 仍声称 admin
        with test_app.session_factory() as db:
            db.get(User, uid).role = "operator"  # 实际已降权
            db.commit()

        client = TestClient(test_app.client.app)
        client.cookies["access_token"] = token
        resp = client.post("/api/auth/users", json={"username": "sneak", "password": "initpass123"})
        assert resp.status_code == 403
        with test_app.session_factory() as db:
            assert db.query(User).filter(User.username == "sneak").count() == 0
    finally:
        test_app.cleanup()
