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
