import uuid

import pytest

from server.tests.utils import build_test_app


def _make_article(client, title="文章") -> int:
    resp = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": {"type": "doc", "content": []},
            "content_html": "<p>x</p>",
            "plain_text": "x",
            "word_count": 1,
            "status": "ready",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.mysql
def test_mark_pending_and_group_sets_pending_and_groups(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.articles.service import mark_pending_and_group

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        a1 = _make_article(client, "甲")
        a2 = _make_article(client, "乙")
        # 新建文章默认 approved；helper 应翻成 pending 并成组
        with test_app.session_factory() as db:
            uid = db.query(Article).first().user_id
        gid = mark_pending_and_group(
            test_app.session_factory, article_ids=[a1, a2], user_id=uid, base_name="测试组"
        )
        assert gid is not None
        with test_app.session_factory() as db:
            assert db.get(Article, a1).review_status == "pending"
            assert db.get(Article, a2).review_status == "pending"
            grp = db.get(ArticleGroup, gid)
            assert grp is not None and grp.name == "测试组"
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert {it.article_id for it in items} == {a1, a2}
    finally:
        test_app.cleanup()


def _make_generation_template(client) -> int:
    r = client.post(
        "/api/prompt-templates",
        json={"name": "模板", "content": "写：", "scope": "generation"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.mark.mysql
def test_pipeline_run_marks_articles_pending_and_groups(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroupItem
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    # 让 ai_generate 真造文章（默认 approved），返回其 id
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
                    client_request_id=str(uuid.uuid4()),
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
        tpl = _make_generation_template(client)
        pid = client.post("/api/pipelines", json={"name": "生成流"}).json()["id"]
        snapshot = {
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
                    "config": {"prompt_template_id": tpl, "count": 2},
                    "flow_meta": {
                        "inputMapping": [{"from": "question_text", "to": "question_text"}]
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
        assert run["status"] == "done"
        assert len(run["article_ids"]) == 2
        with test_app.session_factory() as db:
            for aid in run["article_ids"]:
                assert db.get(Article, aid).review_status == "pending"
            grouped = (
                db.query(ArticleGroupItem)
                .filter(ArticleGroupItem.article_id.in_(run["article_ids"]))
                .all()
            )
            assert len(grouped) == 2  # 都进了某个分组
    finally:
        test_app.cleanup()
