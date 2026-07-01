"""运行期模板解析 role-aware 修复测试。

自动化运行/校验(pipeline ai_compose/ai_generate、方案运行/保存)按 id 解析提示词模板时：
**admin 可跨属主取任意模板，非 admin 仍只限本人私有 + 系统模板**（把编辑器列表接口既有的
admin 全量可见性搬到运行/校验期）。软删 / 停用 / 非 generation 仍一律无效。

背景：生产 pipeline「生文测试」以 run.user_id=1 运行，节点候选是 user 3/4 的私有模板，
运行期旧的 get_visible_prompt_template 按 run 用户归属过滤把它们全滤掉 → 抛「全部无效」。

判定只看**运行主体 run.user_id**（非配置者）：admin 把他人模板配进非 admin 的 workflow 后，
owner/定时（主体=非 admin）运行仍隔离失败——这是既定取舍、非回归（见本文件 units 用例的非 admin 分支）。

LiteLLM 不出网：节点级用例 stub 掉 generate_article_from_prompt。
"""

import pytest

from server.tests.utils import build_test_app, create_extra_user


def _mk_template(
    app,
    *,
    user_id,
    scope="generation",
    is_system=False,
    is_enabled=True,
    is_deleted=False,
    name="t",
    content="写：",
) -> int:
    from server.app.modules.prompt_templates.models import PromptTemplate

    with app.session_factory() as db:
        t = PromptTemplate(
            name=name,
            content=content,
            scope=scope,
            user_id=user_id,
            is_system=is_system,
            is_enabled=is_enabled,
            is_deleted=is_deleted,
        )
        db.add(t)
        db.commit()
        return t.id


@pytest.mark.mysql
def test_get_runtime_prompt_template_role_aware(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.prompt_templates.service import (
            get_runtime_prompt_template,
            get_visible_prompt_template,
        )

        admin_id = app.admin_id
        owner_id, _ = create_extra_user(app, "owner")
        third_id, _ = create_extra_user(app, "third")

        tpl = _mk_template(app, user_id=owner_id)  # 他人私有、启用、未删、generation

        with app.session_factory() as db:
            # admin 跨属主命中；非 admin 第三方隔离；属主本人可取
            assert (
                get_runtime_prompt_template(db, tpl, user_id=admin_id, scope="generation")
                is not None
            )
            assert (
                get_runtime_prompt_template(db, tpl, user_id=third_id, scope="generation") is None
            )
            assert (
                get_runtime_prompt_template(db, tpl, user_id=owner_id, scope="generation")
                is not None
            )
            # 对照：get_visible 对 admin 同样取不到他人私有（差异只在新函数的 admin 分支）
            assert (
                get_visible_prompt_template(db, tpl, user_id=admin_id, scope="generation") is None
            )

        # 系统模板：非 admin 也能取（is_system 分支未动）
        sys_tpl = _mk_template(app, user_id=None, is_system=True)
        with app.session_factory() as db:
            assert (
                get_runtime_prompt_template(db, sys_tpl, user_id=third_id, scope="generation")
                is not None
            )

        # 软删：admin 与非 admin 都取不到（边界不放开）
        del_tpl = _mk_template(app, user_id=owner_id, is_deleted=True)
        with app.session_factory() as db:
            assert (
                get_runtime_prompt_template(db, del_tpl, user_id=admin_id, scope="generation")
                is None
            )
            assert (
                get_runtime_prompt_template(db, del_tpl, user_id=third_id, scope="generation")
                is None
            )

        # scope 不符：generation 查询取不到 ai_format 模板（即便 admin）
        fmt_tpl = _mk_template(app, user_id=owner_id, scope="ai_format")
        with app.session_factory() as db:
            assert (
                get_runtime_prompt_template(db, fmt_tpl, user_id=admin_id, scope="generation")
                is None
            )
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_pick_valid_template_role_aware(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.scheme_executor import _pick_valid_template

        admin_id = app.admin_id
        owner_id, _ = create_extra_user(app, "owner")
        third_id, _ = create_extra_user(app, "third")

        tpl = _mk_template(app, user_id=owner_id)

        with app.session_factory() as db:
            assert _pick_valid_template(db, [tpl], admin_id) is not None  # admin 跨属主
            assert _pick_valid_template(db, [tpl], third_id) is None  # 非 admin 隔离
            assert _pick_valid_template(db, [tpl], owner_id) is not None  # 属主本人

        # is_enabled 复核仍在：停用 / 软删候选即便 admin 也跳过
        disabled = _mk_template(app, user_id=owner_id, is_enabled=False)
        deleted = _mk_template(app, user_id=owner_id, is_deleted=True)
        with app.session_factory() as db:
            assert _pick_valid_template(db, [disabled], admin_id) is None
            assert _pick_valid_template(db, [deleted], admin_id) is None
    finally:
        app.cleanup()


def _stub_generate(
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


@pytest.mark.mysql
def test_ai_compose_units_cross_owner_admin_ok_nonadmin_isolated(monkeypatch):
    """复现生产 bug：ai_compose 逐单元、单元模板属他人。

    admin 作运行主体 → 跨属主可用、正常产文；非 admin 第三方作运行主体 → 仍抛
    「该单元允许模板在运行时全部无效」（既定取舍：判定按运行主体、非配置者，不是回归）。
    """
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt",
        _stub_generate,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        admin_id = app.admin_id
        owner_id, _ = create_extra_user(app, "owner")
        third_id, _ = create_extra_user(app, "third")
        tpl = _mk_template(app, user_id=owner_id, name="他人模板")

        def _unit():
            return {
                "question_type": "A",
                "question_text": "1. 问题q",
                "allowed_prompt_template_ids": [tpl],
                "article_count": 1,
            }

        # admin 运行主体 → 产文成功
        ctx_admin = NodeRunContext(
            session_factory=app.session_factory,
            user_id=admin_id,
            config={},
            inputs={"generation_units": [_unit()]},
            upstream={},
            pipeline_name="生文测试",
        )
        res_admin = run_ai_compose(ctx_admin)
        assert len(res_admin.output["article_ids"]) == 1
        assert not res_admin.output["errors"]

        # 非 admin 第三方运行主体 → 隔离失败（既定行为）
        ctx_third = NodeRunContext(
            session_factory=app.session_factory,
            user_id=third_id,
            config={},
            inputs={"generation_units": [_unit()]},
            upstream={},
            pipeline_name="生文测试",
        )
        res_third = run_ai_compose(ctx_third)
        assert res_third.output["article_ids"] == []
        assert any("该单元允许模板在运行时全部无效" in e for e in res_third.output["errors"])
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_validate_template_ids_role_aware(monkeypatch):
    """方案保存校验与运行期对齐：admin 可选他人模板、非 admin 不可。"""
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.scheme_service import _validate_template_ids
        from server.app.shared.errors import ValidationError

        admin_id = app.admin_id
        owner_id, _ = create_extra_user(app, "owner")
        third_id, _ = create_extra_user(app, "third")
        tpl = _mk_template(app, user_id=owner_id)

        with app.session_factory() as db:
            _validate_template_ids(db, template_ids=[tpl], user_id=admin_id)  # 不抛
            with pytest.raises(ValidationError):
                _validate_template_ids(db, template_ids=[tpl], user_id=third_id)
    finally:
        app.cleanup()
