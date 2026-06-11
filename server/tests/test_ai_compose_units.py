"""ai_compose 逐单元模式：与 ai_generate 对齐，但保留多模板随机 + 空输入安静跳过。

关键护栏 test_units_gate_off_when_no_per_type_count：上游有 generation_units 但没有任何
类型显式配文章数时，必须维持原扁平行为（按本节点 count），不得切逐单元——保护存量
ai_compose 流程（含 question_source 的旧扁平 config，其 gen_units 的 article_count 恒为 None）。
"""

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
def test_units_per_type_counts(monkeypatch):
    """每类型独立文章数：A 自带 2 + B 自带 3 → 共 5（而非本节点 count=1）。"""
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt",
        _fake_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose

        uid = _uid(app)
        t_unit, t_fallback = _make_tpl(app, uid), _make_tpl(app, uid)
        units = [
            {
                "question_type": "A",
                "question_text": "1. qa",
                "allowed_prompt_template_ids": [t_unit],
                "article_count": 2,
            },
            {
                "question_type": "B",
                "question_text": "1. qb",
                "allowed_prompt_template_ids": [],  # 无 per-type 模板 → 回退本节点
                "article_count": 3,
            },
        ]
        ctx = _ctx(
            app,
            uid,
            {"ai_engine": None, "prompt_template_ids": [t_fallback], "count": 1},
            {"generation_units": units, "question_text": "1. qa\n2. qb"},
        )
        res = run_ai_compose(ctx)
        assert len(res.output["article_ids"]) == 5
        assert res.output["errors"] == []
        assert res.article_ids == res.output["article_ids"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_gate_off_when_no_per_type_count(monkeypatch):
    """护栏：units 存在但没有任何类型配文章数 → 维持扁平行为（本节点 count=2 → 2 篇）。"""
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt",
        _fake_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose

        uid = _uid(app)
        tpl = _make_tpl(app, uid)
        units = [
            {
                "question_type": "A",
                "question_text": "1. qa",
                "allowed_prompt_template_ids": [],
                "article_count": None,  # 旧扁平 config 派生的 gen_unit
            },
            {
                "question_type": "B",
                "question_text": "1. qb",
                "allowed_prompt_template_ids": [],
                "article_count": None,
            },
        ]
        ctx = _ctx(
            app,
            uid,
            {"ai_engine": None, "prompt_template_ids": [tpl], "count": 2},
            {"generation_units": units, "question_text": "1. qa\n2. qb"},
        )
        res = run_ai_compose(ctx)
        # 扁平：按本节点 count=2，而不是 sum(per-type)
        assert len(res.output["article_ids"]) == 2
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_recovered_from_upstream(monkeypatch):
    """显式 inputMapping 把 generation_units 从 inputs 筛掉 → 从 upstream 兜底取回（根因③护栏）。"""
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt",
        _fake_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose

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
        ctx = _ctx(
            app,
            uid,
            {"ai_engine": None, "prompt_template_ids": [], "count": 5},
            {"question_text": "1. qa"},  # inputs 不含 generation_units
            upstream={"generation_units": units, "question_text": "1. qa"},
        )
        res = run_ai_compose(ctx)
        # 走逐单元：用 A 自带数量 2（而非本节点 count=5）
        assert len(res.output["article_ids"]) == 2
        assert res.output["errors"] == []
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_total_exceeds_cap_raises(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt",
        _fake_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.core.config import get_settings
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose
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
            {"ai_engine": None, "prompt_template_ids": [t], "count": 1},
            {"generation_units": units},
        )
        with pytest.raises(ValidationError):
            run_ai_compose(ctx)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_missing_template_isolated_to_errors(monkeypatch):
    """单元无模板且本节点也无兜底模板 → 该单元记 errors、不抛；其余单元照常产出。"""
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt",
        _fake_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose

        uid = _uid(app)
        t_ok = _make_tpl(app, uid)
        units = [
            {
                "question_type": "A",
                "question_text": "1. qa",
                "allowed_prompt_template_ids": [t_ok],
                "article_count": 1,
            },
            {
                "question_type": "B",
                "question_text": "1. qb",
                "allowed_prompt_template_ids": [],
                "article_count": 1,
            },
        ]
        ctx = _ctx(
            app,
            uid,
            {"ai_engine": None, "prompt_template_ids": [], "count": 1},  # 无节点兜底模板
            {"generation_units": units},
        )
        res = run_ai_compose(ctx)
        assert len(res.output["article_ids"]) == 1  # 只有 A 成功
        assert len(res.output["errors"]) == 1  # B 记错误、不抛
    finally:
        app.cleanup()
