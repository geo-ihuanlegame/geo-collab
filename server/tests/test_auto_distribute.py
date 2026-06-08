import pytest

from server.tests.utils import build_test_app


def _make_approved_article(client, title="文章"):
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
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _make_account(app, client, key="acc1", name="账号1"):
    """参照 test_pipeline_review_distribute.py 的账号夹具：写 storage_state + 创建账号。"""
    import json as _json
    from pathlib import Path

    state_dir = Path(app.data_dir) / "browser_states" / "toutiao" / key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text(
        _json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
    )
    r = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": name, "account_key": key, "use_browser": False},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.mysql
def test_article_round_robin_task_built(monkeypatch):
    from server.app.modules.tasks.models import PublishRecord, PublishTask
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1, a2, a3 = (_make_approved_article(client, t) for t in ("甲", "乙", "丙"))
        acc1 = _make_account(app, client, "k1", "号1")
        acc2 = _make_account(app, client, "k2", "号2")
        with app.session_factory() as db:
            from server.app.modules.articles.models import Article

            uid = db.get(Article, a1).user_id
            tc = TaskCreate(
                name="自动分发",
                task_type="article_round_robin",
                article_ids=[a1, a2, a3],
                accounts=[
                    TaskAccountInput(account_id=acc1, sort_order=0),
                    TaskAccountInput(account_id=acc2, sort_order=1),
                ],
                stop_before_publish=False,
            )
            task = create_task(db, uid, tc, role="admin")
            db.commit()
            tid = task.id
        with app.session_factory() as db:
            t = db.get(PublishTask, tid)
            assert t.task_type == "article_round_robin"
            recs = db.query(PublishRecord).filter(PublishRecord.task_id == tid).all()
            assert {r.article_id for r in recs} == {a1, a2, a3}  # 3 篇都派发
            assert len({r.account_id for r in recs}) == 2  # round-robin 到 2 账号
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_article_round_robin_empty_raises(monkeypatch):
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task
    from server.app.shared.errors import ClientError

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        acc1 = _make_account(app, client)
        with app.session_factory() as db:
            from server.app.modules.system.models import User

            uid = db.query(User).first().id
            tc = TaskCreate(
                name="空",
                task_type="article_round_robin",
                article_ids=[],
                accounts=[TaskAccountInput(account_id=acc1, sort_order=0)],
            )
            with pytest.raises(ClientError):
                create_task(db, uid, tc, role="admin")
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_approved_content_source_dedup_and_filter(monkeypatch):
    from server.app.modules.articles.models import Article
    from server.app.modules.pipelines.nodes.approved_content_source import (
        run_approved_content_source,
    )
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.tasks.models import PublishRecord
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1 = _make_approved_article(client, "已审1")
        a2 = _make_approved_article(client, "已审2")
        a3 = _make_approved_article(client, "已审已发")
        # a3 标记为已分发（造一条 PublishRecord）；a4 设为 pending（不该被取）
        a4 = _make_approved_article(client, "未审")
        acc1 = _make_account(app, client, "src1", "源账号1")
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id
            db.get(Article, a4).review_status = "pending"
            # 建真实 PublishTask + PublishRecord，满足外键约束
            tc = TaskCreate(
                name="标记已发",
                task_type="article_round_robin",
                article_ids=[a3],
                accounts=[TaskAccountInput(account_id=acc1, sort_order=0)],
                stop_before_publish=False,
            )
            task = create_task(db, uid, tc, role="admin")
            db.flush()
            # 取已建好的 record，把 status 改为 succeeded
            rec = db.query(PublishRecord).filter(PublishRecord.task_id == task.id).first()
            rec.status = "succeeded"
            db.commit()
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"limit": 10, "exclude_distributed": True},
            inputs={},
            upstream={},
        )
        res = run_approved_content_source(ctx)
        ids = set(res.output["article_ids"])
        assert a1 in ids and a2 in ids
        assert a3 not in ids  # 已分发被去重
        assert a4 not in ids  # pending 不取
        # exclude_distributed=False → a3 回来
        ctx2 = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"limit": 10, "exclude_distributed": False},
            inputs={},
            upstream={},
        )
        assert a3 in set(run_approved_content_source(ctx2).output["article_ids"])
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_distribute_consumes_article_ids_and_skips_empty(monkeypatch):
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.distribute_node import run_distribute
    from server.app.modules.tasks.models import PublishTask

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1 = _make_approved_article(client, "x1")
        a2 = _make_approved_article(client, "x2")
        acc1 = _make_account(app, client, "ka", "甲号")
        with app.session_factory() as db:
            from server.app.modules.articles.models import Article

            uid = db.get(Article, a1).user_id
        # 有 article_ids → 建 article_round_robin 任务
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"account_ids": [acc1]},
            inputs={"article_ids": [a1, a2]},
            upstream={},
        )
        res = run_distribute(ctx)
        assert res.output.get("task_id")
        with app.session_factory() as db:
            assert (
                db.query(PublishTask).filter(PublishTask.task_type == "article_round_robin").count()
                == 1
            )
        # 空 article_ids → 跳过、不建任务
        ctx_empty = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"account_ids": [acc1]},
            inputs={"article_ids": []},
            upstream={},
        )
        r2 = run_distribute(ctx_empty)
        assert r2.output.get("skipped")
        with app.session_factory() as db:
            assert db.query(PublishTask).count() == 1  # 没新建第二个
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_end_to_end_approved_to_distribute_dedup(monkeypatch):
    from server.app.modules.pipelines.executor import create_run, run_pipeline
    from server.app.modules.pipelines.models import Pipeline
    from server.app.modules.tasks.models import PublishRecord, PublishTask

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1 = _make_approved_article(client, "审1")
        a2 = _make_approved_article(client, "审2")
        acc1 = _make_account(app, client, "kk", "号")
        snap = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "approved_content_source",
                    "name": "已审核待发布",
                    "node_index": 0,
                    "config": {"limit": 50, "exclude_distributed": True},
                    "flow_meta": None,
                },
                {
                    "node_type": "distribute",
                    "name": "内容分发",
                    "node_index": 1,
                    "config": {"account_ids": [acc1]},
                    "flow_meta": {"inputMapping": [{"from": "article_ids", "to": "article_ids"}]},
                },
            ],
        }
        pid = client.post(
            "/api/pipelines", json={"name": "自动分发智能体", "type": "distribution"}
        ).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
        client.post(f"/api/pipelines/{pid}/publish", json={})

        def _run():
            with app.session_factory() as db:
                p = db.get(Pipeline, pid)
                run = create_run(db, pipeline_id=pid, user_id=p.user_id)
                db.commit()
                rid = run.id
            run_pipeline(rid, app.session_factory)
            return client.get(f"/api/pipelines/runs/{rid}").json()

        # 第一次：建任务，覆盖 a1/a2
        r1 = _run()
        assert r1["status"] == "done", r1
        with app.session_factory() as db:
            assert (
                db.query(PublishTask).filter(PublishTask.task_type == "article_round_robin").count()
                == 1
            )
            distributed = {rec.article_id for rec in db.query(PublishRecord).all()}
            assert {a1, a2}.issubset(distributed)

        # 第二次：a1/a2 已分发 → 源去重为空 → distribute 跳过 → run done、不建第二个任务
        r2 = _run()
        assert r2["status"] == "done", r2
        with app.session_factory() as db:
            assert db.query(PublishTask).count() == 1  # 仍只有 1 个
    finally:
        app.cleanup()


