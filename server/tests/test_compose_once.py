"""compose_one：直调 article_writer.generate_article_from_prompt，绕开 scheme/pipeline 编排。"""

import pytest

from server.app.modules.ai_generation.compose_once import ComposeOnceRequest, compose_one


def test_compose_one_calls_writer_with_template_and_question(monkeypatch):
    """compose_one 应拼好 template_content + question_text 后调 generate_article_from_prompt。"""
    captured = {}

    def fake_writer(*, session_factory, user_id, template_content, question_text, model):
        captured["template_content"] = template_content
        captured["question_text"] = question_text
        captured["user_id"] = user_id
        captured["model"] = model
        return 987  # mock article_id

    monkeypatch.setattr(
        "server.app.modules.ai_generation.compose_once.generate_article_from_prompt",
        fake_writer,
    )

    # mock get_question_item / get_prompt_template
    class _Item:
        question_text = "测试问题"
        category = "未分类"

    class _Tpl:
        content = "请写一篇关于 {{问题}} 的文章"

    def fake_get_item(db, item_id):
        return _Item() if item_id == 1 else None

    def fake_get_tpl(db, tpl_id):
        return _Tpl() if tpl_id == 2 else None

    monkeypatch.setattr(
        "server.app.modules.ai_generation.compose_once._load_question_item", fake_get_item
    )
    monkeypatch.setattr(
        "server.app.modules.ai_generation.compose_once._load_prompt_template", fake_get_tpl
    )

    article_id = compose_one(
        session_factory=lambda: None,
        user_id=42,
        req=ComposeOnceRequest(question_item_id=1, prompt_template_id=2, model=None),
    )
    assert article_id == 987
    assert captured["template_content"] == "请写一篇关于 {{问题}} 的文章"
    assert "测试问题" in captured["question_text"]
    assert captured["user_id"] == 42


def test_compose_one_raises_on_missing_question(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.ai_generation.compose_once._load_question_item",
        lambda db, item_id: None,
    )
    monkeypatch.setattr(
        "server.app.modules.ai_generation.compose_once._load_prompt_template",
        lambda db, tpl_id: object(),
    )

    with pytest.raises(ValueError, match="question_item"):
        compose_one(
            session_factory=lambda: None,
            user_id=42,
            req=ComposeOnceRequest(question_item_id=999, prompt_template_id=2),
        )


def test_compose_once_api_requires_mcp_token(monkeypatch):
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config
        config.get_settings.cache_clear()

        # 不带 token → 401
        r = test_app.client.post(
            "/api/generation/compose-once",
            json={"question_item_id": 1, "prompt_template_id": 2, "user_id": test_app.admin_id},
        )
        assert r.status_code == 401

        # 带错 token → 401
        r = test_app.client.post(
            "/api/generation/compose-once",
            json={"question_item_id": 1, "prompt_template_id": 2, "user_id": test_app.admin_id},
            headers={"X-MCP-Token": "wrong"},
        )
        assert r.status_code == 401
    finally:
        test_app.cleanup()


def test_compose_once_api_returns_400_on_missing_question(monkeypatch):
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config
        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/generation/compose-once",
            json={"question_item_id": 999999, "prompt_template_id": 1, "user_id": test_app.admin_id},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 400
        assert "question_item" in r.json()["detail"]
    finally:
        test_app.cleanup()
