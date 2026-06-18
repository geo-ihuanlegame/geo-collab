import json

from server.app.modules.auto_review.schemas import AutoReviewSubmitRequest
from server.app.modules.auto_review.service import submit_decision
from server.tests.utils import build_test_app


def test_submit_decision_persists(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        # 先建一篇 article
        from server.app.modules.articles.models import Article

        db = test_app.session_factory()
        try:
            a = Article(
                user_id=test_app.admin_id,
                title="test",
                content_json=json.dumps({"type": "doc", "content": []}),
                content_html="",
                plain_text="",
                word_count=0,
                status="draft",
                review_status="pending",
            )
            db.add(a)
            db.commit()
            article_id = a.id
        finally:
            db.close()

        db = test_app.session_factory()
        try:
            decision = submit_decision(
                db,
                article_id,
                AutoReviewSubmitRequest(
                    decision="approved",
                    score_total=85,
                    score_breakdown={"factuality": 90, "readability": 80},
                    reasoning="reads well",
                    decided_by="claude-code-loop",
                ),
            )
            db.commit()
            assert decision.id is not None
            assert decision.decision == "approved"
            assert decision.score_breakdown == {"factuality": 90, "readability": 80}
        finally:
            db.close()
    finally:
        test_app.cleanup()


def test_score_articles_returns_one_per_input(monkeypatch):
    """评分接受一组 article_id，对每个返回一条结果（含失败兜底）。"""

    # mock litellm.completion
    def fake_completion(*args, **kwargs):
        class _Choice:
            message = type("m", (), {"content": (
                '{"score_breakdown": {"factuality": 85, "readability": 80, "style": 75, "policy_safety": 90},'
                ' "score_total": 82, "suggested_decision": "approved", "reasoning": "looks ok"}'
            )})()
        class _Resp:
            choices = [_Choice()]
        return _Resp()

    monkeypatch.setattr("litellm.completion", fake_completion)

    # mock resolve_ai_format_model
    monkeypatch.setattr(
        "server.app.modules.ai_models.service.resolve_ai_format_model",
        lambda db, selected=None: ("deepseek/deepseek-v4-flash", "fake-key", None, 60),
    )

    test_app = build_test_app(monkeypatch)
    try:
        # 建 2 篇 article
        from server.app.modules.articles.models import Article
        db = test_app.session_factory()
        try:
            articles = []
            for i in range(2):
                a = Article(
                    user_id=test_app.admin_id,
                    title=f"t{i}",
                    content_json=json.dumps({"type": "doc", "content": []}),
                    content_html="",
                    plain_text=f"body {i} " * 50,
                    word_count=100,
                    status="draft",
                    review_status="pending",
                )
                db.add(a)
                articles.append(a)
            db.commit()
            ids = [a.id for a in articles]
        finally:
            db.close()

        from server.app.modules.auto_review.schemas import ScoreRequest
        from server.app.modules.auto_review.service import score_articles

        db = test_app.session_factory()
        try:
            results = score_articles(db, ScoreRequest(article_ids=ids))
            assert len(results) == 2
            assert all(r.score_total == 82 for r in results)
            assert all(r.suggested_decision == "approved" for r in results)
        finally:
            db.close()
    finally:
        test_app.cleanup()


def test_score_articles_returns_failure_for_invalid_json(monkeypatch):
    """LLM 输出非 JSON → 该条返回 score_total=None + reasoning 含 [评分失败]。"""

    def fake_completion(*args, **kwargs):
        class _Choice:
            message = type("m", (), {"content": "this is not json"})()
        class _Resp:
            choices = [_Choice()]
        return _Resp()

    monkeypatch.setattr("litellm.completion", fake_completion)
    monkeypatch.setattr(
        "server.app.modules.ai_models.service.resolve_ai_format_model",
        lambda db, selected=None: ("any-model", "k", None, 60),
    )

    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.models import Article
        db = test_app.session_factory()
        try:
            a = Article(
                user_id=test_app.admin_id, title="x",
                content_json=json.dumps({"type": "doc", "content": []}),
                content_html="", plain_text="text",
                word_count=4, status="draft", review_status="pending",
            )
            db.add(a); db.commit()
            aid = a.id
        finally:
            db.close()

        from server.app.modules.auto_review.schemas import ScoreRequest
        from server.app.modules.auto_review.service import score_articles
        db = test_app.session_factory()
        try:
            results = score_articles(db, ScoreRequest(article_ids=[aid]))
            assert len(results) == 1
            assert results[0].score_total is None or results[0].score_total < 0
            assert "[评分失败]" in results[0].reasoning
        finally:
            db.close()
    finally:
        test_app.cleanup()