def _make_group(app, uid, article_ids):
    """直接用 ORM 造一个分组 + 条目，返回 group_id。"""
    from server.app.modules.articles.models import ArticleGroup, ArticleGroupItem

    with app.session_factory() as db:
        g = ArticleGroup(user_id=uid, name="组", version=1)
        db.add(g)
        db.flush()
        for i, aid in enumerate(article_ids):
            db.add(ArticleGroupItem(group_id=g.id, article_id=aid, sort_order=i))
        db.commit()
        return g.id


@pytest.mark.mysql
def test_distribute_prefers_article_ids_over_passthrough_group(monkeypatch):
    """article_group_source → distribute 默认透传时 group_id 与 article_ids 都在，
    distribute 必须优先 article_ids（走 article_round_robin、只发过滤后的子集），
    不能走分组路径重拉全组——否则架空源节点「已审+未分发」过滤，重复发布/未审即失败（#45）。"""
    from server.app.modules.articles.models import Article
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.distribute_node import run_distribute
    from server.app.modules.tasks.models import PublishRecord, PublishTask

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1 = _make_approved_article(client, "g1")
        a2 = _make_approved_article(client, "g2")
        acc1 = _make_account(app, client, "kg", "组号")
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id
        gid = _make_group(app, uid, [a1, a2])

        # 模拟源节点输出：组含 a1/a2，但「已审+未分发」子集只剩 a1（a2 已被源节点过滤掉）
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"account_ids": [acc1]},
            inputs={"group_id": gid, "article_ids": [a1]},  # group_id + 子集都透传
            upstream={},
        )
        res = run_distribute(ctx)
        with app.session_factory() as db:
            t = db.get(PublishTask, res.output["task_id"])
            assert t.task_type == "article_round_robin"  # 优先 article_ids，而非 group_round_robin
            assert t.group_id is None
            covered = {
                rec.article_id
                for rec in db.query(PublishRecord).filter(PublishRecord.task_id == t.id).all()
            }
            assert covered == {a1}  # 只发子集 a1，不重拉 a2

    finally:
        app.cleanup()


