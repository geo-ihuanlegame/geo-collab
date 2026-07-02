"""内容列表返回 MCP 自评分（auto_review_score）。

方式 A（读透展示）：分数不落 articles 表，列表端点从 auto_review_decisions
取每篇最新一条 score_total 暴露给前端卡片。只有 MCP loop/goal 写这张表，
故手动/pipeline/方案文章无分（None）。
"""

import json

import pytest

from server.app.modules.auto_review.schemas import AutoReviewSubmitRequest
from server.app.modules.auto_review.service import submit_decision
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _make_article(test_app, title: str) -> int:
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
        )
        db.add(a)
        db.commit()
        return a.id
    finally:
        db.close()


def test_list_includes_latest_auto_review_score(monkeypatch):
    """多条决策时，列表返回最新一条（id 最大）的 score_total。"""
    test_app = build_test_app(monkeypatch)
    try:
        article_id = _make_article(test_app, "scored article")

        db = test_app.session_factory()
        try:
            # 先 60（needs_rewrite），后 85（approved）——最新一条应为 85
            submit_decision(
                db,
                article_id,
                AutoReviewSubmitRequest(
                    decision="needs_rewrite", score_total=60, decided_by="claude-code-loop"
                ),
            )
            db.commit()
            submit_decision(
                db,
                article_id,
                AutoReviewSubmitRequest(
                    decision="approved", score_total=85, decided_by="claude-goal-verifier"
                ),
            )
            db.commit()
        finally:
            db.close()

        r = test_app.client.get("/api/articles")
        assert r.status_code == 200
        rows = {row["id"]: row for row in r.json()}
        assert rows[article_id]["auto_review_score"] == 85
    finally:
        test_app.cleanup()


def test_list_score_none_without_decision(monkeypatch):
    """没有自评记录的文章（手动/pipeline/方案）auto_review_score 为 None。"""
    test_app = build_test_app(monkeypatch)
    try:
        article_id = _make_article(test_app, "unscored article")

        r = test_app.client.get("/api/articles")
        assert r.status_code == 200
        rows = {row["id"]: row for row in r.json()}
        assert article_id in rows
        assert rows[article_id]["auto_review_score"] is None
    finally:
        test_app.cleanup()
