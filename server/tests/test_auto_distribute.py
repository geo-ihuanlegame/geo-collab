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
