"""
Security boundary tests covering:
- R1/P4: account export restricted to admin
- R2:    article client_request_id dedup still works for same user (regression guard)
- R4:    /api/tasks/preview requires authentication
- R5:    must_change_password blocks all protected endpoints, allows /auth/change-password
- R9:    task creation validates account/article ownership per user
- P1:    operator cannot delete articles
- P2:    operator cannot delete article groups
- P4:    operator cannot delete accounts
- P6:    operator cannot access system status
"""

from fastapi.testclient import TestClient

from server.app.core.security import create_access_token
from server.app.modules.system.models import User
from server.tests.utils import build_test_app

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_operator_client(test_app, username="testoperator") -> tuple[TestClient, int]:
    with test_app.session_factory() as db:
        user = User(username=username, role="operator", is_active=True, must_change_password=False)
        user.set_password("password")
        db.add(user)
        db.commit()
        db.refresh(user)
        uid = user.id
        token = create_access_token(uid, user.role)
    client = TestClient(test_app.client.app)
    client.cookies["access_token"] = token
    return client, uid


def _make_must_change_client(test_app, username="mustchange") -> TestClient:
    with test_app.session_factory() as db:
        user = User(username=username, role="operator", is_active=True, must_change_password=True)
        user.set_password("password")
        db.add(user)
        db.commit()
        db.refresh(user)
        token = create_access_token(user.id, user.role)
    client = TestClient(test_app.client.app)
    client.cookies["access_token"] = token
    return client


