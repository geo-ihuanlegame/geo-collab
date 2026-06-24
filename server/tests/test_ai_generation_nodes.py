import pytest

from server.tests.utils import build_test_app


def _make_pool_with_items(app, items):
    """items: list[(category, question_text, source_active)]. 返回 pool_id + user_id。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool

    with app.session_factory() as db:
        from server.app.modules.system.models import User

        uid = db.query(User).first().id
        pool = QuestionPool(user_id=uid, name="测试池")
        db.add(pool)
        db.flush()
        for i, (cat, text, active) in enumerate(items):
            db.add(
                QuestionItem(
                    pool_id=pool.id,
                    record_id=f"r{i}",
                    fields={},
                    category=cat,
                    question_text=text,
                    source_active=active,
                )
            )
        db.commit()
        return pool.id, uid


@pytest.mark.mysql
def test_question_source_picks_type_and_active(monkeypatch):
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.question_source import run_question_source

    app = build_test_app(monkeypatch)
    try:
        pool_id, uid = _make_pool_with_items(
            app,
            [
                ("美食", "怎么做红烧肉", True),
                ("美食", "怎么做糖醋排骨", True),
                ("旅游", "去哪玩", True),
                ("美食", "停用的问题", False),
            ],
        )
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"pool_id": pool_id, "question_type": "美食"},
            inputs={},
            upstream={},
        )
        res = run_question_source(ctx)
        assert "红烧肉" in res.output["question_text"]
        assert "糖醋排骨" in res.output["question_text"]
        assert "去哪玩" not in res.output["question_text"]
        assert "停用" not in res.output["question_text"]
        assert res.output["question_count"] == 2
        # 无匹配类型 → 空 question_text，不报错
        ctx2 = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"pool_id": pool_id, "question_type": "不存在"},
            inputs={},
            upstream={},
        )
        assert run_question_source(ctx2).output["question_text"] == ""
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_question_source_empty_type_pulls_whole_pool(monkeypatch):
    """空 question_type → 取整池所有 source_active 问题（不按类型过滤）。"""
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.question_source import run_question_source

    app = build_test_app(monkeypatch)
    try:
        pool_id, uid = _make_pool_with_items(
            app,
            [
                ("美食", "红烧肉", True),
                ("旅游", "去哪玩", True),
                (None, "没有分类的问题", True),
                ("美食", "停用的", False),
            ],
        )
        # question_type 缺省（"全部类型"）
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"pool_id": pool_id},
            inputs={},
            upstream={},
        )
        res = run_question_source(ctx)
        assert res.output["question_count"] == 3
        for kw in ("红烧肉", "去哪玩", "没有分类的问题"):
            assert kw in res.output["question_text"]
        assert "停用" not in res.output["question_text"]
        # 显式传空字符串等价于"全部类型"
        ctx_blank = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"pool_id": pool_id, "question_type": ""},
            inputs={},
            upstream={},
        )
        assert run_question_source(ctx_blank).output["question_count"] == 3
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_question_source_uncategorized_sentinel(monkeypatch):
    """question_type == "__uncategorized__" → 只取 category 为 NULL 的问题。"""
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.question_source import run_question_source

    app = build_test_app(monkeypatch)
    try:
        pool_id, uid = _make_pool_with_items(
            app,
            [
                ("美食", "红烧肉", True),
                (None, "未分类甲", True),
                (None, "未分类乙", True),
            ],
        )
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"pool_id": pool_id, "question_type": "__uncategorized__"},
            inputs={},
            upstream={},
        )
        res = run_question_source(ctx)
        assert res.output["question_count"] == 2
        assert "未分类甲" in res.output["question_text"]
        assert "未分类乙" in res.output["question_text"]
        assert "红烧肉" not in res.output["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_question_source_requires_pool_id(monkeypatch):
    """缺 pool_id → ValidationError（question_type 不再必填）。"""
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.question_source import run_question_source
    from server.app.shared.errors import ValidationError

    app = build_test_app(monkeypatch)
    try:
        with app.session_factory() as db:
            from server.app.modules.system.models import User

            uid = db.query(User).first().id
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"question_type": "美食"},
            inputs={},
            upstream={},
        )
        with pytest.raises(ValidationError):
            run_question_source(ctx)
    finally:
        app.cleanup()


def _make_gen_template(app, uid, content="写：", enabled=True):
    from server.app.modules.prompt_templates.models import PromptTemplate

    with app.session_factory() as db:
        t = PromptTemplate(
            name="模板", content=content, scope="generation", user_id=uid, is_enabled=enabled
        )
        db.add(t)
        db.commit()
        return t.id


@pytest.mark.mysql
def test_ai_compose_generates_with_random_template(monkeypatch):
    calls = {"n": 0}

    def _fake_generate(
        *, session_factory, user_id, template_content, question_text, model=None, **_
    ):
        import uuid

        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article

        calls["n"] += 1
        db = session_factory()
        try:
            art = create_article(
                db,
                user_id,
                ArticleCreate(
                    title=f"A{calls['n']}",
                    content_json={"type": "doc", "content": []},
                    content_html="<p>x</p>",
                    plain_text="x",
                    word_count=1,
                    client_request_id=str(uuid.uuid4()),
                ),
            )
            db.commit()
            return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt", _fake_generate
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        with app.session_factory() as db:
            from server.app.modules.system.models import User

            uid = db.query(User).first().id
        t1 = _make_gen_template(app, uid)
        t2 = _make_gen_template(app, uid)
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"ai_engine": None, "prompt_template_ids": [t1, t2], "count": 3},
            inputs={"question_text": "1. 怎么做红烧肉"},
            upstream={},
        )
        res = run_ai_compose(ctx)
        assert len(res.output["article_ids"]) == 3
        assert res.article_ids == res.output["article_ids"]
        # 空问题 → 跳过
        ctx_empty = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"prompt_template_ids": [t1], "count": 2},
            inputs={"question_text": ""},
            upstream={},
        )
        r2 = run_ai_compose(ctx_empty)
        assert r2.output["article_ids"] == [] and r2.output.get("skipped")
        # 模板全无效 → errors 有值、article_ids 空、不抛
        bad = _make_gen_template(app, uid, enabled=False)
        ctx_bad = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"prompt_template_ids": [bad], "count": 1},
            inputs={"question_text": "1. q"},
            upstream={},
        )
        r3 = run_ai_compose(ctx_bad)
        assert r3.output["article_ids"] == [] and r3.output["errors"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_compose_forwards_provenance_to_writer(monkeypatch):
    """溯源接线：ai_compose 必须把「智能体名(pipeline_name) + 实际所选模板名」转发给生文函数。"""
    captured: dict = {}

    def _capturing_generate(
        *,
        session_factory,
        user_id,
        template_content,
        question_text,
        model=None,
        source_agent_name=None,
        source_template_name=None,
        **_,
    ):
        import uuid

        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article

        captured["agent"] = source_agent_name
        captured["template"] = source_template_name
        db = session_factory()
        try:
            art = create_article(
                db,
                user_id,
                ArticleCreate(
                    title="A",
                    content_json={"type": "doc", "content": []},
                    content_html="<p>x</p>",
                    plain_text="x",
                    word_count=1,
                    client_request_id=str(uuid.uuid4()),
                ),
            )
            db.commit()
            return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt",
        _capturing_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose
        from server.app.modules.pipelines.nodes.base import NodeRunContext
        from server.app.modules.prompt_templates.models import PromptTemplate
        from server.app.modules.system.models import User

        with app.session_factory() as db:
            uid = db.query(User).first().id
            tpl = PromptTemplate(
                name="游戏榜单清单",
                content="写：",
                scope="generation",
                user_id=uid,
                is_enabled=True,
            )
            db.add(tpl)
            db.commit()
            tpl_id, tpl_name = tpl.id, tpl.name

        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"prompt_template_ids": [tpl_id], "count": 1},
            inputs={"question_text": "1. q"},
            upstream={},
            pipeline_name="生文自动",
        )
        res = run_ai_compose(ctx)
        assert len(res.output["article_ids"]) == 1
        assert captured["agent"] == "生文自动", "智能体名(pipeline_name)应透传给生文函数"
        assert captured["template"] == tpl_name == "游戏榜单清单", "实际所选模板名应透传"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_to_review_marks_pending_and_groups(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        from server.app.modules.articles.models import Article, ArticleGroupItem
        from server.app.modules.pipelines.nodes.base import NodeRunContext
        from server.app.modules.pipelines.nodes.to_review import run_to_review

        def _mk(title):
            r = client.post(
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
            return r.json()["id"]

        a1, a2 = _mk("甲"), _mk("乙")
        with app.session_factory() as db:
            uid = db.query(Article).first().user_id
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"group_name": "今日生成"},
            inputs={"article_ids": [a1, a2]},
            upstream={},
        )
        res = run_to_review(ctx)
        gid = res.output["group_id"]
        assert gid is not None
        with app.session_factory() as db:
            assert db.get(Article, a1).review_status == "pending"
            assert db.get(Article, a2).review_status == "pending"
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert {it.article_id for it in items} == {a1, a2}
        # 空 article_ids → 跳过且不建组
        ctx_empty = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={},
            inputs={"article_ids": []},
            upstream={},
        )
        assert run_to_review(ctx_empty).output.get("skipped")
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_end_to_end_generate_into_review(monkeypatch):
    def _fake_generate(
        *, session_factory, user_id, template_content, question_text, model=None, **_
    ):
        import uuid

        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article

        # 断言上游问题确实传进来了
        assert "红烧肉" in question_text
        db = session_factory()
        try:
            art = create_article(
                db,
                user_id,
                ArticleCreate(
                    title="成品",
                    content_json={"type": "doc", "content": []},
                    content_html="<p>x</p>",
                    plain_text="x",
                    word_count=1,
                    client_request_id=str(uuid.uuid4()),
                ),
            )
            db.commit()
            return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt", _fake_generate
    )
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pool_id, uid = _make_pool_with_items(app, [("美食", "怎么做红烧肉", True)])
        tpl = _make_gen_template(app, uid)
        snap = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "question_source",
                    "name": "问题源",
                    "node_index": 0,
                    "config": {"pool_id": pool_id, "question_type": "美食"},
                    "flow_meta": None,
                },
                {
                    "node_type": "ai_compose",
                    "name": "创作",
                    "node_index": 1,
                    "config": {"prompt_template_ids": [tpl], "count": 2},
                    "flow_meta": {
                        "inputMapping": [{"from": "question_text", "to": "question_text"}]
                    },
                },
                {
                    "node_type": "to_review",
                    "name": "进未审核",
                    "node_index": 2,
                    "config": {"group_name": "端到端"},
                    "flow_meta": {"inputMapping": [{"from": "article_ids", "to": "article_ids"}]},
                },
            ],
        }
        pid = client.post(
            "/api/pipelines", json={"name": "AI生文智能体", "type": "generation"}
        ).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
        client.post(f"/api/pipelines/{pid}/publish", json={})
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline

        with app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=pid, user_id=p.user_id)
            db.commit()
            rid = run.id
        run_pipeline(rid, app.session_factory)

        run = client.get(f"/api/pipelines/runs/{rid}").json()
        assert run["status"] == "done", run
        assert len(run["article_ids"]) == 2
        # 文章出现在"未审核"列表
        listed = client.get("/api/articles?review_status=pending&limit=50").json()
        listed_ids = {
            a["id"] for a in (listed if isinstance(listed, list) else listed.get("items", []))
        }
        assert set(run["article_ids"]).issubset(listed_ids)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_executor_skips_autogroup_when_to_review_present(monkeypatch):
    def _fake_generate(
        *, session_factory, user_id, template_content, question_text, model=None, **_
    ):
        import uuid

        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article

        db = session_factory()
        try:
            art = create_article(
                db,
                user_id,
                ArticleCreate(
                    title="A",
                    content_json={"type": "doc", "content": []},
                    content_html="<p>x</p>",
                    plain_text="x",
                    word_count=1,
                    client_request_id=str(uuid.uuid4()),
                ),
            )
            db.commit()
            return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt", _fake_generate
    )
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pool_id, uid = _make_pool_with_items(app, [("美食", "怎么做红烧肉", True)])
        tpl = _make_gen_template(app, uid)
        snap = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "question_source",
                    "name": "问题源",
                    "node_index": 0,
                    "config": {"pool_id": pool_id, "question_type": "美食"},
                    "flow_meta": None,
                },
                {
                    "node_type": "ai_compose",
                    "name": "创作",
                    "node_index": 1,
                    "config": {"prompt_template_ids": [tpl], "count": 2},
                    "flow_meta": {
                        "inputMapping": [{"from": "question_text", "to": "question_text"}]
                    },
                },
                {
                    "node_type": "to_review",
                    "name": "进未审核",
                    "node_index": 2,
                    "config": {"group_name": "今日"},
                    "flow_meta": {"inputMapping": [{"from": "article_ids", "to": "article_ids"}]},
                },
            ],
        }
        pid = client.post("/api/pipelines", json={"name": "生成流"}).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
        client.post(f"/api/pipelines/{pid}/publish", json={})
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline

        with app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=pid, user_id=p.user_id)
            db.commit()
            rid = run.id
        run_pipeline(rid, app.session_factory)

        run = client.get(f"/api/pipelines/runs/{rid}").json()
        assert run["status"] == "done", run
        with app.session_factory() as db:
            from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem

            arts = run["article_ids"]
            assert len(arts) == 2
            for aid in arts:
                assert db.get(Article, aid).review_status == "pending"
            # 关键：只成一个组（执行器未重复成组）
            group_ids = {
                it.group_id
                for it in db.query(ArticleGroupItem)
                .filter(ArticleGroupItem.article_id.in_(arts))
                .all()
            }
            assert len(group_ids) == 1
            assert db.query(ArticleGroup).filter(ArticleGroup.id.in_(group_ids)).count() == 1
    finally:
        app.cleanup()


def _fake_generate_factory():
    def _fake_generate(
        *, session_factory, user_id, template_content, question_text, model=None, **_
    ):
        import uuid

        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article

        db = session_factory()
        try:
            art = create_article(
                db,
                user_id,
                ArticleCreate(
                    title="A",
                    content_json={"type": "doc", "content": []},
                    content_html="<p>x</p>",
                    plain_text="x",
                    word_count=1,
                    client_request_id=str(uuid.uuid4()),
                ),
            )
            db.commit()
            return art.id
        finally:
            db.close()

    return _fake_generate


@pytest.mark.mysql
def test_executor_groups_orphans_when_to_review_present_but_did_not_group(monkeypatch):
    """缺陷 2：to_review 节点存在不等于真的成了组。

    to_review 被 condition 跳过（不执行）→ 不成组。执行器不能因为"存在 to_review 节点"
    就放手，否则 ai_compose 产的文章成孤儿。期望：执行器兜底把这些文章成组（一个组），不留孤儿。

    （注：自从"无 inputMapping 默认透传上游"后，漏配映射不再产生孤儿——to_review 会自动拿到
    article_ids 并成组。故这里改用 condition 跳过 to_review 来制造"节点存在但未成组"的孤儿场景。）
    """
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt",
        _fake_generate_factory(),
    )
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pool_id, uid = _make_pool_with_items(app, [("美食", "怎么做红烧肉", True)])
        tpl = _make_gen_template(app, uid)
        snap = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "question_source",
                    "name": "问题源",
                    "node_index": 0,
                    "config": {"pool_id": pool_id, "question_type": "美食"},
                    "flow_meta": None,
                },
                {
                    "node_type": "ai_compose",
                    "name": "创作",
                    "node_index": 1,
                    "config": {"prompt_template_ids": [tpl], "count": 2},
                    "flow_meta": {
                        "inputMapping": [{"from": "question_text", "to": "question_text"}]
                    },
                },
                {
                    # 用 condition 跳过 to_review：节点存在但不执行成组 → 测执行器兜底成组
                    "node_type": "to_review",
                    "name": "进未审核",
                    "node_index": 2,
                    "config": {"group_name": "今日"},
                    "flow_meta": {
                        "condition": {"field": "__never__", "op": "eq", "value": "__skip__"}
                    },
                },
            ],
        }
        pid = client.post("/api/pipelines", json={"name": "孤儿兜底"}).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
        client.post(f"/api/pipelines/{pid}/publish", json={})
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline

        with app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=pid, user_id=p.user_id)
            db.commit()
            rid = run.id
        run_pipeline(rid, app.session_factory)

        run = client.get(f"/api/pipelines/runs/{rid}").json()
        arts = run["article_ids"]
        assert len(arts) == 2
        with app.session_factory() as db:
            from server.app.modules.articles.models import Article, ArticleGroupItem

            for aid in arts:
                assert db.get(Article, aid).review_status == "pending"
            # 关键：文章被执行器兜底成组，不留孤儿
            group_ids = {
                it.group_id
                for it in db.query(ArticleGroupItem)
                .filter(ArticleGroupItem.article_id.in_(arts))
                .all()
            }
            assert len(group_ids) == 1, f"孤儿未成组: {run}"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_grouping_failure_surfaces_partial_failed_not_silent_done(monkeypatch):
    """缺陷 1：成组真失败时不能静默报 done。

    mark_pending_and_group best-effort 失败返回 None。含 to_review 时，原代码短路掉
    执行器自带的 gid-None 降级保护、to_review 自己又不上报 → run 假报 done。
    期望：成组失败 → run 降级 partial_failed 并写明原因。
    """
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt",
        _fake_generate_factory(),
    )
    # 让 to_review 与执行器兜底两处成组都失败（返回 None）
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.to_review.mark_pending_and_group",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "server.app.modules.pipelines.executor.mark_pending_and_group",
        lambda *a, **k: None,
    )
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pool_id, uid = _make_pool_with_items(app, [("美食", "怎么做红烧肉", True)])
        tpl = _make_gen_template(app, uid)
        snap = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "question_source",
                    "name": "问题源",
                    "node_index": 0,
                    "config": {"pool_id": pool_id, "question_type": "美食"},
                    "flow_meta": None,
                },
                {
                    "node_type": "ai_compose",
                    "name": "创作",
                    "node_index": 1,
                    "config": {"prompt_template_ids": [tpl], "count": 2},
                    "flow_meta": {
                        "inputMapping": [{"from": "question_text", "to": "question_text"}]
                    },
                },
                {
                    "node_type": "to_review",
                    "name": "进未审核",
                    "node_index": 2,
                    "config": {"group_name": "今日"},
                    "flow_meta": {"inputMapping": [{"from": "article_ids", "to": "article_ids"}]},
                },
            ],
        }
        pid = client.post("/api/pipelines", json={"name": "成组失败"}).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
        client.post(f"/api/pipelines/{pid}/publish", json={})
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline

        with app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=pid, user_id=p.user_id)
            db.commit()
            rid = run.id
        run_pipeline(rid, app.session_factory)

        run = client.get(f"/api/pipelines/runs/{rid}").json()
        assert run["status"] == "partial_failed", run
        assert run["error_message"] and "成组" in run["error_message"], run
    finally:
        app.cleanup()
