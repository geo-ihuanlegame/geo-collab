"""POST /api/articles/save-from-mcp：Loop runner 主对话生 markdown，直接落库。

替代旧 compose-once 路径——不调任何 LLM，所以 GEO 这边不再需要 GEO_AI_API_KEY 也能
跑通 generation-loop。本测试钉住关键契约：鉴权、404、转换器调用、review_status、metrics。
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.mysql


def _seed_question_and_template(test_app):
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.prompt_templates.models import PromptTemplate

    with test_app.session_factory() as db:
        pool = QuestionPool(name="test-pool", user_id=test_app.admin_id)
        db.add(pool)
        db.commit()
        db.refresh(pool)
        item = QuestionItem(
            pool_id=pool.id,
            record_id="rec-test-1",
            fields={},
            question_text="怎么做红烧肉",
            category="美食",
            source_active=True,
            status="pending",
        )
        db.add(item)
        tpl = PromptTemplate(
            name="test-tpl",
            content="写：{{问题}}",
            scope="generation",
            user_id=test_app.admin_id,
            is_enabled=True,
        )
        db.add(tpl)
        db.commit()
        db.refresh(item)
        db.refresh(tpl)
        return item.id, tpl.id


def _collect_text_nodes(node: Any) -> list[str]:
    if isinstance(node, dict):
        texts: list[str] = []
        text = node.get("text")
        if isinstance(text, str):
            texts.append(text)
        content = node.get("content")
        if isinstance(content, list):
            for child in content:
                texts.extend(_collect_text_nodes(child))
        return texts
    if isinstance(node, list):
        texts = []
        for child in node:
            texts.extend(_collect_text_nodes(child))
        return texts
    return []


def test_save_from_mcp_requires_token(monkeypatch):
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/articles/save-from-mcp",
            json={
                "question_item_id": 1,
                "prompt_template_id": 1,
                "user_id": test_app.admin_id,
                "title": "红烧肉怎么做",
                "markdown_content": "## 食材\n五花肉",
            },
        )
        assert r.status_code == 401

        r2 = test_app.client.post(
            "/api/articles/save-from-mcp",
            json={
                "question_item_id": 1,
                "prompt_template_id": 1,
                "user_id": test_app.admin_id,
                "title": "x",
                "markdown_content": "x",
            },
            headers={"X-MCP-Token": "wrong"},
        )
        assert r2.status_code == 401
    finally:
        test_app.cleanup()


def test_save_from_mcp_returns_404_for_missing_question(monkeypatch):
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/articles/save-from-mcp",
            json={
                "question_item_id": 999999,
                "prompt_template_id": 1,
                "user_id": test_app.admin_id,
                "title": "x",
                "markdown_content": "x",
            },
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 404
        assert "question_item" in r.json()["detail"]
    finally:
        test_app.cleanup()


def _seed_question_and_custom_template(test_app, *, is_enabled=True, is_deleted=False):
    """种一个问题项 + 一个可定制启用/软删状态的提示词模板，返回 (item_id, tpl_id)。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.prompt_templates.models import PromptTemplate

    with test_app.session_factory() as db:
        pool = QuestionPool(name="test-pool", user_id=test_app.admin_id)
        db.add(pool)
        db.commit()
        db.refresh(pool)
        item = QuestionItem(
            pool_id=pool.id,
            record_id="rec-custom-tpl",
            fields={},
            question_text="怎么做红烧肉",
            category="美食",
            source_active=True,
            status="pending",
        )
        db.add(item)
        tpl = PromptTemplate(
            name="custom-tpl",
            content="写：{{问题}}",
            scope="generation",
            user_id=test_app.admin_id,
            is_enabled=is_enabled,
            is_deleted=is_deleted,
        )
        db.add(tpl)
        db.commit()
        db.refresh(item)
        db.refresh(tpl)
        return item.id, tpl.id