def _create_article(client, title="Test Article", crid=None) -> int:
    payload: dict = {"title": title, "content_json": {"type": "doc", "content": []}}
    if crid:
        payload["client_request_id"] = crid
    resp = client.post("/api/articles", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_account(test_app, key="sec-acc", client=None) -> int:
    state_dir = test_app.data_dir / "browser_states" / "toutiao" / key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    c = client or test_app.client
    resp = c.post(
        "/api/accounts/toutiao/login",
        json={"display_name": "Test", "account_key": key, "use_browser": False},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_group(client, name="Test Group") -> int:
    resp = client.post("/api/article-groups", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# ── R1/P4: export admin-only ──────────────────────────────────────────────────


class TestAccountExportAdminOnly:
    def test_operator_cannot_export(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.post("/api/accounts/export", json={})
            assert resp.status_code == 403
            assert resp.json()["detail"] == "需要管理员权限"
        finally:
            test_app.cleanup()

    def test_unauthenticated_cannot_export(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            anon = TestClient(test_app.client.app)
            resp = anon.post("/api/accounts/export", json={})
            assert resp.status_code == 401
        finally:
            test_app.cleanup()


# ── R2: article client_request_id idempotency (same user) ────────────────────


class TestArticleClientRequestIdIdempotency:
    def test_same_user_duplicate_returns_existing(self, monkeypatch):
        """Same user sending the same client_request_id twice gets the same article back."""
        test_app = build_test_app(monkeypatch)
        try:
            client = test_app.client
            crid = "dedup-test-crid-001"
            article_id = _create_article(client, crid=crid)

            # Second request with same crid from same user → idempotent
            resp2 = client.post(
                "/api/articles",
                json={
                    "title": "Duplicate",
                    "content_json": {"type": "doc", "content": []},
                    "client_request_id": crid,
                },
            )
            assert resp2.status_code == 200
            assert resp2.json()["id"] == article_id
        finally:
            test_app.cleanup()


# ── R4: /api/tasks/preview requires authentication ────────────────────────────


class TestTasksPreviewAuth:
    def test_unauthenticated_preview_returns_401(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            anon = TestClient(test_app.client.app)
            resp = anon.post(
                "/api/tasks/preview",
                json={"name": "T", "task_type": "single", "accounts": []},
            )
            assert resp.status_code == 401
        finally:
            test_app.cleanup()

    def test_authenticated_user_can_reach_preview(self, monkeypatch):
        """Authenticated request reaches the endpoint (may return 400 due to missing data, not 401)."""
        test_app = build_test_app(monkeypatch)
        try:
            resp = test_app.client.post(
                "/api/tasks/preview",
                json={"name": "T", "task_type": "single", "accounts": []},
            )
            # 400 is fine — means auth passed but payload validation failed
            assert resp.status_code != 401
        finally:
            test_app.cleanup()


# ── R5: must_change_password blocks protected endpoints ───────────────────────


class TestMustChangePasswordBlocking:
    def test_blocks_articles_list(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            mc = _make_must_change_client(test_app)
            resp = mc.get("/api/articles")
            assert resp.status_code == 403
            assert resp.json()["detail"] == "Password change required"
        finally:
            test_app.cleanup()

    def test_blocks_accounts_list(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            mc = _make_must_change_client(test_app)
            resp = mc.get("/api/accounts")
            assert resp.status_code == 403
            assert resp.json()["detail"] == "Password change required"
        finally:
            test_app.cleanup()

    def test_blocks_tasks_list(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            mc = _make_must_change_client(test_app)
            resp = mc.get("/api/tasks")
            assert resp.status_code == 403
            assert resp.json()["detail"] == "Password change required"
        finally:
            test_app.cleanup()

    def test_allows_change_password(self, monkeypatch):
        """change-password bypasses get_current_user — must work for must_change_password users."""
        test_app = build_test_app(monkeypatch)
        try:
            mc = _make_must_change_client(test_app)
            resp = mc.post(
                "/api/auth/change-password",
                json={"old_password": "password", "new_password": "newpassword123"},
            )
            assert resp.status_code == 200
        finally:
            test_app.cleanup()

    def test_allows_me_endpoint(self, monkeypatch):
        """/api/auth/me bypasses get_current_user — must work for must_change_password users."""
        test_app = build_test_app(monkeypatch)
        try:
            mc = _make_must_change_client(test_app)
            resp = mc.get("/api/auth/me")
            assert resp.status_code == 200
            assert resp.json()["must_change_password"] is True
        finally:
            test_app.cleanup()


# ── P1: operator cannot delete articles ──────────────────────────────────────


class TestOperatorCannotDeleteArticle:
    def test_operator_delete_returns_403(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            article_id = _create_article(test_app.client)
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.delete(f"/api/articles/{article_id}")
            assert resp.status_code == 403
            assert resp.json()["detail"] == "需要管理员权限"
        finally:
            test_app.cleanup()

    def test_admin_delete_succeeds(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            article_id = _create_article(test_app.client)
            resp = test_app.client.delete(f"/api/articles/{article_id}")
            assert resp.status_code == 204
        finally:
            test_app.cleanup()


# ── P2: operator cannot delete article groups ─────────────────────────────────


class TestOperatorCannotDeleteArticleGroup:
    def test_operator_delete_returns_403(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            group_id = _create_group(test_app.client)
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.delete(f"/api/article-groups/{group_id}")
            assert resp.status_code == 403
            assert resp.json()["detail"] == "需要管理员权限"
        finally:
            test_app.cleanup()

    def test_admin_delete_succeeds(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            group_id = _create_group(test_app.client)
            resp = test_app.client.delete(f"/api/article-groups/{group_id}")
            assert resp.status_code == 204
        finally:
            test_app.cleanup()


# ── P4: operator cannot delete accounts ──────────────────────────────────────


class TestOperatorCannotDeleteAccount:
    def test_operator_delete_returns_403(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            account_id = _create_account(test_app)
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.delete(f"/api/accounts/{account_id}")
            assert resp.status_code == 403
            assert resp.json()["detail"] == "需要管理员权限"
        finally:
            test_app.cleanup()

    def test_admin_delete_succeeds(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            account_id = _create_account(test_app)
            resp = test_app.client.delete(f"/api/accounts/{account_id}")
            assert resp.status_code == 204
        finally:
            test_app.cleanup()


# ── P6: operator cannot access system status ──────────────────────────────────


class TestOperatorCannotAccessSystemStatus:
    def test_operator_returns_403(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.get("/api/system/status")
            assert resp.status_code == 403
            assert resp.json()["detail"] == "需要管理员权限"
        finally:
            test_app.cleanup()

    def test_unauthenticated_returns_401(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            anon = TestClient(test_app.client.app)
            resp = anon.get("/api/system/status")
            assert resp.status_code == 401
        finally:
            test_app.cleanup()

    def test_admin_can_access(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            resp = test_app.client.get("/api/system/status")
            assert resp.status_code == 200
        finally:
            test_app.cleanup()


# ── R9: task ownership validation ─────────────────────────────────────────────


class TestTaskOwnershipValidation:
    def test_operator_cannot_use_admin_account(self, monkeypatch):
        """Operator cannot create a task with another user's account — returns 400."""
        test_app = build_test_app(monkeypatch)
        try:
            # Admin creates an account (belongs to admin)
            admin_account_id = _create_account(test_app, key="admin-acc-r9")
            # Operator creates their own article
            op_client, _ = _make_operator_client(test_app)
            op_article_id = _create_article(op_client, title="Op Article R9")

            resp = op_client.post(
                "/api/tasks",
                json={
                    "name": "Bad Task",
                    "task_type": "single",
                    "article_id": op_article_id,
                    "accounts": [{"account_id": admin_account_id, "sort_order": 0}],
                },
            )
            assert resp.status_code == 400
            assert "Account not found" in resp.json()["detail"]
        finally:
            test_app.cleanup()

    def test_operator_cannot_use_admin_article(self, monkeypatch):
        """Operator cannot create a task with another user's article — returns 400."""
        test_app = build_test_app(monkeypatch)
        try:
            # Admin creates an article (belongs to admin)
            admin_article_id = _create_article(test_app.client, title="Admin Article R9")
            # Operator creates their own account
            op_client, _ = _make_operator_client(test_app)
            op_account_id = _create_account(test_app, key="op-acc-r9", client=op_client)

            resp = op_client.post(
                "/api/tasks",
                json={
                    "name": "Bad Task",
                    "task_type": "single",
                    "article_id": admin_article_id,
                    "accounts": [{"account_id": op_account_id, "sort_order": 0}],
                },
            )
            assert resp.status_code == 400
            assert "Article not found" in resp.json()["detail"]
        finally:
            test_app.cleanup()

    def test_admin_can_use_any_account(self, monkeypatch):
        """Admin bypasses ownership check and can create a task with any user's account."""
        test_app = build_test_app(monkeypatch)
        try:
            # Operator creates their own account
            op_client, _ = _make_operator_client(test_app)
            op_account_id = _create_account(test_app, key="op-acc-r9-admin", client=op_client)
            # Admin creates their own article
            admin_article_id = _create_article(test_app.client, title="Admin Article R9 Admin")

            resp = test_app.client.post(
                "/api/tasks",
                json={
                    "name": "Admin Task",
                    "task_type": "single",
                    "article_id": admin_article_id,
                    "accounts": [{"account_id": op_account_id, "sort_order": 0}],
                },
            )
            assert resp.status_code == 200
        finally:
            test_app.cleanup()

    def test_operator_can_use_own_account_and_article(self, monkeypatch):
        """Operator can create a task with their own account and article."""
        test_app = build_test_app(monkeypatch)
        try:
            op_client, _ = _make_operator_client(test_app)
            op_account_id = _create_account(test_app, key="op-own-acc-r9", client=op_client)
            op_article_id = _create_article(op_client, title="Op Own Article R9")

            resp = op_client.post(
                "/api/tasks",
                json={
                    "name": "Own Task",
                    "task_type": "single",
                    "article_id": op_article_id,
                    "accounts": [{"account_id": op_account_id, "sort_order": 0}],
                },
            )
            assert resp.status_code == 200
        finally:
            test_app.cleanup()
