"""方案运行 executor 测试：展开、随机模板记录、部分/全部失败汇总、快照隔离、异步端点。

LiteLLM mock，不真实出网。
"""

import time
from types import SimpleNamespace

from server.tests.utils import build_test_app


def _fake_completion(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _admin_id(session_factory) -> int:
    from server.app.modules.system.models import User

    with session_factory() as db:
        return db.query(User).first().id


def _seed(app):
    """池含 A(a1,a2) / B(b1)，一个 generation 模板。返回 (pool_id, {rec:id}, uid, tpl_id)。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.prompt_templates.models import PromptTemplate

    uid = _admin_id(app.session_factory)
    ids: dict[str, int] = {}
    with app.session_factory() as db:
        pool = QuestionPool(user_id=uid, name="P")
        db.add(pool)
        db.flush()
        for rec, cat in [("a1", "A"), ("a2", "A"), ("b1", "B")]:
            it = QuestionItem(
                pool_id=pool.id,
                record_id=rec,
                fields={},
                question_text=f"问题{rec}",
                category=cat,
                source_active=True,
            )
            db.add(it)
            db.flush()
            ids[rec] = it.id
        tpl = PromptTemplate(
            name="g", content="写：{{问题}}", scope="generation", user_id=uid, is_enabled=True
        )
        db.add(tpl)
        db.flush()
        tpl_id = tpl.id
        pool_id = pool.id
        db.commit()
    return pool_id, ids, uid, tpl_id


def _create_scheme(app, pool_id, lines, uid, ai_engine=None) -> int:
    from server.app.modules.ai_generation.schemas import SchemeCreate, SchemeLineInput
    from server.app.modules.ai_generation.scheme_service import create_scheme

    with app.session_factory() as db:
        payload = SchemeCreate(
            name="s",
            pool_id=pool_id,
            ai_engine=ai_engine,
            lines=[SchemeLineInput(**ln) for ln in lines],
        )
        scheme = create_scheme(db, user_id=uid, pool_id=pool_id, payload=payload)
        db.commit()
        return scheme.id


def _run_now(app, scheme_id, uid) -> int:
    from server.app.modules.ai_generation.scheme_executor import create_run, run_scheme
    from server.app.modules.ai_generation.scheme_service import get_scheme

    with app.session_factory() as db:
        scheme = get_scheme(db, scheme_id)
        run = create_run(db, scheme=scheme, user_id=uid)
        db.commit()
        run_id = run.id
    run_scheme(run_id, app.session_factory)
    return run_id


def test_run_scheme_expands_by_article_count_and_records_template(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import (
            GenerationSchemeRun,
            GenerationSchemeRunTask,
        )

        pool_id, ids, uid, tpl = _seed(app)
        scheme_id = _create_scheme(
            app,
            pool_id,
            [
                {
                    "question_type": "A",
                    "question_item_ids": [ids["a1"], ids["a2"]],
                    "article_count": 3,
                    "allowed_prompt_template_ids": [tpl],
                },
                {
                    "question_type": "B",
                    "question_item_ids": [ids["b1"]],
                    "article_count": 1,
                    "allowed_prompt_template_ids": [tpl],
                },
            ],
            uid,
        )
        monkeypatch.setattr("litellm.completion", lambda **kw: _fake_completion("# 标题\n\n正文。"))

        run_id = _run_now(app, scheme_id, uid)

        with app.session_factory() as db:
            run = db.get(GenerationSchemeRun, run_id)
            tasks = db.query(GenerationSchemeRunTask).filter_by(run_id=run_id).all()
            assert len(tasks) == 4  # A:3 + B:1
            assert run.status == "done"
            assert len(run.article_ids) == 4
            assert all(t.status == "done" for t in tasks)
            assert all(t.article_id is not None for t in tasks)
            assert all(t.actual_prompt_template_id == tpl for t in tasks)
            # A 行 task 的问题文本是 a1、a2 的编号列表（合并选中问题）
            a_task = next(t for t in tasks if t.question_type == "A")
            assert "1. 问题a1" in a_task.question_text
            assert "2. 问题a2" in a_task.question_text
    finally:
        app.cleanup()


def test_run_scheme_prepends_questions_before_prompt_body(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
        from server.app.modules.prompt_templates.models import PromptTemplate

        uid = _admin_id(app.session_factory)
        ids: dict[str, int] = {}
        with app.session_factory() as db:
            pool = QuestionPool(user_id=uid, name="P")
            db.add(pool)
            db.flush()
            for rec in ("a1", "a2"):
                it = QuestionItem(
                    pool_id=pool.id,
                    record_id=rec,
                    fields={},
                    question_text=f"问题{rec}",
                    category="A",
                    source_active=True,
                )
                db.add(it)
                db.flush()
                ids[rec] = it.id
            # 不含 {{问题}} 占位符，走前置分支
            tpl = PromptTemplate(
                name="g",
                content="【产品提示词正文】围绕用户问题写作。",
                scope="generation",
                user_id=uid,
                is_enabled=True,
            )
            db.add(tpl)
            db.flush()
            tpl_id = tpl.id
            pool_id = pool.id
            db.commit()

        captured: dict[str, list] = {}

        def _cap(**kw):
            # 只记录首个调用（生文）的 messages；生文成功后的自动 AI 排版会再调一次。
            captured.setdefault("messages", kw["messages"])
            return _fake_completion("# 标题\n\n正文。")

        monkeypatch.setattr("litellm.completion", _cap)

        scheme_id = _create_scheme(
            app,
            pool_id,
            [
                {
                    "question_type": "A",
                    "question_item_ids": [ids["a1"], ids["a2"]],
                    "article_count": 1,
                    "allowed_prompt_template_ids": [tpl_id],
                }
            ],
            uid,
        )
        _run_now(app, scheme_id, uid)

        user_msg = next(m["content"] for m in captured["messages"] if m["role"] == "user")
        assert user_msg.startswith("基于以下 2 个问题，结合参考这些问题生成 1 篇文章：")
        assert "1. 问题a1" in user_msg
        # 真实问题排在产品提示词正文之前
        assert user_msg.index("1. 问题a1") < user_msg.index("【产品提示词正文】")
    finally:
        app.cleanup()


def test_run_scheme_passes_ai_engine_model_to_llm(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pool_id, ids, uid, tpl = _seed(app)
        scheme_id = _create_scheme(
            app,
            pool_id,
            [
                {
                    "question_type": "A",
                    "question_item_ids": [ids["a1"]],
                    "article_count": 1,
                    "allowed_prompt_template_ids": [tpl],
                }
            ],
            uid,
            ai_engine="deepseek/deepseek-chat",
        )
        seen: dict[str, str] = {}

        def _cap(**kw):
            # 只记录首个调用（生文）的 model；生文成功后的自动 AI 排版会用格式模型
            # 再调一次 litellm，不能覆盖这里的断言目标。
            seen.setdefault("model", kw.get("model"))
            return _fake_completion("# T\n\nx")

        monkeypatch.setattr("litellm.completion", _cap)
        _run_now(app, scheme_id, uid)
        assert seen.get("model") == "deepseek/deepseek-chat"
    finally:
        app.cleanup()


def test_run_scheme_partial_failed_when_a_types_templates_invalid(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import (
            GenerationSchemeRun,
            GenerationSchemeRunTask,
        )
        from server.app.modules.prompt_templates.models import PromptTemplate

        pool_id, ids, uid, tpl = _seed(app)
        with app.session_factory() as db:
            bad = PromptTemplate(
                name="b", content="x", scope="generation", user_id=uid, is_enabled=True
            )
            db.add(bad)
            db.flush()
            bad_id = bad.id
            db.commit()

        scheme_id = _create_scheme(
            app,
            pool_id,
            [
                {
                    "question_type": "A",
                    "question_item_ids": [ids["a1"]],
                    "article_count": 1,
                    "allowed_prompt_template_ids": [tpl],
                },
                {
                    "question_type": "B",
                    "question_item_ids": [ids["b1"]],
                    "article_count": 1,
                    "allowed_prompt_template_ids": [bad_id],
                },
            ],
            uid,
        )
        # 运行前把 B 行唯一模板软删除 → 该类型运行时模板全无效
        with app.session_factory() as db:
            db.get(PromptTemplate, bad_id).is_deleted = True
            db.commit()

        monkeypatch.setattr("litellm.completion", lambda **kw: _fake_completion("# T\n\nx"))
        run_id = _run_now(app, scheme_id, uid)

        with app.session_factory() as db:
            run = db.get(GenerationSchemeRun, run_id)
            tasks = {
                t.question_type: t
                for t in db.query(GenerationSchemeRunTask).filter_by(run_id=run_id)
            }
            assert run.status == "partial_failed"
            assert tasks["A"].status == "done"
            assert tasks["B"].status == "failed"
            assert tasks["B"].error_message and "无效" in tasks["B"].error_message
            assert run.article_ids == [tasks["A"].article_id]
    finally:
        app.cleanup()


def test_run_scheme_all_failed_when_llm_raises(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import (
            GenerationSchemeRun,
            GenerationSchemeRunTask,
        )

        pool_id, ids, uid, tpl = _seed(app)
        scheme_id = _create_scheme(
            app,
            pool_id,
            [
                {
                    "question_type": "A",
                    "question_item_ids": [ids["a1"]],
                    "article_count": 2,
                    "allowed_prompt_template_ids": [tpl],
                }
            ],
            uid,
        )

        def _boom(**kw):
            raise RuntimeError("LLM down")

        monkeypatch.setattr("litellm.completion", _boom)
        run_id = _run_now(app, scheme_id, uid)

        with app.session_factory() as db:
            run = db.get(GenerationSchemeRun, run_id)
            tasks = db.query(GenerationSchemeRunTask).filter_by(run_id=run_id).all()
            assert run.status == "failed"
            assert run.article_ids == []
            assert all(t.status == "failed" for t in tasks)
    finally:
        app.cleanup()


def test_run_scheme_uses_snapshot_and_does_not_touch_pool(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import (
            GenerationSchemeRunTask,
            QuestionItem,
        )

        pool_id, ids, uid, tpl = _seed(app)
        scheme_id = _create_scheme(
            app,
            pool_id,
            [
                {
                    "question_type": "A",
                    "question_item_ids": [ids["a1"]],
                    "article_count": 1,
                    "allowed_prompt_template_ids": [tpl],
                }
            ],
            uid,
        )
        # 方案保存后改动问题池：改文本 + 失效。运行应只读快照、不受影响。
        with app.session_factory() as db:
            it = db.get(QuestionItem, ids["a1"])
            it.question_text = "CHANGED"
            it.source_active = False
            db.commit()

        monkeypatch.setattr("litellm.completion", lambda **kw: _fake_completion("# T\n\nx"))
        run_id = _run_now(app, scheme_id, uid)

        with app.session_factory() as db:
            task = db.query(GenerationSchemeRunTask).filter_by(run_id=run_id).one()
            assert "问题a1" in task.question_text  # 用快照
            assert "CHANGED" not in task.question_text
            # 不碰旧消费字段
            it = db.get(QuestionItem, ids["a1"])
            assert it.status == "pending"
            assert it.article_id is None
    finally:
        app.cleanup()


def test_post_run_endpoint_executes_async(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pool_id, ids, uid, tpl = _seed(app)
        scheme_id = _create_scheme(
            app,
            pool_id,
            [
                {
                    "question_type": "A",
                    "question_item_ids": [ids["a1"]],
                    "article_count": 1,
                    "allowed_prompt_template_ids": [tpl],
                }
            ],
            uid,
        )
        monkeypatch.setattr("litellm.completion", lambda **kw: _fake_completion("# T\n\nx"))

        r = app.client.post(f"/api/generation/schemes/{scheme_id}/runs")
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]

        data = None
        for _ in range(50):
            data = app.client.get(f"/api/generation/scheme-runs/{run_id}").json()
            if data["status"] in ("done", "partial_failed", "failed"):
                break
            time.sleep(0.1)
        assert data is not None and data["status"] == "done", data
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["actual_prompt_template_id"] == tpl
    finally:
        app.cleanup()


def test_post_run_returns_503_when_bg_not_injected(monkeypatch):
    """Task 21: bg_session_factory 未注入时标 run failed + 返回 503，不再撒谎 202。"""
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation import scheme_router
        from server.app.modules.ai_generation.models import GenerationSchemeRun

        pool_id, ids, uid, tpl = _seed(app)
        scheme_id = _create_scheme(
            app,
            pool_id,
            [
                {
                    "question_type": "A",
                    "question_item_ids": [ids["a1"]],
                    "article_count": 1,
                    "allowed_prompt_template_ids": [tpl],
                }
            ],
            uid,
        )
        monkeypatch.setattr(scheme_router, "bg_session_factory", None)

        r = app.client.post(f"/api/generation/schemes/{scheme_id}/runs")
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["status"] == "failed"
        with app.session_factory() as db:
            run = db.get(GenerationSchemeRun, body["run_id"])
            assert run is not None and run.status == "failed"
            assert run.error_message
    finally:
        app.cleanup()


def test_group_failure_downgrades_run_to_partial_failed(monkeypatch):
    """Task 17: 成组失败（mark_pending_and_group 返回 None）时把 done 降级 partial_failed。"""
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import GenerationSchemeRun

        pool_id, ids, uid, tpl = _seed(app)
        scheme_id = _create_scheme(
            app,
            pool_id,
            [
                {
                    "question_type": "A",
                    "question_item_ids": [ids["a1"]],
                    "article_count": 1,
                    "allowed_prompt_template_ids": [tpl],
                }
            ],
            uid,
        )
        monkeypatch.setattr("litellm.completion", lambda **kw: _fake_completion("# T\n\nx"))
        monkeypatch.setattr(
            "server.app.modules.articles.service.mark_pending_and_group",
            lambda *a, **k: None,
        )

        run_id = _run_now(app, scheme_id, uid)
        with app.session_factory() as db:
            run = db.get(GenerationSchemeRun, run_id)
            assert run.status == "partial_failed"  # 旧实现：停在 done
            assert "成组" in (run.error_message or "") or "审核" in (run.error_message or "")
    finally:
        app.cleanup()


def test_temp_cover_skipped_when_bucket_not_configured(monkeypatch):
    """Task 24: GEO_TEMP_COVER_BUCKET 为空时整段跳过，不开数据库会话。"""
    monkeypatch.setenv("GEO_TEMP_COVER_BUCKET", "")
    from server.app.core.config import get_settings

    get_settings.cache_clear()
    try:
        from server.app.modules.ai_generation.scheme_executor import (
            _assign_temp_cover_from_bucket,
        )

        calls = {"n": 0}

        def _factory():
            calls["n"] += 1
            raise RuntimeError("禁用封面时不应打开会话")  # 旧实现会吞掉，故用计数断言

        _assign_temp_cover_from_bucket(article_id=1, user_id=1, session_factory=_factory)
        assert calls["n"] == 0  # 新实现：bucket 空 → 提前 return
    finally:
        get_settings.cache_clear()
