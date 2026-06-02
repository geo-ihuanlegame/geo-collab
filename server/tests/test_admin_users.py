"""
管理后台用户管理 API 测试

覆盖：
- GET    /api/auth/users         列出用户
- PATCH  /api/auth/users/{id}    更新用户（启用/禁用/改角色）
- POST   /api/auth/users/{id}/reset-password  重置密码
"""

from fastapi.testclient import TestClient

from server.app.core.security import create_access_token
from server.app.modules.system.models import User
from server.tests.utils import build_test_app


def _make_operator_client(test_app, username="op1") -> tuple[TestClient, int]:
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


def _make_extra_user(test_app, username="extra", role="operator", is_active=True) -> int:
    with test_app.session_factory() as db:
        user = User(username=username, role=role, is_active=is_active, must_change_password=False)
        user.set_password("pass1234")
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id


class TestListUsers:
    def test_admin_can_list_users(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            _make_extra_user(test_app, "user_a")
            _make_extra_user(test_app, "user_b")
            resp = test_app.client.get("/api/auth/users")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            usernames = [u["username"] for u in data]
            assert "testadmin" in usernames
            assert "user_a" in usernames
            assert "user_b" in usernames
        finally:
            test_app.cleanup()

    def test_operator_gets_403(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.get("/api/auth/users")
            assert resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_unauthenticated_gets_401(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            anon = TestClient(test_app.client.app)
            resp = anon.get("/api/auth/users")
            assert resp.status_code == 401
        finally:
            test_app.cleanup()

    def test_response_contains_expected_fields(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            resp = test_app.client.get("/api/auth/users")
            assert resp.status_code == 200
            user = resp.json()[0]
            for field in (
                "id",
                "username",
                "role",
                "is_active",
                "must_change_password",
                "created_at",
                "last_login_at",
            ):
                assert field in user, f"Missing field: {field}"
        finally:
            test_app.cleanup()


class TestUpdateUser:
    def test_disable_user_blocks_login(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            uid = _make_extra_user(test_app, "victim")
            resp = test_app.client.patch(f"/api/auth/users/{uid}", json={"is_active": False})
            assert resp.status_code == 200
            assert resp.json()["is_active"] is False

            login_resp = test_app.client.post(
                "/api/auth/login", json={"username": "victim", "password": "pass1234"}
            )
            assert login_resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_enable_disabled_user(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            uid = _make_extra_user(test_app, "sleeper", is_active=False)
            resp = test_app.client.patch(f"/api/auth/users/{uid}", json={"is_active": True})
            assert resp.status_code == 200
            assert resp.json()["is_active"] is True
        finally:
            test_app.cleanup()

    def test_change_role(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            uid = _make_extra_user(test_app, "promoted", role="operator")
            resp = test_app.client.patch(f"/api/auth/users/{uid}", json={"role": "admin"})
            assert resp.status_code == 200
            assert resp.json()["role"] == "admin"
        finally:
            test_app.cleanup()

    def test_cannot_modify_own_account(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            # find admin's own id
            users = test_app.client.get("/api/auth/users").json()
            admin_id = next(u["id"] for u in users if u["username"] == "testadmin")
            resp = test_app.client.patch(f"/api/auth/users/{admin_id}", json={"is_active": False})
            assert resp.status_code == 400
        finally:
            test_app.cleanup()

    def test_operator_gets_403(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            uid = _make_extra_user(test_app, "target")
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.patch(f"/api/auth/users/{uid}", json={"is_active": False})
            assert resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_nonexistent_user_404(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            resp = test_app.client.patch("/api/auth/users/99999", json={"is_active": False})
            assert resp.status_code == 404
        finally:
            test_app.cleanup()


class TestResetPassword:
    def test_reset_allows_login_with_new_password(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            uid = _make_extra_user(test_app, "resetme")
            resp = test_app.client.post(
                f"/api/auth/users/{uid}/reset-password", json={"new_password": "newpass999"}
            )
            assert resp.status_code == 200

            login_resp = test_app.client.post(
                "/api/auth/login", json={"username": "resetme", "password": "newpass999"}
            )
            assert login_resp.status_code == 200
        finally:
            test_app.cleanup()

    def test_reset_old_password_no_longer_works(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            uid = _make_extra_user(test_app, "oldpass")
            test_app.client.post(
                f"/api/auth/users/{uid}/reset-password", json={"new_password": "newpass999"}
            )

            login_resp = test_app.client.post(
                "/api/auth/login", json={"username": "oldpass", "password": "pass1234"}
            )
            assert login_resp.status_code == 401
        finally:
            test_app.cleanup()

    def test_reset_sets_must_change_password(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            uid = _make_extra_user(test_app, "flagme")
            test_app.client.post(
                f"/api/auth/users/{uid}/reset-password", json={"new_password": "newpass999"}
            )

            users = test_app.client.get("/api/auth/users").json()
            flagme = next(u for u in users if u["username"] == "flagme")
            assert flagme["must_change_password"] is True
        finally:
            test_app.cleanup()

    def test_operator_gets_403(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            uid = _make_extra_user(test_app, "target2")
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.post(
                f"/api/auth/users/{uid}/reset-password", json={"new_password": "hack"}
            )
            assert resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_nonexistent_user_404(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            # Use a valid-length password so request-body validation (min_length)
            # passes and we actually reach the user-lookup 404 path.
            resp = test_app.client.post(
                "/api/auth/users/99999/reset-password", json={"new_password": "validpass123"}
            )
            assert resp.status_code == 404
        finally:
            test_app.cleanup()
