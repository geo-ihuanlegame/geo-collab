"""
Phase 5 search tests: plain_text included in article search.

Tests cover both the MySQL FULLTEXT path and the LIKE fallback path. The fallback
is exercised when the query length is < 3 characters or when FULLTEXT raises.

We cover both paths:
- Short query (< 3 chars): always uses LIKE
- Long query (>= 3 chars): attempts MySQL FULLTEXT first; if it is unavailable it falls
  through to LIKE. We create articles with distinctive keywords so both paths
  produce the same result.
"""

from io import BytesIO

from server.tests.utils import build_test_app

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _upload_cover(client) -> str:
    resp = client.post("/api/assets", files={"file": ("cover.png", BytesIO(_PNG), "image/png")})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_article(
    client, *, title: str = "Untitled", author: str = "", plain_text: str = ""
) -> int:
    cover_id = _upload_cover(client)
    payload = {
        "title": title,
        "author": author or None,
        "content_json": {"type": "doc", "content": []},
        "content_html": "",
        "plain_text": plain_text,
        "cover_asset_id": cover_id,
    }
    resp = client.post("/api/articles", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestArticleSearch:
    def test_search_by_title_finds_article(self, monkeypatch):
        """按标题搜索应能找到对应文章。"""
        test_app = build_test_app(monkeypatch)
        try:
            _create_article(test_app.client, title="Python 编程指南", plain_text="一些正文内容")
            _create_article(test_app.client, title="无关文章", plain_text="不相关的内容")

            resp = test_app.client.get("/api/articles?q=Python")
            assert resp.status_code == 200, resp.text
            articles = resp.json()
            titles = [a["title"] for a in articles]
            assert "Python 编程指南" in titles
            assert "无关文章" not in titles
        finally:
            test_app.cleanup()

    def test_search_by_plain_text_finds_article(self, monkeypatch):
        """按正文关键词搜索应能找到对应文章（LIKE fallback 路径）。"""
        test_app = build_test_app(monkeypatch)
        try:
            _create_article(
                test_app.client,
                title="普通标题",
                plain_text="这篇文章讨论了量子计算的基础原理",
            )
            _create_article(
                test_app.client,
                title="另一篇文章",
                plain_text="这篇文章和量子力学无关",
            )

            # Use a short query (< 3 chars would skip FTS, use LIKE) — but we
            # want to test LIKE specifically, so use a unique keyword that is
            # >= 2 chars but also present only in one article's plain_text.
            # We trigger LIKE by using a query that's exactly 2 chars.
            # Actually let's use a unique substring in the plain_text that is
            # long enough to also exercise the FTS path; both should work.
            resp = test_app.client.get("/api/articles?q=量子计算")
            assert resp.status_code == 200, resp.text
            articles = resp.json()
            titles = [a["title"] for a in articles]
            assert "普通标题" in titles, f"Expected '普通标题' in results, got: {titles}"
            assert "另一篇文章" not in titles
        finally:
            test_app.cleanup()

    def test_plain_text_like_fallback_with_short_query(self, monkeypatch):
        """查询长度 < 3 时直接走 LIKE 路径，plain_text 应被包含在搜索范围内。"""
        test_app = build_test_app(monkeypatch)
        try:
            # Use a two-character keyword that appears only in plain_text (not title/author)
            _create_article(
                test_app.client,
                title="普通标题A",
                plain_text="AB正文内容很独特",
            )
            _create_article(
                test_app.client,
                title="普通标题B",
                plain_text="完全不同的正文",
            )

            # 2-char query forces LIKE path
            resp = test_app.client.get("/api/articles?q=AB")
            assert resp.status_code == 200, resp.text
            articles = resp.json()
            titles = [a["title"] for a in articles]
            assert "普通标题A" in titles, f"Expected '普通标题A' in results, got: {titles}"
            assert "普通标题B" not in titles
        finally:
            test_app.cleanup()

    def test_no_duplicate_when_keyword_in_title_and_body(self, monkeypatch):
        """标题和正文同时匹配时，文章不应重复返回。"""
        test_app = build_test_app(monkeypatch)
        try:
            _create_article(
                test_app.client,
                title="机器学习入门",
                plain_text="机器学习是人工智能的一个重要分支",
            )

            resp = test_app.client.get("/api/articles?q=机器学习")
            assert resp.status_code == 200, resp.text
            articles = resp.json()
            ids = [a["id"] for a in articles]
            assert len(ids) == len(set(ids)), f"Duplicate articles returned: {ids}"
        finally:
            test_app.cleanup()

    def test_empty_query_returns_all_articles(self, monkeypatch):
        """空 query 参数应返回全部文章。"""
        test_app = build_test_app(monkeypatch)
        try:
            _create_article(test_app.client, title="文章一", plain_text="内容一")
            _create_article(test_app.client, title="文章二", plain_text="内容二")
            _create_article(test_app.client, title="文章三", plain_text="内容三")

            resp = test_app.client.get("/api/articles")
            assert resp.status_code == 200, resp.text
            articles = resp.json()
            assert len(articles) >= 3
        finally:
            test_app.cleanup()

    def test_no_match_returns_empty_list(self, monkeypatch):
        """无匹配结果时应返回空列表。"""
        test_app = build_test_app(monkeypatch)
        try:
            _create_article(test_app.client, title="已有文章", plain_text="普通内容")

            resp = test_app.client.get("/api/articles?q=XYZZY_NONEXISTENT_KEYWORD")
            assert resp.status_code == 200, resp.text
            articles = resp.json()
            assert articles == [], f"Expected empty list, got: {articles}"
        finally:
            test_app.cleanup()