@pytest.mark.mysql
def test_distribute_empty_passthrough_skips(monkeypatch):
    """源节点选中组的「已审+未分发」子集为空（article_ids=[]）经透传到 distribute 时，
    应跳过（无新内容、done），不报错——定时自动分发跑完后不该每轮变红。
    （手动配置 group_id、无上游 article_ids 的真·空分组仍报错，见下一个测试。）"""
    from server.app.modules.articles.models import Article
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.distribute_node import run_distribute
    from server.app.modules.tasks.models import PublishTask

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1 = _make_approved_article(client, "x")
        acc1 = _make_account(app, client, "ke", "空号")
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id
        gid = _make_group(app, uid, [a1])

        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"account_ids": [acc1]},
            inputs={"group_id": gid, "article_ids": []},  # 子集为空（全已分发）
            upstream={},
        )
        res = run_distribute(ctx)
        assert res.output.get("skipped")  # 跳过、非报错
        with app.session_factory() as db:
            assert db.query(PublishTask).count() == 0  # 没建任务

    finally:
        app.cleanup()


@pytest.mark.mysql
def test_distribute_manual_empty_group_config_raises(monkeypatch):
    """手动把 distribute 配成 group_id（无上游 article_ids）指向空分组时，仍应报错。
    这是 article_ids is None（真·手动分组路径）的场景，保留 #37 对空分组的报错语义。"""
    from server.app.modules.articles.models import Article
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.distribute_node import run_distribute
    from server.app.shared.errors import ClientError, ValidationError

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1 = _make_approved_article(client, "x")
        acc1 = _make_account(app, client, "kem", "手动空号")
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id
        empty_gid = _make_group(app, uid, [])  # 空分组

        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"account_ids": [acc1], "group_id": empty_gid},  # 手动配置，无上游
            inputs={},
            upstream={},
        )
        with pytest.raises((ClientError, ValidationError)):
            run_distribute(ctx)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_end_to_end_group_source_to_distribute_honors_subset(monkeypatch):
    """#45 端到端回归：article_group_source → distribute。
    组里 a_ok 未分发、a_dup 已分发；distribute 必须只发 a_ok（article_round_robin），
    不能走分组路径重拉全组把已分发的 a_dup 重复发布。"""
    from server.app.modules.articles.models import Article
    from server.app.modules.pipelines.executor import create_run, run_pipeline
    from server.app.modules.pipelines.models import Pipeline
    from server.app.modules.tasks.models import PublishRecord, PublishTask
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a_ok = _make_approved_article(client, "未分发")
        a_dup = _make_approved_article(client, "已分发")
        acc1 = _make_account(app, client, "kg45", "号45")
        with app.session_factory() as db:
            uid = db.get(Article, a_ok).user_id
        gid = _make_group(app, uid, [a_ok, a_dup])

        # 预置：把 a_dup 标记为已分发（pending 记录即算在途），源节点应将其过滤出子集
        with app.session_factory() as db:
            create_task(
                db,
                uid,
                TaskCreate(
                    name="预置已分发",
                    task_type="article_round_robin",
                    article_ids=[a_dup],
                    accounts=[TaskAccountInput(account_id=acc1, sort_order=0)],
                ),
                role="admin",
            )
            db.commit()

        snap = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "article_group_source",
                    "name": "分组源",
                    "node_index": 0,
                    "config": {"group_id": gid},
                    "flow_meta": None,
                },
                {
                    "node_type": "distribute",
                    "name": "内容分发",
                    "node_index": 1,
                    "config": {"account_ids": [acc1]},
                    "flow_meta": None,
                },
            ],
        }
        pid = client.post(
            "/api/pipelines", json={"name": "分组自动分发", "type": "distribution"}
        ).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
        client.post(f"/api/pipelines/{pid}/publish", json={})

        with app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=pid, user_id=p.user_id)
            db.commit()
            rid = run.id
        run_pipeline(rid, app.session_factory)
        r = client.get(f"/api/pipelines/runs/{rid}").json()
        assert r["status"] == "done", r

        with app.session_factory() as db:
            auto = [
                t for t in db.query(PublishTask).all() if t.name and t.name.startswith("自动分发")
            ]
            assert len(auto) == 1
            assert auto[0].task_type == "article_round_robin"  # 非 group_round_robin（不重拉全组）
            # a_dup 不被重复发布：仍只有预置那 1 条记录
            dup_recs = db.query(PublishRecord).filter(PublishRecord.article_id == a_dup).all()
            assert len(dup_recs) == 1, "a_dup 被重复发布了（重拉全组的 bug）"
            ok_recs = db.query(PublishRecord).filter(PublishRecord.article_id == a_ok).all()
            assert len(ok_recs) == 1  # a_ok 被分发 1 次
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_distribute_blocks_unapproved_article_on_article_ids_path(monkeypatch):
    """审核门禁必须覆盖 #45/#46 的 article_ids 透传主路径。

    若上游「已审+未分发」子集里混入了未审核文章（比如 inputMapping 配错、或并发改了审核态），
    distribute → create_task 必须抛 ValidationError 并且不建任何任务——绝不能让未审内容被发布。

    现有用例只在 group_round_robin（手动分组）路径上验证过门禁（test_pipeline_review_distribute.py）；
    本用例补 article_round_robin 路径，而它正是源节点默认透传后的当前主路径。
    """
    from server.app.modules.articles.models import Article
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.distribute_node import run_distribute
    from server.app.modules.tasks.models import PublishRecord, PublishTask
    from server.app.shared.errors import ValidationError

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a_ok = _make_approved_article(client, "已审")
        a_bad = _make_approved_article(client, "未审")
        acc1 = _make_account(app, client, "kgate", "门禁号")
        with app.session_factory() as db:
            uid = db.get(Article, a_ok).user_id
            db.get(Article, a_bad).review_status = "pending"  # 子集里混入一篇未审
            db.commit()

        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"account_ids": [acc1]},
            inputs={"article_ids": [a_ok, a_bad]},  # 透传子集混入未审文章
            upstream={},
        )
        with pytest.raises(ValidationError):
            run_distribute(ctx)
        # 门禁在落库前拦截：不应残留任何任务
        with app.session_factory() as db:
            assert db.query(PublishTask).count() == 0

        # 反向对照：把 a_bad 改回 approved，同一子集应能正常建任务，
        # 证明上面的拦截确由「未审」状态触发，而非账号/文章缺失等其它原因。
        with app.session_factory() as db:
            db.get(Article, a_bad).review_status = "approved"
            db.commit()
        res = run_distribute(ctx)
        assert res.output.get("task_id")
        with app.session_factory() as db:
            t = db.get(PublishTask, res.output["task_id"])
            assert t.task_type == "article_round_robin"
            covered = {
                rec.article_id
                for rec in db.query(PublishRecord).filter(PublishRecord.task_id == t.id).all()
            }
            assert covered == {a_ok, a_bad}
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_approved_content_source_dedup_excludes_live_keeps_failed_softdeleted(monkeypatch):
    """去重：成功 + 在途(pending)记录都算「已分发/在途」→ 排除，不重复分发；
    失败、软删的记录不算 → 文章应重新可分发（可重试，不被永久埋没）。"""
    from server.app.modules.articles.models import Article
    from server.app.modules.pipelines.nodes.approved_content_source import (
        run_approved_content_source,
    )
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.tasks.models import PublishRecord
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a_ok = _make_approved_article(client, "成功已发")
        a_pending = _make_approved_article(client, "在途")
        a_fail = _make_approved_article(client, "失败")
        a_del = _make_approved_article(client, "软删记录")
        acc1 = _make_account(app, client, "dd", "去重号")
        with app.session_factory() as db:
            uid = db.get(Article, a_ok).user_id

            def _record_for(aid, status, deleted=False):
                tc = TaskCreate(
                    name=f"t-{aid}",
                    task_type="article_round_robin",
                    article_ids=[aid],
                    accounts=[TaskAccountInput(account_id=acc1, sort_order=0)],
                    stop_before_publish=False,
                )
                task = create_task(db, uid, tc, role="admin")
                db.flush()
                rec = db.query(PublishRecord).filter(PublishRecord.task_id == task.id).first()
                rec.status = status
                rec.is_deleted = deleted

            _record_for(a_ok, "succeeded")
            _record_for(a_pending, "pending")
            _record_for(a_fail, "failed")
            _record_for(a_del, "succeeded", deleted=True)
            db.commit()

        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"limit": 50, "exclude_distributed": True},
            inputs={},
            upstream={},
        )
        ids = set(run_approved_content_source(ctx).output["article_ids"])
        assert a_ok not in ids  # 成功 → 排除
        assert a_pending not in ids  # 在途 → 排除（不重复入队）
        assert a_fail in ids  # 失败 → 不排除（应能重试）
        assert a_del in ids  # 软删记录 → 不排除
    finally:
        app.cleanup()
