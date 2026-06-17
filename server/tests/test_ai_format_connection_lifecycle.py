"""Task 1a/1b —— run_ai_format 连接持有纪律：慢 IO（LLM + 联网下载）期间不得持有 DB 连接。

确定性断言（无 sleep、无定时采样）：单线程调用 run_ai_format，在被 mock 的
慢 IO 入口抓 engine.pool.checkedout()——
- 改造前：run_ai_format 在第一段开 session 一路持到慢 IO 之后，此处为 ≥1（RED）。
- 分段改造后：相关段已 close，慢 IO 期间不持连接，此处为 0（GREEN）。

Task 1a 覆盖 web_fallback=False（scheme 配图 / 手动排版）的 LLM 调用；
Task 1b 覆盖 web_fallback=True（AI配图 节点）的 LLM 调用 + 联网搜图下载两段慢 IO。
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


# ── Task 1b：web_fallback=True 路径（LLM + 联网搜图下载两段慢 IO）─────────────────


def _stub_minio(monkeypatch):
    """把 MinIO 建桶/上传打桩成空操作，让 store_image_bytes / 建陪衬栏目 不触网。"""
    monkeypatch.setattr("server.app.modules.image_library.store.ensure_bucket", lambda bucket: None)
    monkeypatch.setattr(
        "server.app.modules.image_library.store.upload_image",
        lambda bucket, key, data, content_type: None,
    )


@pytest.mark.mysql
def test_no_db_connection_held_during_web_fallback_download(monkeypatch):
    """Task 1b 核心断言：web_fallback=True 下，联网搜图下载这段慢 IO 期间池里 0 连接被占。

    模型点名一个【不在候选栏目】的陪衬游戏 → 触发 get-or-create 陪衬栏目 + 联网搜图 + 下载。
    探针挂在 baidu.download_image（最慢的网络段）入口抓 checkedout()。
    旧单 session 路径整段持一条连接 → 此处 ≥1（RED）；两遍式剥离后 → 0（GREEN）。
    """
    monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "test-key")
    monkeypatch.setenv("GEO_BAIDU_API_KEY", "test-baidu-key")
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_format import run_ai_format
        from server.app.shared.baidu import BaiduImage

        article_id, lock_started_at = _create_locked_article(test_app)

        captured: dict[str, int] = {}

        def _probe_llm(**_):
            captured["checked_out_during_llm"] = test_app.engine.pool.checkedout()
            # 模型点名库里没有的陪衬游戏（无 category_id）→ 走联网兜底补图
            return _fake_completion(
                '{"heading_indices": [], "image_positions": [{"index": 0, "game": "测试游戏"}]}'
            )

        def _fake_search(name, **_):
            return [
                BaiduImage(
                    url="https://example.com/a.jpg",
                    width=1920,
                    height=1080,
                    source_url="https://example.com/page",
                    title="t",
                )
            ]

        def _probe_download(url):
            captured["checked_out_during_download"] = test_app.engine.pool.checkedout()
            return (b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion", _probe_llm
        )
        monkeypatch.setattr("server.app.shared.baidu.search_landscape_images", _fake_search)
        monkeypatch.setattr("server.app.shared.baidu.download_image", _probe_download)
        _stub_minio(monkeypatch)

        # 前置：池静止，无遗留 checkout
        assert test_app.engine.pool.checkedout() == 0

        run_ai_format(
            article_id,
            include_images=True,
            web_fallback=True,
            lock_started_at=lock_started_at,
            candidate_categories=[],
        )

        # 探针确实被触发（否则断言形同虚设）
        assert "checked_out_during_download" in captured, (
            "联网下载探针未被触发——web_fallback 取图路径没跑到，测试无效"
        )
        assert captured["checked_out_during_llm"] == 0, (
            f"run_ai_format held {captured.get('checked_out_during_llm')} DB connection(s) during "
            f"the LLM call (web_fallback=True) — 慢 IO 期间不得占用池连接（Task 1b）"
        )
        assert captured["checked_out_during_download"] == 0, (
            f"run_ai_format held {captured.get('checked_out_during_download')} DB connection(s) "
            f"during the web-fallback image download — 慢 IO 期间不得占用池连接（Task 1b）"
        )
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_web_fallback_inserts_downloaded_image_and_unlocks(monkeypatch):
    """行为护栏：web_fallback 取图成功后图片入文档、版本+1、锁释放（剥离重构不丢配图语义）。"""
    monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "test-key")
    monkeypatch.setenv("GEO_BAIDU_API_KEY", "test-baidu-key")
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_format import run_ai_format
        from server.app.modules.articles.models import Article
        from server.app.modules.articles.parser import loads_content_json
        from server.app.shared.baidu import BaiduImage

        article_id, lock_started_at = _create_locked_article(test_app)

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion",
            lambda **_: _fake_completion(
                '{"heading_indices": [], "image_positions": [{"index": 0, "game": "测试游戏"}]}'
            ),
        )
        monkeypatch.setattr(
            "server.app.shared.baidu.search_landscape_images",
            lambda name, **_: [
                BaiduImage(
                    url="https://example.com/a.jpg",
                    width=1920,
                    height=1080,
                    source_url="https://example.com/page",
                    title="t",
                )
            ],
        )
        monkeypatch.setattr(
            "server.app.shared.baidu.download_image",
            lambda url: (b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg"),
        )
        _stub_minio(monkeypatch)

        inserted = run_ai_format(
            article_id,
            include_images=True,
            web_fallback=True,
            lock_started_at=lock_started_at,
            candidate_categories=[],
        )

        assert inserted == 1
        with test_app.session_factory() as db:
            article = db.get(Article, article_id)
            assert article.ai_checking is False
            assert article.ai_checking_started_at is None
            content = loads_content_json(article.content_json)
            assert any(
                isinstance(n, dict) and n.get("type") == "image"
                for n in content.get("content") or []
            )
    finally:
        test_app.cleanup()
