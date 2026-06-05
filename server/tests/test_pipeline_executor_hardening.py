"""Pipelines executor 生产级加固回归测试（Task 2 / 5 / 8 / 3）。

自包含：所需的 client / DB 夹具 helper 从 test_pipeline_review_distribute.py 复制进来，
不 import 那个测试模块（避免跨测试模块耦合）。
"""

import uuid

import pytest

from server.tests.utils import build_test_app

# --- helpers（复制自 test_pipeline_review_distribute.py，保持自包含）---


def _write_storage_state(data_dir, account_key: str) -> None:
    state_dir = data_dir / "browser_states" / "toutiao" / account_key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")


def _create_account(client, data_dir, account_key: str, display_name: str) -> int:
    """写 storage_state + /api/accounts/toutiao/login（不开浏览器）。"""
    _write_storage_state(data_dir, account_key)
    resp = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": display_name, "account_key": account_key, "use_browser": False},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


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


def _make_generation_template(client) -> int:
    r = client.post(
        "/api/prompt-templates",
        json={"name": "模板", "content": "写：", "scope": "generation"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# --- Task 2: 单活跃 run 闸 ---


@pytest.mark.mysql
def test_create_run_rejects_when_active_run_exists(monkeypatch):
    import pytest as _pytest

    from server.app.modules.pipelines.executor import create_run
    from server.app.modules.pipelines.models import Pipeline, PipelineRun
    from server.app.modules.system.models import User
    from server.app.shared.errors import ConflictError

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            user_id = db.query(User).first().id
            p = Pipeline(user_id=user_id, name="p", has_draft=False)
            db.add(p)
            db.flush()
            db.add(
                PipelineRun(
                    pipeline_id=p.id,
                    user_id=user_id,
                    status="running",
                    node_results={},
                    article_ids=[],
                )
            )
            db.commit()
            pid = p.id

        with test_app.session_factory() as db:
            with _pytest.raises(ConflictError):
                create_run(db, pipeline_id=pid, user_id=user_id)
    finally:
        test_app.cleanup()


# --- Task 5: 状态聚合修正（全失败 → failed）+ error_message ---


@pytest.mark.mysql
def test_run_all_generation_failed_is_failed_with_error_message(monkeypatch):
    def _boom(*, session_factory, user_id, template_content, question_text, model=None):
        raise RuntimeError("LLM 503")

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _boom,
    )

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        tpl = _make_generation_template(client)
        pid = client.post("/api/pipelines", json={"name": "全失败流"}).json()["id"]
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
        assert run["status"] == "failed", run  # 之前会错判 partial_failed
        assert run["error_message"] and "LLM 503" in run["error_message"]
    finally:
        test_app.cleanup()


# --- Task 8: 上游失败传播（dependsOnIndex 阻断下游）---


@pytest.mark.mysql
def test_downstream_blocked_when_dependency_failed(monkeypatch):
    from server.app.modules.tasks.models import PublishTask

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        acc1 = _create_account(client, test_app.data_dir, "account-a", "Account A")
        # source 指向不存在的 group → 抛错；distribute 自带 config.group_id 兜底（错误分组）
        snapshot = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "article_group_source",
                    "name": "源",
                    "node_index": 0,
                    "config": {"group_id": 999999},
                    "flow_meta": None,
                },
                {
                    "node_type": "distribute",
                    "name": "分发",
                    "node_index": 1,
                    "config": {"account_ids": [acc1], "group_id": 999999},
                    "flow_meta": {
                        "dependsOnIndex": 0,
                        "inputMapping": [{"from": "group_id", "to": "group_id"}],
                    },
                },
            ],
        }
        pid = client.post("/api/pipelines", json={"name": "依赖流"}).json()["id"]
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
        assert "上游" in (run["node_results"].get("1", {}).get("error", ""))
        with test_app.session_factory() as db:
            assert db.query(PublishTask).count() == 0  # 下游被阻断，未建错误任务
    finally:
        test_app.cleanup()


# --- Task 3: 成组 / 送审失败时降级 run 状态 ---


@pytest.mark.mysql
def test_run_downgraded_when_grouping_fails(monkeypatch):
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
    # 模拟成组失败：helper 返回 None
    monkeypatch.setattr(
        "server.app.modules.pipelines.executor.mark_pending_and_group",
        lambda *a, **k: None,
        raising=False,
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
                    "config": {"prompt_template_id": tpl, "count": 1},
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
        assert run["status"] == "partial_failed", run
        assert run["error_message"] and "成组" in run["error_message"]
    finally:
        test_app.cleanup()
