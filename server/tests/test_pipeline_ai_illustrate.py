"""pipeline ai_illustrate 节点 snapshot 测试.

Task 2 把节点内部 _format_one + _maybe_set_cover 抽到 ai_illustrate_svc.illustrate_one；
此测试通过 mock service 返指定 IllustrateResult，断言节点 NodeResult.output 6 字段
schema 完全不变（前端 / agent_run_logs 展示逻辑零改动）.
"""

from __future__ import annotations

import json

import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_ai_illustrate_node_output_schema_stable(monkeypatch):
    """节点 output 含 6 个固定 key：article_ids / errors / images_inserted /
    format_errors / covers_set / cover_errors."""
    test_app = build_test_app(monkeypatch)
    try:
        # 准备 2 篇 article id 走 mock service
        from server.app.modules.articles.ai_illustrate_svc import IllustrateResult
        from server.app.modules.articles.models import Article
        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        db = test_app.session_factory()
        try:
            a1 = Article(
                user_id=test_app.admin_id,
                title="t1",
                content_json=json.dumps(
                    {
                        "type": "doc",
                        "content": [
                            {
                                "type": "heading",
                                "attrs": {"level": 2},
                                "content": [{"type": "text", "text": "h"}],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                content_html="",
                plain_text="",
                word_count=1,
                status="draft",
                review_status="pending",
            )
            a2 = Article(
                user_id=test_app.admin_id,
                title="t2",
                content_json=json.dumps(
                    {
                        "type": "doc",
                        "content": [
                            {
                                "type": "heading",
                                "attrs": {"level": 2},
                                "content": [{"type": "text", "text": "h"}],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                content_html="",
                plain_text="",
                word_count=1,
                status="draft",
                review_status="pending",
            )
            db.add(a1)
            db.add(a2)
            db.commit()
            ids = [a1.id, a2.id]
        finally:
            db.close()

        # mock service：第一篇 set 封面 + 2 张图，第二篇 cover error
        def fake_illustrate_one(*, article_id, **kwargs):
            if article_id == ids[0]:
                return IllustrateResult(
                    article_id=article_id, images_inserted=2, cover_status="set"
                )
            return IllustrateResult(
                article_id=article_id,
                images_inserted=1,
                cover_status="error",
                cover_error="minio down",
            )

        monkeypatch.setattr(
            "server.app.modules.pipelines.nodes.ai_illustrate.illustrate_one",
            fake_illustrate_one,
        )

        ctx = NodeRunContext(
            session_factory=test_app.session_factory,
            user_id=test_app.admin_id,
            config={"main_category_id": 1},  # 必填
            inputs={"article_ids": ids},
            upstream={},
        )
        result = run_ai_illustrate(ctx)

        output = result.output
        # 6 个固定字段都在
        assert set(output.keys()) >= {
            "article_ids",
            "errors",
            "images_inserted",
            "format_errors",
            "covers_set",
            "cover_errors",
        }
        assert output["article_ids"] == ids
        assert output["images_inserted"] == 3  # 2 + 1
        assert output["covers_set"] == 1  # 只 a1
        assert any("minio down" in e for e in output["cover_errors"])
        assert output["errors"] == []  # 没未捕获异常
    finally:
        test_app.cleanup()
