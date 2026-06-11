"""可编辑「搜图关键词 / 陪衬插图提示词」测试。

覆盖：
- prompt_templates.service.get_active_template_content：取该 scope「当前启用」模板内容（本人优先于系统、
  过滤软删/停用、空表 / 无 user_id 回退 default）。
- run_ai_format（web_fallback=True）顶层把两个模板解析成字符串：陪衬提示词拼进 system prompt，
  搜索词透传给 _maybe_insert_images（→ baidu）。无模板时回退内置默认。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.tests.utils import build_test_app


def _fake_completion(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _admin_uid(db):
    from server.app.modules.system.models import User

    user = db.query(User).filter(User.role == "admin").first()
    assert user is not None
    return user.id


def _add_template(
    db, *, content, scope, user_id, is_system=False, is_enabled=True, is_deleted=False
):
    from server.app.modules.prompt_templates.models import PromptTemplate

    db.add(
        PromptTemplate(
            name=f"{scope}-{content[:6]}",
            content=content,
            scope=scope,
            user_id=user_id,
            is_system=is_system,
            is_enabled=is_enabled,
            is_deleted=is_deleted,
        )
    )


# ── get_active_template_content ──────────────────────────────────────────────


@pytest.mark.mysql
def test_get_active_template_prefers_own_over_system(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.prompt_templates.service import get_active_template_content

        with app.session_factory() as db:
            uid = _admin_uid(db)
            _add_template(db, content="SYS", scope="image_search", user_id=None, is_system=True)
            _add_template(db, content="MINE", scope="image_search", user_id=uid)
            db.commit()

        with app.session_factory() as db:
            uid = _admin_uid(db)
            content = get_active_template_content(
                db, scope="image_search", user_id=uid, default="DEF"
            )
            assert content == "MINE"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_get_active_template_filters_and_falls_back(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.prompt_templates.service import get_active_template_content

        with app.session_factory() as db:
            uid = _admin_uid(db)
            # 仅启用的系统模板 + 一条停用的本人 + 一条软删的本人
            _add_template(db, content="SYS", scope="image_search", user_id=None, is_system=True)
            _add_template(db, content="OFF", scope="image_search", user_id=uid, is_enabled=False)
            _add_template(db, content="DEL", scope="image_search", user_id=uid, is_deleted=True)
            db.commit()

        with app.session_factory() as db:
            uid = _admin_uid(db)
            # 停用/软删被过滤 → 落到系统模板
            assert (
                get_active_template_content(db, scope="image_search", user_id=uid, default="DEF")
                == "SYS"
            )
            # user_id 为 None → default
            assert (
                get_active_template_content(db, scope="image_search", user_id=None, default="DEF")
                == "DEF"
            )
            # 该 scope 无任何模板 → default
            assert (
                get_active_template_content(db, scope="image_companion", user_id=uid, default="DEF")
                == "DEF"
            )
    finally:
        app.cleanup()


# ── run_ai_format 接线 ───────────────────────────────────────────────────────


def _one_paragraph_article(client):
    resp = client.post(
        "/api/articles",
        json={
            "title": "搜图提示词接线测试",
            "content_json": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "原神是一款开放世界游戏。"}],
                    }
                ],
            },
        },
    )
    assert resp.status_code == 200
    return resp.json()["id"]


@pytest.mark.mysql
def test_run_ai_format_uses_editable_image_prompts(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        from server.app.modules.articles.ai_format import run_ai_format

        with app.session_factory() as db:
            uid = _admin_uid(db)
            _add_template(db, content="{game} 横版 官方宣传图", scope="image_search", user_id=uid)
            _add_template(
                db,
                content="【陪衬增强】请更积极地为陪衬游戏配图",
                scope="image_companion",
                user_id=uid,
            )
            db.commit()

        article_id = _one_paragraph_article(client)

        captured: dict = {}

        def fake_completion(**kw):
            captured["messages"] = kw["messages"]
            return _fake_completion('{"heading_indices": [], "image_positions": []}')

        def fake_maybe(*args, **kwargs):
            captured["image_search_query"] = kwargs.get("image_search_query")
            return args[0], 0

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion", fake_completion
        )
        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._maybe_insert_images", fake_maybe
        )
        monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "test-key")

        run_ai_format(article_id, include_images=True, web_fallback=True, user_id=uid)

        system_prompt = captured["messages"][0]["content"]
        assert "【陪衬增强】请更积极地为陪衬游戏配图" in system_prompt
        assert captured["image_search_query"] == "{game} 横版 官方宣传图"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_run_ai_format_defaults_when_no_templates(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        from server.app.modules.articles.ai_format import (
            _WEB_FALLBACK_PROMPT_SUFFIX,
            run_ai_format,
        )

        with app.session_factory() as db:
            uid = _admin_uid(db)

        article_id = _one_paragraph_article(client)

        captured: dict = {}

        def fake_completion(**kw):
            captured["messages"] = kw["messages"]
            return _fake_completion('{"heading_indices": [], "image_positions": []}')

        def fake_maybe(*args, **kwargs):
            captured["image_search_query"] = kwargs.get("image_search_query")
            return args[0], 0

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion", fake_completion
        )
        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._maybe_insert_images", fake_maybe
        )
        monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "test-key")

        run_ai_format(article_id, include_images=True, web_fallback=True, user_id=uid)

        system_prompt = captured["messages"][0]["content"]
        # 无 image_companion 模板 → 用内置默认后缀
        assert _WEB_FALLBACK_PROMPT_SUFFIX in system_prompt
        # 无 image_search 模板 → 用 baidu 默认模板
        assert captured["image_search_query"] == "{game} 横版 官方宣传图"
    finally:
        app.cleanup()
