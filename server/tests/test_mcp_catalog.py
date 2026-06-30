"""[MCP] /api/mcp/* catalog 端点：MCP token 鉴权 + 基本返回形态。

覆盖：
- 不带 / 错 token → 401（每个端点）
- 带正确 token → 200 + JSON list（空 DB 也能返回 []）
"""

from __future__ import annotations

import pytest


@pytest.mark.mysql
def test_mcp_catalog_endpoints_require_token(monkeypatch):
    """所有 /api/mcp/* GET 端点都必须用 MCP token；user JWT cookie 不该过、空 / 错 token 401。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        paths = [
            "/api/mcp/articles",
            "/api/mcp/question-pools",
            "/api/mcp/prompt-templates",
            "/api/mcp/pipelines",
            "/api/mcp/accounts",
        ]
        for path in paths:
            # 不带 token
            r = test_app.client.get(path)
            assert r.status_code == 401, f"{path} 未带 token 应 401，实际 {r.status_code}"
            # 错 token
            r = test_app.client.get(path, headers={"X-MCP-Token": "wrong"})
            assert r.status_code == 401, f"{path} 错 token 应 401，实际 {r.status_code}"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_mcp_catalog_endpoints_pass_with_token(monkeypatch):
    """带正确 MCP token 调时应返回 200 + JSON list（空 DB 返回空列表）。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()
        headers = {"X-MCP-Token": "secret"}

        for path in [
            "/api/mcp/articles",
            "/api/mcp/question-pools",
            "/api/mcp/prompt-templates",
            "/api/mcp/pipelines",
            "/api/mcp/accounts",
        ]:
            r = test_app.client.get(path, headers=headers)
            assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"
            assert isinstance(r.json(), list), f"{path} 应返回 list"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_mcp_prompt_templates_excludes_disabled_but_admin_list_keeps(monkeypatch):
    """MCP catalog 只把"启用"的提示词递给 Loop（关闭的不该出现在清单里）；
    但 admin 管理列表 /api/prompt-templates 仍要看得到关闭模板以便重新启用。

    两条业务逻辑隔离：查询是查询（catalog 过滤 enabled、admin 列全量），
    跟保存层的校验各管各的。
    """
    from server.app.modules.prompt_templates.models import PromptTemplate
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        with test_app.session_factory() as db:
            db.add(
                PromptTemplate(
                    name="enabled-tpl",
                    content="x",
                    scope="generation",
                    user_id=test_app.admin_id,
                    is_enabled=True,
                )
            )
            db.add(
                PromptTemplate(
                    name="disabled-tpl",
                    content="x",
                    scope="generation",
                    user_id=test_app.admin_id,
                    is_enabled=False,
                )
            )
            db.commit()

        # MCP 视角（service token）：只看得到启用的
        r = test_app.client.get("/api/mcp/prompt-templates", headers={"X-MCP-Token": "secret"})
        assert r.status_code == 200, r.text
        mcp_names = {t["name"] for t in r.json()}
        assert "enabled-tpl" in mcp_names
        assert "disabled-tpl" not in mcp_names

        # admin 管理列表（user JWT，test_app.client 默认 admin）：两者都在
        r2 = test_app.client.get("/api/prompt-templates")
        assert r2.status_code == 200, r2.text
        admin_names = {t["name"] for t in r2.json()}
        assert "enabled-tpl" in admin_names
        assert "disabled-tpl" in admin_names
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_mcp_catalog_articles_filter_by_review_status(monkeypatch):
    """`review_status=approved` 过滤应只返回审核通过的文章。"""
    from server.app.modules.articles import create_article
    from server.app.modules.articles.schemas import ArticleCreate
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        # 准备一篇 pending 一篇 approved
        from server.app.db.session import SessionLocal

        db = SessionLocal()
        try:
            a1 = create_article(
                db,
                test_app.admin_id,
                ArticleCreate(
                    title="未审核",
                    content_json={"type": "doc", "content": []},
                    plain_text="正文一",
                    word_count=10,
                ),
            )
            a2 = create_article(
                db,
                test_app.admin_id,
                ArticleCreate(
                    title="已审核",
                    content_json={"type": "doc", "content": []},
                    plain_text="正文二",
                    word_count=10,
                ),
            )
            # 默认 review_status="approved"，显式把 a1 改成 pending 才能测过滤
            a1.review_status = "pending"
            db.commit()
            approved_id = a2.id
            pending_id = a1.id
        finally:
            db.close()

        r = test_app.client.get(
            "/api/mcp/articles?review_status=approved",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        ids = {item["id"] for item in r.json()}
        assert approved_id in ids
        assert pending_id not in ids
    finally:
        test_app.cleanup()
