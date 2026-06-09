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
        keys = {f["key"] for f in node["config_schema"]}
        assert {"main_category_id", "include_companion"} <= keys
        main_field = next(f for f in node["config_schema"] if f["key"] == "main_category_id")
        assert main_field["type"] == "stock_category_main"
        # ai_illustrate 执行器已在 PR2 注册
        assert "ai_illustrate" in data["registered"]
    finally:
        app.cleanup()
