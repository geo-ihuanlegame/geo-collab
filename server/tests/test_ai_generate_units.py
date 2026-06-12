import uuid

import pytest

from server.tests.utils import build_test_app


def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    db = session_factory()
    try:
        art = create_article(
            db,
            user_id,
            ArticleCreate(
                title=f"A-{uuid.uuid4().hex[:6]}",
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


def _make_tpl(app, uid, enabled=True):
    from server.app.modules.prompt_templates.models import PromptTemplate

    with app.session_factory() as db:
        t = PromptTemplate(
            name="模板",
            content="写: {{question}}",
            scope="generation",
            user_id=uid,
            is_enabled=enabled,
        )
        db.add(t)
        db.commit()
        return t.id


def _uid(app):
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        return db.query(User).first().id


def _ctx(app, uid, config, inputs, upstream=None):
    from server.app.modules.pipelines.nodes.base import NodeRunContext

    return NodeRunContext(
        session_factory=app.session_factory,
        user_id=uid,
        config=config,
        inputs=inputs,
        upstream=upstream or {},
    )


@pytest.mark.mysql
def test_units_per_unit_fallback(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _fake_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate

        uid = _uid(app)
        t_unit, t_fallback = _make_tpl(app, uid), _make_tpl(app, uid)
        units = [
            {
                "question_type": "A",
                "question_text": "1. qa",
                "allowed_prompt_template_ids": [t_unit],
                "article_count": 2,
            },  # 自带模板+数量
            {
                "question_type": "B",
                "question_text": "1. qb",
                "allowed_prompt_template_ids": [],
                "article_count": None,
            },  # 全兜底
        ]
        ctx = _ctx(
            app,
            uid,
            {"prompt_template_id": t_fallback, "count": 3, "model": None},
            {"generation_units": units},
        )
        res = run_ai_generate(ctx)
        # A: 2 篇；B: 兜底数量 3 篇 → 共 5
        assert len(res.output["article_ids"]) == 5
        assert res.output["errors"] == []
        assert res.article_ids == res.output["article_ids"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_recovered_from_upstream_when_inputmapping_drops_them(monkeypatch):
    """显式 inputMapping 把 generation_units 从 inputs 里筛掉时，节点应从 upstream 兜底取回，
    per-type 模板/数量不被静默丢弃（根因③护栏）。"""
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _fake_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate

        uid = _uid(app)
        t_unit = _make_tpl(app, uid)
        units = [
            {
                "question_type": "A",
                "question_text": "1. qa",
                "allowed_prompt_template_ids": [t_unit],
                "article_count": 2,
            }
        ]
        # inputs 不含 generation_units（模拟 inputMapping 只映射了别的字段），upstream 仍带。
        ctx = _ctx(
            app,
            uid,
            {"prompt_template_id": None, "count": 5, "model": None},
            {"question_text": "1. qa"},
            upstream={"generation_units": units, "question_text": "1. qa"},
        )
        res = run_ai_generate(ctx)
        # 走逐单元：用 A 自带数量 2（而非本节点 count=5）→ 2 篇，且无错误
        assert len(res.output["article_ids"]) == 2
        assert res.output["errors"] == []
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_total_exceeds_cap_raises(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _fake_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.core.config import get_settings
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate
        from server.app.shared.errors import ValidationError

        uid = _uid(app)
        t = _make_tpl(app, uid)
        cap = get_settings().ai_generate_max_count
        units = [
            {
                "question_type": "A",
                "question_text": "1. q",
                "allowed_prompt_template_ids": [t],
                "article_count": cap + 1,
            }
        ]
        ctx = _ctx(
            app,
            uid,
            {"prompt_template_id": t, "count": 1, "model": None},
            {"generation_units": units},
        )
        with pytest.raises(ValidationError):
            run_ai_generate(ctx)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_missing_template_isolated_to_errors(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _fake_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate

        uid = _uid(app)
        t_ok = _make_tpl(app, uid)
        units = [
            {
                "question_type": "A",
                "question_text": "1. qa",
                "allowed_prompt_template_ids": [t_ok],
                "article_count": 1,
            },  # 正常
            {
                "question_type": "B",
                "question_text": "1. qb",
                "allowed_prompt_template_ids": [],
                "article_count": 1,
            },  # 无模板且本节点也无兜底模板
        ]
        ctx = _ctx(
            app,
            uid,
            {"prompt_template_id": None, "count": 1, "model": None},
            {"generation_units": units},
        )
        res = run_ai_generate(ctx)
        assert len(res.output["article_ids"]) == 1  # 只有 A 成功
        assert len(res.output["errors"]) == 1  # B 记错误、不抛
    finally:
        app.cleanup()
