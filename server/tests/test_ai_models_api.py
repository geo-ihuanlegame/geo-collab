"""AI 模型注册表 admin CRUD + 403 门禁 + 每 scope 默认唯一 + 播种 + 下拉反映 DB。"""

from __future__ import annotations

import pytest

from server.app.modules.ai_models.models import AiModel
from server.app.modules.ai_models.service import seed_ai_models_if_empty
from server.tests.utils import build_test_app, create_extra_user

pytestmark = pytest.mark.mysql


def test_admin_crud_lifecycle(monkeypatch):
    app = build_test_app(monkeypatch)
    c = app.client
    try:
        r = c.post(
            "/api/ai-models",
            json={
                "label": "Opus",
                "model": "claude-opus-4-8",
                "scope": "generation",
                "base_url": "https://relay/v1",
                "api_key_env": "GEO_X",
                "is_default": True,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        mid = body["id"]
        assert body["api_key_env"] == "GEO_X"  # 变量名回传安全
        assert "api_key" not in body  # 密钥本体绝不出现

        assert any(m["id"] == mid for m in c.get("/api/ai-models?scope=generation").json())
        assert c.get(f"/api/ai-models/{mid}").status_code == 200

        r = c.patch(f"/api/ai-models/{mid}", json={"label": "Opus2", "is_enabled": False})
        assert r.status_code == 200
        assert r.json()["label"] == "Opus2"
        assert r.json()["is_enabled"] is False

        assert c.delete(f"/api/ai-models/{mid}").status_code == 204
        assert c.get(f"/api/ai-models/{mid}").status_code == 404
    finally:
        app.cleanup()


def test_non_admin_forbidden(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _, opc = create_extra_user(app, "op1", role="operator")
        assert opc.get("/api/ai-models").status_code == 403
        assert (
            opc.post("/api/ai-models", json={"label": "x", "scope": "generation"}).status_code
            == 403
        )
        assert opc.patch("/api/ai-models/1", json={"label": "y"}).status_code == 403
        assert opc.delete("/api/ai-models/1").status_code == 403
    finally:
        app.cleanup()


def test_invalid_scope_rejected(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        r = app.client.post("/api/ai-models", json={"label": "x", "scope": "bogus"})
        assert r.status_code == 422
    finally:
        app.cleanup()


def test_default_uniqueness_within_scope(monkeypatch):
    app = build_test_app(monkeypatch)
    c = app.client
    try:
        c.post(
            "/api/ai-models",
            json={"label": "A", "model": "a", "scope": "generation", "is_default": True},
        )
        b = c.post(
            "/api/ai-models",
            json={"label": "B", "model": "b", "scope": "generation", "is_default": True},
        ).json()
        with app.session_factory() as db:
            gen_defaults = (
                db.query(AiModel)
                .filter(AiModel.scope == "generation", AiModel.is_default.is_(True))
                .all()
            )
            assert len(gen_defaults) == 1
            assert gen_defaults[0].id == b["id"]
            # 跨 scope 独立：ai_format 的（seed 建的）默认仍在
            fmt_defaults = (
                db.query(AiModel)
                .filter(AiModel.scope == "ai_format", AiModel.is_default.is_(True))
                .count()
            )
            assert fmt_defaults == 1
    finally:
        app.cleanup()


def test_seed_idempotent(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        with app.session_factory() as db:
            # create_app 已播种过 → 非空
            n0 = db.query(AiModel).count()
            assert n0 >= 1
            seed_ai_models_if_empty(db)  # 非空 = no-op
            assert db.query(AiModel).count() == n0

            db.query(AiModel).delete()
            db.commit()
            seed_ai_models_if_empty(db)  # 空 = 重新播种
            n1 = db.query(AiModel).count()
            assert n1 >= 1
            seed_ai_models_if_empty(db)  # 幂等
            assert db.query(AiModel).count() == n1
            # 恰好一个 ai_format 默认
            fmt_defaults = (
                db.query(AiModel)
                .filter(AiModel.scope == "ai_format", AiModel.is_default.is_(True))
                .count()
            )
            assert fmt_defaults == 1
    finally:
        app.cleanup()


def test_engine_dropdowns_reflect_db(monkeypatch):
    app = build_test_app(monkeypatch)
    c = app.client
    try:
        c.post("/api/ai-models", json={"label": "WriteX", "model": "wx", "scope": "generation"})
        c.post("/api/ai-models", json={"label": "FmtY", "model": "fy", "scope": "ai_format"})
        we = c.get("/api/generation/ai-engines").json()
        fe = c.get("/api/generation/format-engines").json()
        assert any(e["model"] == "wx" for e in we)
        assert any(e["model"] == "fy" for e in fe)
        # 下拉只下发 label/model，绝无密钥字段
        assert all(set(e.keys()) <= {"label", "model"} for e in we)
    finally:
        app.cleanup()
