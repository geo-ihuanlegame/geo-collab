# server/tests/test_pipeline_service.py
import pytest

from server.tests.utils import build_test_app


def _create_generation_template(client) -> int:
    resp = client.post(
        "/api/prompt-templates",
        json={
            "name": "测试模板",
            "content": "写一篇关于：",
            "scope": "generation",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _draft_snapshot(tpl_id: int) -> dict:
    return {
        "schemaVersion": 1,
        "nodes": [
            {
                "node_type": "input",
                "name": "源",
                "node_index": 0,
                "config": {"question_text": "主题"},
                "flow_meta": None,
            },
            {
                "node_type": "ai_generate",
                "name": "生文",
                "node_index": 1,
                "config": {"prompt_template_id": tpl_id, "count": 1},
                "flow_meta": {"inputMapping": [{"from": "question_text", "to": "question_text"}]},
            },
        ],
    }


@pytest.mark.mysql
def test_publish_draft_version_no_is_sequential(monkeypatch):
    # 加行锁串行化后，对同一 pipeline 连续发布两次草稿，version_no 仍应依次为 1、2
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        tpl_id = _create_generation_template(client)
        pid = client.post("/api/pipelines", json={"name": "版本流"}).json()["id"]

        # 第一次发布 -> version_no == 1
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": _draft_snapshot(tpl_id)})
        r1 = client.post(f"/api/pipelines/{pid}/publish", json={"remark": "v1"})
        assert r1.status_code == 200, r1.text
        assert r1.json()["version_no"] == 1

        # 第二次发布（publish 已清空草稿，需先再存一次草稿）-> version_no == 2
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": _draft_snapshot(tpl_id)})
        r2 = client.post(f"/api/pipelines/{pid}/publish", json={"remark": "v2"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["version_no"] == 2

        # 版本列表应有两条，版本号 2、1
        vers = client.get(f"/api/pipelines/{pid}/versions").json()
        assert [v["version_no"] for v in vers] == [2, 1]
    finally:
        test_app.cleanup()
