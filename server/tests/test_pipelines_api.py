# server/tests/test_pipelines_api.py
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


@pytest.mark.mysql
def test_pipeline_draft_publish_version_and_run(monkeypatch):
    # monkeypatch 掉真实 LLM 调用：造一篇真实文章（默认 approved）并返回其 id，
    # 这样运行结束后的 pending+成组能成功、run 才会是 done（返回假 id 会成组失败 → partial_failed）。
    import uuid as _uuid

    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
        db = session_factory()
        try:
            art = create_article(
                db,
                user_id,
                ArticleCreate(
                    title="AI",
                    content_json={"type": "doc", "content": []},
                    content_html="<p>a</p>",
                    plain_text="a",
                    word_count=1,
                    client_request_id=str(_uuid.uuid4()),
                ),
            )
            db.commit()
            return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _fake_generate,
    )
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        tpl_id = _create_generation_template(client)

        # 1) 新建 pipeline
        r = client.post("/api/pipelines", json={"name": "我的流程"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        assert r.json()["has_draft"] is False

        # 2) 存草稿：input -> ai_generate，inputMapping 传 question_text
        snapshot = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "input",
                    "name": "源",
                    "node_index": 0,
                    "config": {"question_text": "如何养生"},
                    "flow_meta": None,
                },
                {
                    "node_type": "ai_generate",
                    "name": "生文",
                    "node_index": 1,
                    "config": {"prompt_template_id": tpl_id, "count": 2},
                    "flow_meta": {
                        "schemaVersion": 1,
                        "inputMapping": [{"from": "question_text", "to": "question_text"}],
                    },
                },
            ],
        }
        r = client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        assert r.status_code == 200, r.text

        # 草稿不影响 live：此时无已发布节点 -> 运行应 400
        r = client.post(f"/api/pipelines/{pid}/runs")
        assert r.status_code == 400

        # 3) 发布 -> 版本号 1，live 节点出现
        r = client.post(f"/api/pipelines/{pid}/publish", json={"remark": "v1"})
        assert r.status_code == 200, r.text
        assert r.json()["version_no"] == 1
        detail = client.get(f"/api/pipelines/{pid}").json()
        assert detail["has_draft"] is False
        assert len(detail["nodes"]) == 2

        # 4) 版本列表
        vers = client.get(f"/api/pipelines/{pid}/versions").json()
        assert len(vers) == 1 and vers[0]["version_no"] == 1

        # 5) 运行（测试内同步执行）
        from server.app.modules.pipelines.executor import create_run, run_pipeline

        with test_app.session_factory() as db:
            from server.app.modules.pipelines.models import Pipeline

            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=p.id, user_id=p.user_id)
            db.commit()
            run_id = run.id
        run_pipeline(run_id, test_app.session_factory)

        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["status"] == "done", run
        assert len(run["article_ids"]) == 2

        # 6) 回溯：先再发布一版以制造历史，再回溯 v1 到草稿
        r = client.post(f"/api/pipelines/{pid}/publish", json={"remark": "v2"})
        # 注意：publish 需要 has_draft；此处先存一次草稿再发布
        # （上一次 publish 已清空草稿，因此这里应 400）
        assert r.status_code == 400

        v1_id = vers[0]["id"]
        r = client.post(f"/api/pipelines/versions/{v1_id}/rollback")
        assert r.status_code == 200
        assert client.get(f"/api/pipelines/{pid}").json()["has_draft"] is True
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_pipeline_skip_condition(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        lambda **kwargs: 999,
    )
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        tpl_id = _create_generation_template(client)
        pid = client.post("/api/pipelines", json={"name": "条件流程"}).json()["id"]
        snapshot = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "input",
                    "name": "源",
                    "node_index": 0,
                    "config": {"question_text": "x"},
                    "flow_meta": None,
                },
                {
                    "node_type": "ai_generate",
                    "name": "生文",
                    "node_index": 1,
                    "config": {"prompt_template_id": tpl_id, "count": 1},
                    "flow_meta": {
                        "condition": {"field": "question_text", "op": "eq", "value": "不匹配"}
                    },
                },
            ],
        }
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        client.post(f"/api/pipelines/{pid}/publish", json={})

        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline

        with test_app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=p.id, user_id=p.user_id)
            db.commit()
            run_id = run.id
        run_pipeline(run_id, test_app.session_factory)

        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["article_ids"] == []  # ai_generate 被跳过
        assert run["node_results"]["1"] == {"skipped": True}
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_delete_pipeline_rejected_when_active_run(monkeypatch):
    """delete_pipeline raises ConflictError when the pipeline has a pending/running run."""
    import server.app.modules.pipelines.service as svc
    from server.app.modules.pipelines.models import PipelineRun
    from server.app.modules.system.models import User
    from server.app.shared.errors import ConflictError

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            admin_id = db.query(User).filter(User.username == "testadmin").first().id

            p = svc.create_pipeline(
                db,
                user_id=admin_id,
                name="待删流程",
                description=None,
            )
            db.flush()

            run = PipelineRun(
                pipeline_id=p.id,
                user_id=admin_id,
                status="running",
                node_results={},
                article_ids=[],
            )
            db.add(run)
            db.commit()
            db.refresh(p)

            with pytest.raises(ConflictError):
                svc.delete_pipeline(db, p)
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_read_pipeline_with_null_tags(monkeypatch):
    """_to_read should not raise when tags is NULL in the DB (returns empty list)."""
    test_app = build_test_app(monkeypatch)
    try:
        from sqlalchemy import text

        import server.app.modules.pipelines.service as svc
        from server.app.modules.pipelines.router import _to_read
        from server.app.modules.system.models import User

        with test_app.session_factory() as db:
            admin_id = db.query(User).filter(User.username == "testadmin").first().id

            p = svc.create_pipeline(db, user_id=admin_id, name="t", description=None)
            db.commit()
            pid = p.id

            # Force tags to NULL bypassing ORM (disable strict mode to allow NULL update)
            db.execute(text("SET sql_mode=''"))
            db.execute(text("UPDATE pipelines SET tags=NULL WHERE id=:i"), {"i": pid})
            db.commit()
            db.refresh(p)

            data = _to_read(db, p)
            assert data["tags"] == []
    finally:
        test_app.cleanup()
