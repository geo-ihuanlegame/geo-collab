"""
文章搜索测试：title / author / plain_text 三列全文检索。

覆盖两条路径，注意它们语义 *不同*（见 issue #50 修复）：
- 短查询（< 3 字）：直接走 LIKE 子串匹配。
- 长查询（>= 3 字）：走 MySQL FULLTEXT（ngram 自然语言模式）；FULLTEXT 不可用（如索引缺失）
  时 except 回退 LIKE。FULLTEXT 是按 ngram bigram 模糊召回，比 LIKE 子串更宽（高召回、低精度）——
  例如「量子计算」会召回只含「量子力学」的文章。这条语义由
  test_fts_natural_language_recalls_by_shared_ngram 钉住；FTS 路径确实被走到（而非静默退化）
  由 test_fts_match_path_is_used_not_silently_degraded 钉住。
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
        """按正文关键词搜索应命中含该词的文章、且不返回完全无关的文章（真实 FTS 路径）。

        注意：负样本的正文/标题刻意与查询「量子计算」不共享任何 ngram bigram（量子/子计/计算），
        否则在 ngram 自然语言模式下会因共享 bigram 被模糊召回——那种「共享 bigram 即召回」的
        行为是 *预期* 的，单独由 test_fts_natural_language_recalls_by_shared_ngram 钉住。
        """
        test_app = build_test_app(monkeypatch)
        try:
            _create_article(
                test_app.client,
                title="普通标题",
                plain_text="这篇文章讨论了量子计算的基础原理",
            )
            _create_article(
                test_app.client,
                title="园艺主题",
                plain_text="一篇关于阳台种植与盆栽养护的文章",
            )

            resp = test_app.client.get("/api/articles?q=量子计算")
            assert resp.status_code == 200, resp.text
            articles = resp.json()
            titles = [a["title"] for a in articles]
            assert "普通标题" in titles, f"Expected '普通标题' in results, got: {titles}"
            assert "园艺主题" not in titles, f"无共享 bigram 的文章不应被召回，got: {titles}"
        finally:
            test_app.cleanup()

    def test_fts_natural_language_recalls_by_shared_ngram(self, monkeypatch):
        """钉住已接受的语义：ngram 自然语言模式按「共享 bigram」模糊召回（issue #50 修复后的行为）。

        这是有意为之的高召回（相对旧的 LIKE 子串精确匹配）：查询「量子计算」会召回正文只含
        「量子力学」的文章，因为二者共享 bigram「量子」。若未来有人把检索收紧成布尔 AND / 子串，
        本用例会失败，提醒那是一次语义变更而非无害重构。
        """
        test_app = build_test_app(monkeypatch)
        try:
            _create_article(
                test_app.client,
                title="物理前沿",
                plain_text="本文讨论量子力学的测量问题",  # 与「量子计算」共享 bigram「量子」
            )
            resp = test_app.client.get("/api/articles?q=量子计算")
            assert resp.status_code == 200, resp.text
            titles = [a["title"] for a in resp.json()]
            assert "物理前沿" in titles, (
                f"ngram 自然语言模式应按共享 bigram 模糊召回，got: {titles}"
            )
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

    def test_fts_match_path_is_used_not_silently_degraded(self, monkeypatch):
        """回归 issue #50：≥3 字查询必须真正走 MySQL FULLTEXT(MATCH...AGAINST)，
        而不是因 _search_articles 抛错被 list_articles 的 except 静默吞掉、退化成 LIKE。

        为什么不能用纯黑盒断言：FTS(ngram 自然语言) 和 LIKE 子串对「量子纠缠」这种查询
        返回结果一样，这正是 #50 长期没被发现的原因。所以这里 spy 包住真实 _search_articles，
        记录调用次数与是否抛异常——FTS 正常时 raised==0；一旦 MATCH 子句再次写坏（如 #50 的
        func.match().against()），它会抛、被 except 接住回退 LIKE，本断言 raised==0 即抓回该回归。
        """
        from server.app.modules.articles import service as svc

        real = svc._search_articles
        calls = {"n": 0, "raised": 0}

        def spy(db, query, user_id=None):
            calls["n"] += 1
            try:
                return real(db, query, user_id=user_id)
            except Exception:
                calls["raised"] += 1
                raise

        monkeypatch.setattr(svc, "_search_articles", spy)

        test_app = build_test_app(monkeypatch)
        try:
            _create_article(test_app.client, title="量子纠缠研究综述", plain_text="正文内容")
            _create_article(test_app.client, title="无关主题", plain_text="别的内容")

            resp = test_app.client.get("/api/articles?q=量子纠缠")
            assert resp.status_code == 200, resp.text
            assert calls["n"] >= 1, "≥3 字查询应调用 FTS 路径 _search_articles"
            assert calls["raised"] == 0, "FTS MATCH 子句抛异常→静默退化 LIKE（issue #50 回归）"
            titles = [a["title"] for a in resp.json()]
            assert "量子纠缠研究综述" in titles
            assert "无关主题" not in titles
        finally:
            test_app.cleanup()

    def test_search_query_with_boolean_operator_chars_does_not_error(self, monkeypatch):
        """自然语言模式下，查询里含 + - " * ( ) 等字符不应 500/报错（它们被当词分隔符，非操作符）。

        这是选自然语言模式而非布尔模式的关键收益：用户随手输入特殊字符不会触发
        FULLTEXT syntax error，也不需要前置转义。
        """
        test_app = build_test_app(monkeypatch)
        try:
            _create_article(test_app.client, title="C++ 与 Rust 对比", plain_text="系统编程语言")

            for q in ["C++ Rust", "a -b +c", '"未配对 (括号']:
                resp = test_app.client.get("/api/articles", params={"q": q})
                assert resp.status_code == 200, f"q={q!r} -> {resp.status_code}: {resp.text}"
        finally:
            test_app.cleanup()
