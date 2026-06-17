"""Task 1a —— run_ai_format 连接持有纪律：慢 IO（LLM）期间不得持有 DB 连接。

确定性断言（无 sleep、无定时采样）：单线程调用 run_ai_format，在被 mock 的
_call_litellm_completion 入口抓 engine.pool.checkedout()——
- 改造前：run_ai_format 在第一段开 session 一路持到 LLM 之后，此处为 1（RED）。
- 三段式改造后：第一段已 close，LLM 期间不持连接，此处为 0（GREEN）。

只覆盖 web_fallback=False（scheme 配图 / 手动排版）；web_fallback=True 的下载剥离是 Task 1b。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server.tests.utils import build_test_app


def _fake_completion(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _create_locked_article(test_app) -> tuple[int, datetime]:
    from server.app.modules.articles.models import Article

    resp = test_app.client.post(
        "/api/articles",
        json={
            "title": "conn-lifecycle",
            "content_json": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "正文段落一"}]}
                ],
            },
        },
    )
    assert resp.status_code == 200
    article_id = resp.json()["id"]
    lock_started_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    with test_app.session_factory() as db:
        article = db.get(Article, article_id)
        article.ai_checking = True
        article.ai_checking_started_at = lock_started_at
        db.commit()
    return article_id, lock_started_at


@pytest.mark.mysql
def test_no_db_connection_held_during_llm_call(monkeypatch):
    monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "test-key")
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_format import run_ai_format

        article_id, lock_started_at = _create_locked_article(test_app)

        captured: dict[str, int] = {}

        def _probe_llm(**_):
            captured["checked_out_during_llm"] = test_app.engine.pool.checkedout()
            return _fake_completion('{"heading_indices": []}')

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion", _probe_llm
        )

        # 前置：池静止，无遗留 checkout
        assert test_app.engine.pool.checkedout() == 0

        run_ai_format(article_id, include_images=False, lock_started_at=lock_started_at)

        assert captured["checked_out_during_llm"] == 0, (
            f"run_ai_format held {captured.get('checked_out_during_llm')} DB connection(s) during "
            f"the LLM call — 慢 IO 期间不得占用池连接（Task 1a）"
        )
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_llm_error_sets_format_error_and_unlocks(monkeypatch):
    """回归护栏：LLM 抛错时仍须落 ai_format_error 并解锁（改造后异常路径走短 session）。"""
    monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "test-key")
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_format import run_ai_format
        from server.app.modules.articles.models import Article

        article_id, lock_started_at = _create_locked_article(test_app)

        def _boom(**_):
            raise RuntimeError("llm exploded")

        monkeypatch.setattr("server.app.modules.articles.ai_format._call_litellm_completion", _boom)

        run_ai_format(article_id, include_images=False, lock_started_at=lock_started_at)

        with test_app.session_factory() as db:
            article = db.get(Article, article_id)
            assert article.ai_checking is False
            assert article.ai_checking_started_at is None
            assert article.ai_format_error is not None
    finally:
        test_app.cleanup()
