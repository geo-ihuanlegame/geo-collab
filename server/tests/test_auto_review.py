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
