import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_node_types_includes_ai_illustrate(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        r = app.client.get("/api/pipelines/node-types")
        assert r.status_code == 200, r.text
        data = r.json()
        types = {nt["type"]: nt for nt in data["node_types"]}
        assert "ai_illustrate" in types
        node = types["ai_illustrate"]
        assert node["label"] == "AI配图"
        fields = {f["key"]: f for f in node["config_schema"]}
        assert {"main_category_id", "web_fallback"} <= fields.keys()
        assert fields["main_category_id"]["type"] == "stock_category_main"
        # 联网兜底为开关（先存配置，搜图后续做）
        assert fields["web_fallback"]["type"] == "toggle"
        # ai_illustrate 执行器已在 PR2 注册
        assert "ai_illustrate" in data["registered"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_generate_model_field_is_ai_engine(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        r = app.client.get("/api/pipelines/node-types")
        assert r.status_code == 200, r.text
        types = {nt["type"]: nt for nt in r.json()["node_types"]}
        fields = {f["key"]: f for f in types["ai_generate"]["config_schema"]}
        assert fields["model"]["type"] == "ai_engine"
    finally:
        app.cleanup()
