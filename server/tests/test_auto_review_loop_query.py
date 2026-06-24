"""list_recent_decisions service 测试 + /today-loop-decisions 端点测试。

测试覆盖：
- 基本查询（命中 decided_by + decision）
- 时间窗边界（since_hours 之外不算）
- decided_by 过滤（其它 decided_by 排除）
- model_label 过滤（Article.metrics.writer_model）
- limit 截断 items 但 count 给全量
- 端点鉴权（无 MCP token → 401）
- 端点正常返回（count + items 结构）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from server.app.core.time import utcnow
from server.tests.utils import build_test_app


def _mk_article(test_app, *, title: str, writer_model: str | None = None) -> int:
    """Helper: 建一篇最小 article，可选写 metrics.writer_model。"""
    from server.app.modules.articles.models import Article

    db = test_app.session_factory()
    try:
        a = Article(
            user_id=test_app.admin_id,
            title=title,
            content_json=json.dumps({"type": "doc", "content": []}),
            content_html="",
            plain_text="",
            word_count=0,
            status="draft",
            review_status="pending",
            metrics={"writer_model": writer_model} if writer_model else None,
        )
        db.add(a)
        db.commit()
        return a.id
    finally:
        db.close()


def _mk_decision(
    test_app,
    *,
    article_id: int,
    decided_by: str = "claude-goal-verifier",
    decision: str = "approved",
    score_total: int | None = 80,
    created_at: datetime | None = None,
) -> int:
    """Helper: 建一条 AutoReviewDecision，可选指定 created_at（用于时间窗测试）。"""
    from server.app.modules.auto_review.models import AutoReviewDecision

    db = test_app.session_factory()
    try:
        d = AutoReviewDecision(
            article_id=article_id,
            decision=decision,
            score_total=score_total,
            score_breakdown=None,
            reasoning=None,
            decided_by=decided_by,
        )
        if created_at is not None:
            d.created_at = created_at
        db.add(d)
        db.commit()
        return d.id
    finally:
        db.close()


@pytest.mark.mysql
def test_list_recent_decisions_basic(monkeypatch):
    """命中 decided_by + decision 的行进入 count & items。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.auto_review.service import list_recent_decisions

        a1 = _mk_article(test_app, title="goal-article-1")
        a2 = _mk_article(test_app, title="goal-article-2")
        a3 = _mk_article(test_app, title="other-article")

        _mk_decision(test_app, article_id=a1)  # claude-goal-verifier / approved
        _mk_decision(test_app, article_id=a2)  # claude-goal-verifier / approved
        _mk_decision(test_app, article_id=a3, decided_by="other-bot")  # 不命中

        db = test_app.session_factory()
        try:
            count, items = list_recent_decisions(
                db,
                decided_by="claude-goal-verifier",
                decision="approved",
                since_hours=24,
            )
            assert count == 2
            assert {it["title"] for it in items} == {"goal-article-1", "goal-article-2"}
            assert all("decided_at" in it for it in items)
            assert all(it["score_total"] == 80 for it in items)
        finally:
            db.close()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_list_recent_decisions_time_window(monkeypatch):
    """26h 前的 decision 不算（since_hours=24 默认）。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.auto_review.service import list_recent_decisions

        a_old = _mk_article(test_app, title="old")
        a_new = _mk_article(test_app, title="new")

        _mk_decision(test_app, article_id=a_old, created_at=utcnow() - timedelta(hours=26))
        _mk_decision(test_app, article_id=a_new)  # 默认 utcnow

        db = test_app.session_factory()
        try:
            count, items = list_recent_decisions(
                db,
                decided_by="claude-goal-verifier",
                decision="approved",
                since_hours=24,
            )
            assert count == 1
            assert items[0]["title"] == "new"
        finally:
            db.close()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_list_recent_decisions_decision_filter(monkeypatch):
    """decision='approved' 不命中 needs_rewrite / rejected 行。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.auto_review.service import list_recent_decisions

        a1 = _mk_article(test_app, title="ok")
        a2 = _mk_article(test_app, title="rw")

        _mk_decision(test_app, article_id=a1, decision="approved")
        _mk_decision(test_app, article_id=a2, decision="needs_rewrite")

        db = test_app.session_factory()
        try:
            count, items = list_recent_decisions(
                db,
                decided_by="claude-goal-verifier",
                decision="approved",
                since_hours=24,
            )
            assert count == 1
            assert items[0]["title"] == "ok"
        finally:
            db.close()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_list_recent_decisions_model_label_filter(monkeypatch):
    """model_label='X' 只命中 article.metrics.writer_model == 'X' 的行。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.auto_review.service import list_recent_decisions

        a1 = _mk_article(test_app, title="goal-loop", writer_model="claude-goal-opus-4-7")
        a2 = _mk_article(test_app, title="other-loop", writer_model="claude-other")
        a3 = _mk_article(test_app, title="no-label")

        _mk_decision(test_app, article_id=a1)
        _mk_decision(test_app, article_id=a2)
        _mk_decision(test_app, article_id=a3)

        db = test_app.session_factory()
        try:
            count, items = list_recent_decisions(
                db,
                decided_by="claude-goal-verifier",
                decision="approved",
                since_hours=24,
                model_label="claude-goal-opus-4-7",
            )
            assert count == 1
            assert items[0]["title"] == "goal-loop"
        finally:
            db.close()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_list_recent_decisions_limit_caps_items_not_count(monkeypatch):
    """limit=2 但匹配 5 行：count=5、items 长度 2。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.auto_review.service import list_recent_decisions

        for i in range(5):
            aid = _mk_article(test_app, title=f"a{i}")
            _mk_decision(test_app, article_id=aid)

        db = test_app.session_factory()
        try:
            count, items = list_recent_decisions(
                db,
                decided_by="claude-goal-verifier",
                decision="approved",
                since_hours=24,
                limit=2,
            )
            assert count == 5
            assert len(items) == 2
        finally:
            db.close()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_today_loop_decisions_requires_mcp_token(monkeypatch):
    """无 X-MCP-Token → 401。"""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.get("/api/articles/today-loop-decisions")
        assert r.status_code == 401
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_today_loop_decisions_returns_count_and_items(monkeypatch):
    """有 token + 命中 2 条 → count=2, items=2，结构符合契约。"""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        a1 = _mk_article(test_app, title="x1")
        a2 = _mk_article(test_app, title="x2")
        _mk_decision(test_app, article_id=a1, score_total=82)
        _mk_decision(test_app, article_id=a2, score_total=75)

        r = test_app.client.get(
            "/api/articles/today-loop-decisions",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["data"]["count"] == 2
        items = body["data"]["items"]
        assert len(items) == 2
        assert {it["title"] for it in items} == {"x1", "x2"}
        assert all("decided_at" in it for it in items)
        assert all(it["article_id"] in {a1, a2} for it in items)
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_today_loop_decisions_since_hours_param(monkeypatch):
    """since_hours=1 → 2 小时前的 decision 不算。"""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        a1 = _mk_article(test_app, title="recent")
        a2 = _mk_article(test_app, title="2h-ago")
        _mk_decision(test_app, article_id=a1)
        _mk_decision(test_app, article_id=a2, created_at=utcnow() - timedelta(hours=2))

        r = test_app.client.get(
            "/api/articles/today-loop-decisions",
            params={"since_hours": 1},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["data"]["count"] == 1
        assert body["data"]["items"][0]["title"] == "recent"
    finally:
        test_app.cleanup()
