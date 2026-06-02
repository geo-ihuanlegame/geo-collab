"""
技能（skills）模块 API 测试

覆盖端点（全部挂在 /api/skills 下，依赖 get_current_user）：
- GET    /api/skills              列出未删除的 skill
- POST   /api/skills              新建 skill（201）
- PUT    /api/skills/{id}         全量更新 name/content/description
- PATCH  /api/skills/{id}         切换 is_enabled
- DELETE /api/skills/{id}         软删除（204）
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


def _create_skill(client, name: str, content: str = "技能正文内容", description=None) -> dict:
    body = {"name": name, "content": content}
    if description is not None:
        body["description"] = description
    response = client.post("/api/skills", json=body)
    assert response.status_code == 201, response.text
    return response.json()


class TestCreateSkill:
    def test_create_happy_path(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "写作技巧", content="正文", description="一段描述")
            assert skill["name"] == "写作技巧"
            assert skill["content"] == "正文"
            assert skill["description"] == "一段描述"
            assert skill["is_enabled"] is True
            assert skill["is_deleted"] is False
            assert isinstance(skill["id"], int)
            for field in (
                "id",
                "name",
                "description",
                "content",
                "is_enabled",
                "is_deleted",
                "created_at",
            ):
                assert field in skill, f"Missing field: {field}"
        finally:
            test_app.cleanup()

    def test_create_strips_name(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "  有空格的名字  ")
            assert skill["name"] == "有空格的名字"
        finally:
            test_app.cleanup()

    def test_create_default_description_is_null(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "无描述技能")
            assert skill["description"] is None
        finally:
            test_app.cleanup()

    def test_create_empty_description_becomes_null(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "空描述技能", description="")
            # 路由把空字符串 description 归一化为 None
            assert skill["description"] is None
        finally:
            test_app.cleanup()

    def test_create_missing_name_422(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.post("/api/skills", json={"content": "正文"})
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_create_missing_content_422(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.post("/api/skills", json={"name": "缺正文"})
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_create_empty_name_422(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.post("/api/skills", json={"name": "", "content": "正文"})
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_create_empty_content_422(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.post("/api/skills", json={"name": "技能", "content": ""})
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_create_name_too_long_422(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.post("/api/skills", json={"name": "x" * 201, "content": "正文"})
            assert resp.status_code == 422
        finally:
            test_app.cleanup()


class TestListSkills:
    def test_list_returns_created_items(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            s1 = _create_skill(client, "技能一")
            s2 = _create_skill(client, "技能二")

            resp = client.get("/api/skills")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            ids = [s["id"] for s in data]
            assert s1["id"] in ids
            assert s2["id"] in ids
            # service 按 id 升序排序
            assert ids == sorted(ids)
        finally:
            test_app.cleanup()

    def test_list_empty_when_none(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.get("/api/skills")
            assert resp.status_code == 200
            assert resp.json() == []
        finally:
            test_app.cleanup()

    def test_deleted_skill_excluded_from_list(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            keep = _create_skill(client, "保留")
            gone = _create_skill(client, "删除")
            assert client.delete(f"/api/skills/{gone['id']}").status_code == 204

            ids = [s["id"] for s in client.get("/api/skills").json()]
            assert keep["id"] in ids
            assert gone["id"] not in ids
        finally:
            test_app.cleanup()


class TestUpdateSkill:
    def test_update_happy_path(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "原名", content="原正文", description="原描述")
            resp = client.put(
                f"/api/skills/{skill['id']}",
                json={"name": "新名", "content": "新正文", "description": "新描述"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == skill["id"]
            assert data["name"] == "新名"
            assert data["content"] == "新正文"
            assert data["description"] == "新描述"
        finally:
            test_app.cleanup()

    def test_update_strips_name(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "原名")
            resp = client.put(
                f"/api/skills/{skill['id']}",
                json={"name": "  改名  ", "content": "正文"},
            )
            assert resp.status_code == 200
            assert resp.json()["name"] == "改名"
        finally:
            test_app.cleanup()

    def test_update_clears_description_with_empty_string(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "技能", description="有描述")
            resp = client.put(
                f"/api/skills/{skill['id']}",
                json={"name": "技能", "content": "正文", "description": ""},
            )
            assert resp.status_code == 200
            # 路由把空字符串 description 归一化为 None
            assert resp.json()["description"] is None
        finally:
            test_app.cleanup()

    def test_update_nonexistent_404(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.put(
                "/api/skills/99999",
                json={"name": "技能", "content": "正文"},
            )
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Skill 不存在"
        finally:
            test_app.cleanup()

    def test_update_validation_error_422(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "技能")
            resp = client.put(
                f"/api/skills/{skill['id']}",
                json={"name": "", "content": "正文"},
            )
            assert resp.status_code == 422
        finally:
            test_app.cleanup()

    def test_update_missing_content_422(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "技能")
            resp = client.put(
                f"/api/skills/{skill['id']}",
                json={"name": "技能"},
            )
            assert resp.status_code == 422
        finally:
            test_app.cleanup()


class TestPatchSkill:
    def test_patch_disable(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "技能")
            assert skill["is_enabled"] is True

            resp = client.patch(f"/api/skills/{skill['id']}", json={"is_enabled": False})
            assert resp.status_code == 200
            assert resp.json()["is_enabled"] is False
        finally:
            test_app.cleanup()

    def test_patch_re_enable(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "技能")
            client.patch(f"/api/skills/{skill['id']}", json={"is_enabled": False})
            resp = client.patch(f"/api/skills/{skill['id']}", json={"is_enabled": True})
            assert resp.status_code == 200
            assert resp.json()["is_enabled"] is True
        finally:
            test_app.cleanup()

    def test_patch_nonexistent_404(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.patch("/api/skills/99999", json={"is_enabled": False})
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Skill 不存在"
        finally:
            test_app.cleanup()

    def test_patch_missing_field_422(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "技能")
            resp = client.patch(f"/api/skills/{skill['id']}", json={})
            assert resp.status_code == 422
        finally:
            test_app.cleanup()


class TestDeleteSkill:
    def test_delete_happy_path(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "待删除")
            resp = client.delete(f"/api/skills/{skill['id']}")
            assert resp.status_code == 204

            ids = [s["id"] for s in client.get("/api/skills").json()]
            assert skill["id"] not in ids
        finally:
            test_app.cleanup()

    def test_delete_nonexistent_404(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            resp = client.delete("/api/skills/99999")
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Skill 不存在"
        finally:
            test_app.cleanup()

    def test_delete_twice_second_is_404(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        client = test_app.client
        try:
            skill = _create_skill(client, "重复删除")
            assert client.delete(f"/api/skills/{skill['id']}").status_code == 204
            # 软删除后再次删除应 404（get_skill 过滤 is_deleted）
            assert client.delete(f"/api/skills/{skill['id']}").status_code == 404
        finally:
            test_app.cleanup()


class TestAuthBoundaries:
    def test_unauthenticated_list_401(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            anon = TestClient(test_app.client.app)
            resp = anon.get("/api/skills")
            assert resp.status_code == 401
        finally:
            test_app.cleanup()

    def test_unauthenticated_create_401(self, monkeypatch):
        test_app = build_test_app(monkeypatch)
        try:
            anon = TestClient(test_app.client.app)
            resp = anon.post("/api/skills", json={"name": "技能", "content": "正文"})
            assert resp.status_code == 401
        finally:
            test_app.cleanup()

    def test_operator_can_use_skills(self, monkeypatch):
        # skills 路由只依赖 get_current_user（无 require_admin），operator 同样有完整权限
        test_app = build_test_app(monkeypatch)
        try:
            op_client, _ = _make_operator_client(test_app)
            created = op_client.post("/api/skills", json={"name": "运营技能", "content": "正文"})
            assert created.status_code == 201
            skill_id = created.json()["id"]

            assert op_client.get("/api/skills").status_code == 200
            assert (
                op_client.put(
                    f"/api/skills/{skill_id}",
                    json={"name": "运营技能改", "content": "正文2"},
                ).status_code
                == 200
            )
            assert (
                op_client.patch(f"/api/skills/{skill_id}", json={"is_enabled": False}).status_code
                == 200
            )
            assert op_client.delete(f"/api/skills/{skill_id}").status_code == 204
        finally:
            test_app.cleanup()
