"""
提示词模板 API 测试

覆盖 /api/prompt-templates 下的端点：
- GET    ""              列出当前用户可见模板（本人 + system），可按 scope 过滤
- POST   ""              创建模板（201）
- PUT    /{template_id}  全量更新（name/content/scope/is_system）
- PATCH  /{template_id}  局部更新（is_enabled/scope/is_system）
- DELETE /{template_id}  软删除（204）

可见性与权限：
- 普通 operator 只能改自己的、非 system 的模板，否则 403
- 只有 admin 可以创建 / 标记 is_system=True，否则 403
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


def _create_template(
    client, *, name="模板A", content="正文内容", scope="generation", is_system=False
):
    return client.post(
        "/api/prompt-templates",
        json={"name": name, "content": content, "scope": scope, "is_system": is_system},
    )


class TestCreatePromptTemplate:
    def test_admin_create_happy_path(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = _create_template(
                client, name="写作提示词", content="请写一篇文章", scope="generation"
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["name"] == "写作提示词"
            assert data["content"] == "请写一篇文章"
            assert data["scope"] == "generation"
            assert data["is_system"] is False
            assert data["is_enabled"] is True
            assert data["is_deleted"] is False
            assert data["user_id"] is not None
            assert isinstance(data["id"], int)
            assert "created_at" in data
            assert "updated_at" in data
        finally:
            test_app.cleanup()

    def test_create_ai_format_scope(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = _create_template(
                client, name="格式提示词", content="调整格式", scope="ai_format"
            )
            assert resp.status_code == 201
            assert resp.json()["scope"] == "ai_format"
        finally:
            test_app.cleanup()

    def test_create_defaults_scope_generation(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.post(
                "/api/prompt-templates",
                json={"name": "默认范围", "content": "内容"},
            )
            assert resp.status_code == 201
            assert resp.json()["scope"] == "generation"
            assert resp.json()["is_system"] is False
        finally:
            test_app.cleanup()

    def test_admin_can_create_system_template(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = _create_template(client, name="系统模板", content="系统内容", is_system=True)
            assert resp.status_code == 201
            assert resp.json()["is_system"] is True
        finally:
            test_app.cleanup()

    def test_operator_cannot_create_system_template(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            op_client, _ = _make_operator_client(test_app)
            resp = _create_template(op_client, name="非法系统模板", content="x", is_system=True)
            assert resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_operator_can_create_own_template(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            op_client, uid = _make_operator_client(test_app)
            resp = _create_template(op_client, name="个人模板", content="x")
            assert resp.status_code == 201
            assert resp.json()["user_id"] == uid
            assert resp.json()["is_system"] is False
        finally:
            test_app.cleanup()

    def test_create_empty_name_rejected(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.post("/api/prompt-templates", json={"name": "", "content": "x"})
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_create_empty_content_rejected(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.post("/api/prompt-templates", json={"name": "标题", "content": ""})
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_create_name_too_long_rejected(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.post(
                "/api/prompt-templates",
                json={"name": "x" * 201, "content": "正文"},
            )
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_create_invalid_scope_rejected(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.post(
                "/api/prompt-templates",
                json={"name": "标题", "content": "正文", "scope": "bogus"},
            )
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_create_requires_auth(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            anon = TestClient(test_app.client.app)
            resp = _create_template(anon, name="无权限", content="x")
            assert resp.status_code == 401
        finally:
            test_app.cleanup()


class TestListPromptTemplates:
    def test_list_returns_created_items(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            t1 = _create_template(client, name="模板1", content="c1").json()
            t2 = _create_template(client, name="模板2", content="c2").json()
            resp = client.get("/api/prompt-templates")
            assert resp.status_code == 200
            ids = [t["id"] for t in resp.json()]
            assert t1["id"] in ids
            assert t2["id"] in ids
        finally:
            test_app.cleanup()

    def test_list_filter_by_scope(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            gen = _create_template(client, name="生成", content="c", scope="generation").json()
            fmt = _create_template(client, name="格式", content="c", scope="ai_format").json()

            resp = client.get("/api/prompt-templates", params={"scope": "generation"})
            assert resp.status_code == 200
            ids = [t["id"] for t in resp.json()]
            assert gen["id"] in ids
            assert fmt["id"] not in ids
            assert all(t["scope"] == "generation" for t in resp.json())
        finally:
            test_app.cleanup()

    def test_list_invalid_scope_rejected(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.get("/api/prompt-templates", params={"scope": "bogus"})
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_operator_sees_own_and_system_not_others(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        admin_client = test_app.client
        try:
            # admin 建一个 system 模板 + 一个 admin 私有模板
            system_t = _create_template(
                admin_client, name="系统可见", content="c", is_system=True
            ).json()
            admin_private = _create_template(admin_client, name="管理员私有", content="c").json()

            op_client, _ = _make_operator_client(test_app)
            op_own = _create_template(op_client, name="操作员私有", content="c").json()

            resp = op_client.get("/api/prompt-templates")
            assert resp.status_code == 200
            ids = [t["id"] for t in resp.json()]
            assert op_own["id"] in ids
            assert system_t["id"] in ids
            assert admin_private["id"] not in ids
        finally:
            test_app.cleanup()

    def test_list_requires_auth(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            anon = TestClient(test_app.client.app)
            resp = anon.get("/api/prompt-templates")
            assert resp.status_code == 401
        finally:
            test_app.cleanup()


class TestUpdatePromptTemplate:
    def test_update_happy_path(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            created = _create_template(
                client, name="原名", content="原内容", scope="generation"
            ).json()
            resp = client.put(
                f"/api/prompt-templates/{created['id']}",
                json={"name": "新名", "content": "新内容", "scope": "ai_format"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "新名"
            assert data["content"] == "新内容"
            assert data["scope"] == "ai_format"
        finally:
            test_app.cleanup()

    def test_update_without_scope_keeps_existing(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            created = _create_template(
                client, name="原名", content="原内容", scope="ai_format"
            ).json()
            resp = client.put(
                f"/api/prompt-templates/{created['id']}",
                json={"name": "改名", "content": "改内容"},
            )
            assert resp.status_code == 200
            # scope 未传则保持不变
            assert resp.json()["scope"] == "ai_format"
        finally:
            test_app.cleanup()

    def test_update_404(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.put(
                "/api/prompt-templates/999999",
                json={"name": "x", "content": "y"},
            )
            assert resp.status_code == 404
        finally:
            test_app.cleanup()

    def test_update_validation_error(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            created = _create_template(client, name="名称", content="内容").json()
            resp = client.put(
                f"/api/prompt-templates/{created['id']}",
                json={"name": "", "content": "内容"},
            )
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_operator_cannot_update_system_template(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        admin_client = test_app.client
        try:
            system_t = _create_template(
                admin_client, name="系统模板", content="c", is_system=True
            ).json()
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.put(
                f"/api/prompt-templates/{system_t['id']}",
                json={"name": "想改系统", "content": "c"},
            )
            assert resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_operator_cannot_update_others_template(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        admin_client = test_app.client
        try:
            admin_private = _create_template(admin_client, name="管理员私有", content="c").json()
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.put(
                f"/api/prompt-templates/{admin_private['id']}",
                json={"name": "想改别人", "content": "c"},
            )
            assert resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_operator_can_update_own_template(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            op_client, _ = _make_operator_client(test_app)
            own = _create_template(op_client, name="自己的", content="c").json()
            resp = op_client.put(
                f"/api/prompt-templates/{own['id']}",
                json={"name": "改自己的", "content": "新内容"},
            )
            assert resp.status_code == 200
            assert resp.json()["name"] == "改自己的"
        finally:
            test_app.cleanup()

    def test_operator_cannot_promote_to_system(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            op_client, _ = _make_operator_client(test_app)
            own = _create_template(op_client, name="自己的", content="c").json()
            resp = op_client.put(
                f"/api/prompt-templates/{own['id']}",
                json={"name": "自己的", "content": "c", "is_system": True},
            )
            assert resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_update_requires_auth(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            created = _create_template(client, name="名称", content="内容").json()
            anon = TestClient(test_app.client.app)
            resp = anon.put(
                f"/api/prompt-templates/{created['id']}",
                json={"name": "x", "content": "y"},
            )
            assert resp.status_code == 401
        finally:
            test_app.cleanup()


class TestPatchPromptTemplate:
    def test_patch_toggle_enabled(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            created = _create_template(client, name="开关模板", content="c").json()
            assert created["is_enabled"] is True
            resp = client.patch(
                f"/api/prompt-templates/{created['id']}",
                json={"is_enabled": False},
            )
            assert resp.status_code == 200
            assert resp.json()["is_enabled"] is False
        finally:
            test_app.cleanup()

    def test_patch_change_scope(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            created = _create_template(
                client, name="范围模板", content="c", scope="generation"
            ).json()
            resp = client.patch(
                f"/api/prompt-templates/{created['id']}",
                json={"scope": "ai_format"},
            )
            assert resp.status_code == 200
            assert resp.json()["scope"] == "ai_format"
        finally:
            test_app.cleanup()

    def test_patch_404(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.patch("/api/prompt-templates/999999", json={"is_enabled": False})
            assert resp.status_code == 404
        finally:
            test_app.cleanup()

    def test_patch_invalid_scope_rejected(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            created = _create_template(client, name="范围模板", content="c").json()
            resp = client.patch(
                f"/api/prompt-templates/{created['id']}",
                json={"scope": "bogus"},
            )
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_operator_cannot_patch_system_template(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        admin_client = test_app.client
        try:
            system_t = _create_template(
                admin_client, name="系统模板", content="c", is_system=True
            ).json()
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.patch(
                f"/api/prompt-templates/{system_t['id']}",
                json={"is_enabled": False},
            )
            assert resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_patch_requires_auth(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            created = _create_template(client, name="名称", content="内容").json()
            anon = TestClient(test_app.client.app)
            resp = anon.patch(
                f"/api/prompt-templates/{created['id']}",
                json={"is_enabled": False},
            )
            assert resp.status_code == 401
        finally:
            test_app.cleanup()


class TestDeletePromptTemplate:
    def test_delete_happy_path(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            created = _create_template(client, name="待删除", content="c").json()
            resp = client.delete(f"/api/prompt-templates/{created['id']}")
            assert resp.status_code == 204

            # 软删除后不再可见于列表
            listed = client.get("/api/prompt-templates").json()
            assert created["id"] not in [t["id"] for t in listed]

            # 再次删除应 404（已被软删除，get_prompt_template 过滤 is_deleted）
            again = client.delete(f"/api/prompt-templates/{created['id']}")
            assert again.status_code == 404
        finally:
            test_app.cleanup()

    def test_delete_404(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.delete("/api/prompt-templates/999999")
            assert resp.status_code == 404
        finally:
            test_app.cleanup()

    def test_operator_cannot_delete_system_template(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        admin_client = test_app.client
        try:
            system_t = _create_template(
                admin_client, name="系统模板", content="c", is_system=True
            ).json()
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.delete(f"/api/prompt-templates/{system_t['id']}")
            assert resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_operator_cannot_delete_others_template(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        admin_client = test_app.client
        try:
            admin_private = _create_template(admin_client, name="管理员私有", content="c").json()
            op_client, _ = _make_operator_client(test_app)
            resp = op_client.delete(f"/api/prompt-templates/{admin_private['id']}")
            assert resp.status_code == 403
        finally:
            test_app.cleanup()

    def test_operator_can_delete_own_template(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            op_client, _ = _make_operator_client(test_app)
            own = _create_template(op_client, name="自己的", content="c").json()
            resp = op_client.delete(f"/api/prompt-templates/{own['id']}")
            assert resp.status_code == 204
        finally:
            test_app.cleanup()

    def test_delete_requires_auth(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            created = _create_template(client, name="名称", content="内容").json()
            anon = TestClient(test_app.client.app)
            resp = anon.delete(f"/api/prompt-templates/{created['id']}")
            assert resp.status_code == 401
        finally:
            test_app.cleanup()
