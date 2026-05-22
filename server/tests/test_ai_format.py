"""Tests for AI format lock enforcement on article edit/delete."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from server.tests.utils import build_test_app


def _create_article(client) -> dict:
    response = client.post(
        "/api/articles",
        json={
            "title": "锁定测试文章",
            "content_json": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]},
        },
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.mysql
def test_edit_locked_article_returns_409(monkeypatch):
    """PUT /api/articles/{id} returns 409 when article is ai_checking."""
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        # Directly set ai_checking=True and ai_checking_started_at=now on the DB record
        from server.app.models.article import Article

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()

        response = client.put(
            f"/api/articles/{article_id}",
            json={"title": "修改标题"},
        )
        assert response.status_code == 409
        assert "AI 格式调整" in response.json()["detail"]
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_delete_locked_article_returns_409(monkeypatch):
    """DELETE /api/articles/{id} returns 409 when article is ai_checking."""
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        from server.app.models.article import Article

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()

        response = client.delete(f"/api/articles/{article_id}")
        assert response.status_code == 409
        assert "AI 格式调整" in response.json()["detail"]
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_edit_expired_lock_allows_update(monkeypatch):
    """PUT /api/articles/{id} succeeds when ai_checking lock has timed out (>120s)."""
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        from datetime import timedelta

        from server.app.models.article import Article

        # Set started_at to 121 seconds ago (past the 120s timeout)
        expired_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=121)

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = expired_time
            db.commit()

        response = client.put(
            f"/api/articles/{article_id}",
            json={"title": "过期锁应允许更新"},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "过期锁应允许更新"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_edit_unlocked_article_succeeds(monkeypatch):
    """PUT /api/articles/{id} succeeds when ai_checking=False."""
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        response = client.put(
            f"/api/articles/{article_id}",
            json={"title": "正常更新"},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "正常更新"
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# ai_format.py unit tests (no DB, no LiteLLM)
# ---------------------------------------------------------------------------
from server.app.modules.articles.ai_format import (
    _top_level_paragraphs,
    _paragraph_text,
    _apply_headings,
)


def test_top_level_paragraphs_returns_only_paragraphs():
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 1}, "content": []},
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
        ],
    }
    result = _top_level_paragraphs(doc)
    assert len(result) == 1
    assert result[0][0] == 1  # index in doc.content


def test_paragraph_text_joins_text_nodes():
    node = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "World"},
        ],
    }
    assert _paragraph_text(node) == "Hello World"


def test_apply_headings_converts_paragraph_to_h1():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Title"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Body"}]},
        ],
    }
    result = _apply_headings(doc, heading_indices={0})
    assert result["content"][0]["type"] == "heading"
    assert result["content"][0]["attrs"]["level"] == 1
    assert result["content"][1]["type"] == "paragraph"