def test_save_from_mcp_rejects_disabled_template(monkeypatch):
    """关闭（is_enabled=False）的提示词不该能用来生成文章 → 400，且不落库。

    bug 复现：被运营关闭的模板若仍能写入，等于"关闭"这个业务开关对 MCP 生文路径失效。
    """
    from server.app.modules.articles.models import Article
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        qid, tpl_id = _seed_question_and_custom_template(test_app, is_enabled=False)

        r = test_app.client.post(
            "/api/articles/save-from-mcp",
            json={
                "question_item_id": qid,
                "prompt_template_id": tpl_id,
                "user_id": test_app.admin_id,
                "title": "x",
                "markdown_content": "正文",
            },
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 400, r.text
        assert "prompt_template" in r.json()["detail"]
        with test_app.session_factory() as db:
            assert db.query(Article).count() == 0
    finally:
        test_app.cleanup()


def test_save_from_mcp_rejects_soft_deleted_template(monkeypatch):
    """软删（is_deleted=True）的提示词视同不存在 → 404。"""
    from server.app.modules.articles.models import Article
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        qid, tpl_id = _seed_question_and_custom_template(test_app, is_deleted=True)

        r = test_app.client.post(
            "/api/articles/save-from-mcp",
            json={
                "question_item_id": qid,
                "prompt_template_id": tpl_id,
                "user_id": test_app.admin_id,
                "title": "x",
                "markdown_content": "正文",
            },
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 404, r.text
        assert "prompt_template" in r.json()["detail"]
        with test_app.session_factory() as db:
            assert db.query(Article).count() == 0
    finally:
        test_app.cleanup()


def test_save_from_mcp_returns_404_for_missing_template(monkeypatch):
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        qid, _ = _seed_question_and_template(test_app)

        r = test_app.client.post(
            "/api/articles/save-from-mcp",
            json={
                "question_item_id": qid,
                "prompt_template_id": 999999,
                "user_id": test_app.admin_id,
                "title": "x",
                "markdown_content": "x",
            },
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 404
        assert "prompt_template" in r.json()["detail"]
    finally:
        test_app.cleanup()


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", ""),  # min_length=1
        ("title", "x" * 301),  # max_length=300
        ("markdown_content", ""),  # min_length=1
    ],
)
def test_save_from_mcp_rejects_invalid_payload(monkeypatch, field, value):
    """schema 校验：title / markdown_content 不能空，title ≤ 300。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        qid, tpl_id = _seed_question_and_template(test_app)

        payload = {
            "question_item_id": qid,
            "prompt_template_id": tpl_id,
            "user_id": test_app.admin_id,
            "title": "ok title",
            "markdown_content": "## ok body",
            field: value,
        }

        r = test_app.client.post(
            "/api/articles/save-from-mcp",
            json=payload,
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 422
    finally:
        test_app.cleanup()


def test_save_from_mcp_persists_article_with_review_pending(monkeypatch):
    """happy path：落库后 title/plain_text/review_status/metrics writer_model 都正确。"""
    from server.app.modules.articles.models import Article
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        qid, tpl_id = _seed_question_and_template(test_app)

        markdown = "## 食材\n- 五花肉 500g\n\n## 步骤\n1. 焯水\n2. 上色"
        r = test_app.client.post(
            "/api/articles/save-from-mcp",
            json={
                "question_item_id": qid,
                "prompt_template_id": tpl_id,
                "user_id": test_app.admin_id,
                "title": "红烧肉家常做法",
                "markdown_content": markdown,
                "model_label": "claude-opus-4-7",
            },
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        article_id = r.json()["article_id"]
        assert article_id > 0

        with test_app.session_factory() as db:
            article = db.query(Article).filter(Article.id == article_id).first()
            assert article is not None
            assert article.title == "红烧肉家常做法"
            assert article.plain_text == markdown
            assert article.review_status == "pending"
            assert article.user_id == test_app.admin_id
            # content_json / html 由 converter 填好，至少非空
            assert article.content_json
            assert article.content_html
            # metrics 落写作者标签
            assert (article.metrics or {}).get("writer_model") == "claude-opus-4-7"
    finally:
        test_app.cleanup()


def test_save_from_mcp_normalizes_json_escaped_quotes(monkeypatch):
    """writer 把正文直引号写成字面 `\"` 时，保存边界要还原成普通引号。"""
    from server.app.modules.articles.models import Article
    from server.app.modules.articles.parser import loads_content_json
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        qid, tpl_id = _seed_question_and_template(test_app)

        markdown = (
            r"但\"平板好玩\"不等于\"手机好玩\"。"
            "\n\n## 综合测评\n"
            r"它比\"开店\"更长线。"
        )
        r = test_app.client.post(
            "/api/articles/save-from-mcp",
            json={
                "question_item_id": qid,
                "prompt_template_id": tpl_id,
                "user_id": test_app.admin_id,
                "title": "平板游戏推荐",
                "markdown_content": markdown,
            },
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text

        with test_app.session_factory() as db:
            article = db.query(Article).filter(Article.id == r.json()["article_id"]).first()
            assert article is not None
            assert '\\"' not in article.plain_text
            assert '\\"' not in article.content_html
            assert '"平板好玩"' in article.plain_text
            assert '"开店"' in article.content_html

            content_json = loads_content_json(article.content_json)
            texts = "".join(_collect_text_nodes(content_json))
            assert '\\"' not in texts
            assert '"手机好玩"' in texts
    finally:
        test_app.cleanup()


def test_save_from_mcp_stamps_loop_agent_and_template_name(monkeypatch):
    """MCP 落库要回写生文溯源：智能体固定字样 "loop"，模板取所用提示词模板的名称。

    与 pipeline 生文路径（source_agent_name=管线名 / source_template_name=模板名）对齐，
    让 MCP 文章在内容列表 / 卡片里也能显示「智能体：loop」「模板：<模板名>」，而非「—」。
    模板名由后端从 prompt_template_id 查得的 tpl.name 权威给出，不信任客户端传参。
    """
    from server.app.modules.articles.models import Article
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        qid, tpl_id = _seed_question_and_template(test_app)

        r = test_app.client.post(
            "/api/articles/save-from-mcp",
            json={
                "question_item_id": qid,
                "prompt_template_id": tpl_id,
                "user_id": test_app.admin_id,
                "title": "红烧肉家常做法",
                "markdown_content": "## 食材\n五花肉",
            },
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text

        with test_app.session_factory() as db:
            article = db.query(Article).filter(Article.id == r.json()["article_id"]).first()
            assert article is not None
            assert article.source_agent_name == "loop"
            assert article.source_template_name == "test-tpl"
            assert article.source_template_id == tpl_id
    finally:
        test_app.cleanup()


def test_save_from_mcp_without_model_label_leaves_metrics_clean(monkeypatch):
    """model_label 不传时 article.metrics 不该被强写 None/空键，保持 None/{} 原状。"""
    from server.app.modules.articles.models import Article
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        qid, tpl_id = _seed_question_and_template(test_app)

        r = test_app.client.post(
            "/api/articles/save-from-mcp",
            json={
                "question_item_id": qid,
                "prompt_template_id": tpl_id,
                "user_id": test_app.admin_id,
                "title": "ok",
                "markdown_content": "正文一段",
            },
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        with test_app.session_factory() as db:
            article = db.query(Article).filter(Article.id == r.json()["article_id"]).first()
            # 不传 model_label 时 metrics 是 None 或不含 writer_model
            metrics = article.metrics or {}
            assert "writer_model" not in metrics
    finally:
        test_app.cleanup()
