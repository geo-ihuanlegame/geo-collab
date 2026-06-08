import uuid

import pytest

from server.tests.utils import build_test_app


def _write_storage_state(data_dir, account_key: str) -> None:
    state_dir = data_dir / "browser_states" / "toutiao" / account_key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")


def _create_account(client, data_dir, account_key: str, display_name: str) -> int:
    """复用 test_tasks_api.py 的做法：写 storage_state + /api/accounts/toutiao/login（不开浏览器）。"""
    _write_storage_state(data_dir, account_key)
    resp = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": display_name, "account_key": account_key, "use_browser": False},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _set_review_status(test_app, article_id: int, status: str) -> None:
    from server.app.modules.articles.models import Article

    with test_app.session_factory() as db:
        article = db.get(Article, article_id)
        assert article is not None
        article.review_status = status
        db.commit()


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


@pytest.mark.mysql
def test_distribute_node_creates_round_robin_task_for_approved_group(monkeypatch):
    """覆盖 *非默认* 的手动 group_id-only 分发路径。

    注意：#45/#46 之后，distribute 的默认行为是透传上游 article_ids → article_round_robin。
    本用例通过 inputMapping 只转发 group_id（不透传 article_ids），刻意走 group_round_robin
    分支，因此它验证的是手动分组路径，*不是* 当前默认链路（默认链路见
    test_auto_distribute.py 的 article_ids 透传用例）。
    """
    from server.app.modules.tasks.models import PublishTask

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        # 1) 造 approved 文章 + 分组 + 账号（参照 test_tasks_api.py 的夹具写法）
        #    新建文章默认 review_status=approved；走 /api/article-groups + items 成组。
        art1 = _make_article(client, "甲")
        art2 = _make_article(client, "乙")
        acc1 = _create_account(client, test_app.data_dir, "account-a", "Account A")

        group = client.post("/api/article-groups", json={"name": "已审核分组"}).json()
        g = group["id"]
        upd = client.put(
            f"/api/article-groups/{g}/items",
            json={
                "items": [
                    {"article_id": art1, "sort_order": 10},
                    {"article_id": art2, "sort_order": 20},
                ]
            },
        )
        assert upd.status_code == 200, upd.text

        # 2) 建 article_group_source(g) -> distribute(account_ids=[acc1]) pipeline
        snapshot = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "article_group_source",
                    "name": "源",
                    "node_index": 0,
                    "config": {"group_id": g},
                    "flow_meta": None,
                },
                {
                    "node_type": "distribute",
                    "name": "分发",
                    "node_index": 1,
                    "config": {"account_ids": [acc1]},
                    "flow_meta": {"inputMapping": [{"from": "group_id", "to": "group_id"}]},
                },
            ],
        }
        pid = client.post("/api/pipelines", json={"name": "分发流"}).json()["id"]
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
        assert run["status"] == "done", run
        assert run["node_results"]["1"].get("task_id")
        with test_app.session_factory() as db:
            tasks = db.query(PublishTask).all()
            assert any(t.task_type == "group_round_robin" for t in tasks)
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_mark_pending_and_group_fallback_suffix_is_stable(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup
    from server.app.modules.articles.service import mark_pending_and_group

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        a1 = _make_article(client, "甲")
        a2 = _make_article(client, "乙")
        with test_app.session_factory() as db:
            uid = db.query(Article).first().user_id
        gid1 = mark_pending_and_group(
            test_app.session_factory,
            article_ids=[a1],
            user_id=uid,
            base_name="撞名组",
            fallback_suffix="#101",
        )
        gid2 = mark_pending_and_group(
            test_app.session_factory,
            article_ids=[a2],
            user_id=uid,
            base_name="撞名组",
            fallback_suffix="#202",
        )
        assert gid1 is not None and gid2 is not None and gid1 != gid2
        with test_app.session_factory() as db:
            names = {g.name for g in db.query(ArticleGroup).all()}
            assert "撞名组" in names and "撞名组 #202" in names
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_distribute_node_fails_when_group_has_pending(monkeypatch):
    """手动 group_id-only 路径下的审核门禁（非默认链路，见上一个用例的说明）。

    默认链路下 article_group_source 已把 pending 文章过滤出 article_ids 子集，distribute
    不会看到它；本用例靠只转发 group_id 的 inputMapping 才能让 distribute 自己去拉整组并
    撞上 pending。article_ids 透传路径上的审核门禁另由 Block D 的用例覆盖。
    """
    from server.app.modules.tasks.models import PublishTask

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        # 同上夹具，但一篇 article.review_status="pending"（未过审）
        art1 = _make_article(client, "甲")
        art2 = _make_article(client, "乙")
        _set_review_status(test_app, art2, "pending")
        acc1 = _create_account(client, test_app.data_dir, "account-a", "Account A")

        group = client.post("/api/article-groups", json={"name": "含未审分组"}).json()
        g = group["id"]
        upd = client.put(
            f"/api/article-groups/{g}/items",
            json={
                "items": [
                    {"article_id": art1, "sort_order": 10},
                    {"article_id": art2, "sort_order": 20},
                ]
            },
        )
        assert upd.status_code == 200, upd.text

        snapshot = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "article_group_source",
                    "name": "源",
                    "node_index": 0,
                    "config": {"group_id": g},
                    "flow_meta": None,
                },
                {
                    "node_type": "distribute",
                    "name": "分发",
                    "node_index": 1,
                    "config": {"account_ids": [acc1]},
                    "flow_meta": {"inputMapping": [{"from": "group_id", "to": "group_id"}]},
                },
            ],
        }
        pid = client.post("/api/pipelines", json={"name": "门禁流"}).json()["id"]
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
        assert run["status"] == "failed", run
        with test_app.session_factory() as db:
            assert db.query(PublishTask).count() == 0
    finally:
        test_app.cleanup()
