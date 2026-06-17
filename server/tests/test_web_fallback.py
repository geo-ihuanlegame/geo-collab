"""AI配图「联网兜底」测试。

纯逻辑用例（baidu 解析/横版过滤/magic-bytes/拼音 bucket）无需 DB，裸 pytest 即可跑；
集成用例 mock 掉 MinIO + 百度搜图，只需 MySQL（@pytest.mark.mysql）。
"""

import re

import httpx
import pytest

from server.app.shared import baidu

# ── 纯逻辑：无需 DB ──────────────────────────────────────────────────────────


def test_parse_image_references():
    data = {
        "references": [
            {
                "url": "http://news.example.com/a",
                "title": "原神风景",
                "image": {"url": "http://img.example.com/a.jpg", "width": "1920", "height": "1080"},
            },
            {"url": "http://x/b", "title": "无图引用"},  # 没有 image → 跳过
            {"image": {"url": "ftp://bad/c.jpg", "width": "10", "height": "10"}},  # 非 http → 跳过
        ]
    }
    out = baidu.parse_image_references(data)
    assert len(out) == 1
    assert out[0].url == "http://img.example.com/a.jpg"
    assert out[0].width == 1920 and out[0].height == 1080
    assert out[0].source_url == "http://news.example.com/a"


def test_landscape_only_filters_and_sorts():
    imgs = [
        baidu.BaiduImage("u1", 1200, 1800, "", ""),  # 竖 → 去掉
        baidu.BaiduImage("u2", 1920, 1080, "", ""),  # 横，面积大
        baidu.BaiduImage("u3", 800, 600, "", ""),  # 横，面积小
        baidu.BaiduImage("u4", 0, 0, "", ""),  # 无尺寸 → 去掉
        baidu.BaiduImage("u5", 1000, 1000, "", ""),  # 方 → 去掉
    ]
    out = baidu.landscape_only(imgs)
    assert [im.url for im in out] == ["u2", "u3"]


def test_sniff_image_mime():
    assert baidu.sniff_image_mime(b"\xff\xd8\xff\xe0\x00") == "image/jpeg"
    assert baidu.sniff_image_mime(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert baidu.sniff_image_mime(b"GIF89a....") == "image/gif"
    assert baidu.sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert baidu.sniff_image_mime(b"not an image at all") is None


def test_slugify_bucket():
    from server.app.modules.image_library.service import slugify_bucket

    assert slugify_bucket("蛋仔派对") == "danzaipaidui"
    # 只含小写字母和数字（符号被清掉，数字保留）
    assert slugify_bucket("原神 5.0!!") == "yuanshen50"
    assert re.fullmatch(r"[a-z0-9]+", slugify_bucket("Dota2 传说"))
    assert 3 <= len(slugify_bucket("a")) <= 63  # 过短回退补足
    assert len(slugify_bucket("龙" * 100)) <= 63  # 过长截断


def test_search_returns_empty_without_key(monkeypatch):
    # 没配 key → best-effort 返回 []，不抛。
    # 用 setenv 而非 setattr(Settings, ...)：config 走 env_file=".env"，本机/部署 .env 里可能真有
    # GEO_BAIDU_API_KEY。os.environ 优先级高于 .env 文件，setenv("") 才能真正模拟"没配 key"；
    # 改类属性压不住 env_file 来源的值，会误跑真实联网搜图（曾导致本用例只在带 .env 的机器上失败）。
    from server.app.core import config

    monkeypatch.setenv("GEO_BAIDU_API_KEY", "")
    config.get_settings.cache_clear()
    assert baidu.search_landscape_images("蛋仔派对") == []


def test_build_search_query_replaces_placeholder():
    # 含 {game} 占位符 → 替换为游戏名
    assert baidu.build_search_query("原神", "{game} 横版 官方宣传图") == "原神 横版 官方宣传图"
    assert baidu.build_search_query("原神", "官方 {game} 立绘") == "官方 原神 立绘"


def test_build_search_query_appends_without_placeholder():
    # 不含 {game} → 游戏名后空格拼接模板内容
    assert baidu.build_search_query("原神", "横版 官方宣传图") == "原神 横版 官方宣传图"


def test_build_search_query_falls_back_to_default_on_empty():
    # 空模板 → 回退默认模板
    assert baidu.build_search_query("原神", "") == "原神 横版 官方宣传图"
    assert baidu.build_search_query("原神", "   ") == "原神 横版 官方宣传图"
    assert baidu.build_search_query("原神") == "原神 横版 官方宣传图"


def test_web_fallback_fill_category_forwards_query_template(monkeypatch):
    # _web_fallback_fill_category 把可编辑搜索词透传给 baidu.search_landscape_images
    from types import SimpleNamespace

    from server.app.modules.articles.ai_format import _web_fallback_fill_category

    captured: dict = {}

    def fake_search(name, **kw):
        captured["name"] = name
        captured["kw"] = kw
        return []  # 返回空 → 不下载、不入库，函数返回 None

    monkeypatch.setattr(baidu, "search_landscape_images", fake_search)

    result = _web_fallback_fill_category(
        None, SimpleNamespace(name="原神"), "{game} 横版 官方宣传图"
    )
    assert result is None
    assert captured["name"] == "原神"
    assert captured["kw"] == {"query_template": "{game} 横版 官方宣传图"}


def test_web_fallback_fill_category_omits_query_template_when_none(monkeypatch):
    # 不传搜索词时不带 query_template kwarg，baidu 用自身默认
    from types import SimpleNamespace

    from server.app.modules.articles.ai_format import _web_fallback_fill_category

    captured: dict = {}

    def fake_search(name, **kw):
        captured["kw"] = kw
        return []

    monkeypatch.setattr(baidu, "search_landscape_images", fake_search)

    _web_fallback_fill_category(None, SimpleNamespace(name="原神"))
    assert captured["kw"] == {}


# ── 限流韧性：限速 + 429 重试退避 + 同名去重负缓存（无需 DB）─────────────────────


def _mk_response(status, json_body=None, retry_after=None):
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    req = httpx.Request("POST", "https://qianfan.baidubce.com/v2/ai_search/web_search")
    return httpx.Response(status, headers=headers, json=json_body or {}, request=req)


_OK_BODY = {
    "references": [
        {
            "url": "http://news/p",
            "image": {"url": "http://img/x.jpg", "width": "1920", "height": "1080"},
        }
    ]
}


def _set_baidu_env(monkeypatch, *, max_retries=3, min_interval=0.0):
    """配好 key + 限速/重试旋钮，并隔离负缓存、置空退避 sleep（避免真睡眠、跨用例污染）。"""
    from server.app.core import config

    monkeypatch.setenv("GEO_BAIDU_API_KEY", "test-key")
    monkeypatch.setenv("GEO_BAIDU_MAX_RETRIES", str(max_retries))
    monkeypatch.setenv("GEO_BAIDU_MIN_INTERVAL_SECONDS", str(min_interval))
    config.get_settings.cache_clear()
    monkeypatch.setattr(baidu, "sleep", lambda _s: None)
    monkeypatch.setattr(baidu, "_neg_cache", {})


def test_throttle_sleeps_to_enforce_min_interval(monkeypatch):
    # 距上次调用仅 0.4s，min_interval=1.0 → 还需补睡 0.6s
    slept = []
    monkeypatch.setattr(baidu, "monotonic", lambda: 100.0)
    monkeypatch.setattr(baidu, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(baidu, "_last_call_monotonic", 99.6)
    baidu._throttle(1.0)
    assert slept and slept[0] == pytest.approx(0.6, abs=1e-6)


def test_throttle_no_sleep_when_interval_elapsed(monkeypatch):
    # 距上次调用已很久 → 不睡
    slept = []
    monkeypatch.setattr(baidu, "monotonic", lambda: 200.0)
    monkeypatch.setattr(baidu, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(baidu, "_last_call_monotonic", 100.0)
    baidu._throttle(1.0)
    assert slept == []


def test_throttle_disabled_when_interval_non_positive(monkeypatch):
    slept = []
    monkeypatch.setattr(baidu, "sleep", lambda s: slept.append(s))
    baidu._throttle(0)
    assert slept == []


def test_search_retries_on_429_then_succeeds(monkeypatch):
    # 连续两次 429 → 第三次 200，最终拿到图，且确实重试了 3 发
    _set_baidu_env(monkeypatch, max_retries=3)
    seq = [
        _mk_response(429, retry_after=0),
        _mk_response(429, retry_after=0),
        _mk_response(200, _OK_BODY),
    ]
    calls = {"n": 0}

    def fake_post(*a, **k):
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(httpx, "post", fake_post)
    out = baidu.search_landscape_images("原神")
    assert calls["n"] == 3
    assert [im.url for im in out] == ["http://img/x.jpg"]


def test_search_gives_up_after_max_retries_on_429(monkeypatch):
    # 全程 429 → 返回 []，且只重试到上限（1 首发 + 2 重试）
    _set_baidu_env(monkeypatch, max_retries=2)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _mk_response(429, retry_after=0)

    monkeypatch.setattr(httpx, "post", fake_post)
    out = baidu.search_landscape_images("原神")
    assert out == []
    assert calls["n"] == 3


def test_search_neg_caches_failed_query_to_skip_repeat(monkeypatch):
    # 同名搜图失败后进负缓存，TTL 内再搜直接短路、不再打网络
    _set_baidu_env(monkeypatch, max_retries=0)
    monkeypatch.setattr(baidu, "monotonic", lambda: 1000.0)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _mk_response(429, retry_after=0)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert baidu.search_landscape_images("月兔漫游") == []
    assert calls["n"] == 1
    assert baidu.search_landscape_images("月兔漫游") == []
    assert calls["n"] == 1


def test_search_neg_cache_expires_after_ttl(monkeypatch):
    # 负缓存过 TTL 后失效 → 重新搜
    _set_baidu_env(monkeypatch, max_retries=0)
    monkeypatch.setenv("GEO_BAIDU_NEG_CACHE_SECONDS", "120")
    from server.app.core import config

    config.get_settings.cache_clear()
    clock = [1000.0]
    monkeypatch.setattr(baidu, "monotonic", lambda: clock[0])
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _mk_response(429, retry_after=0)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert baidu.search_landscape_images("小森灵") == []
    assert calls["n"] == 1
    clock[0] = 1000.0 + 121
    assert baidu.search_landscape_images("小森灵") == []
    assert calls["n"] == 2


# ── 集成：mock MinIO + 百度，仅需 MySQL ──────────────────────────────────────


@pytest.mark.mysql
def test_web_fallback_creates_category_and_inserts(monkeypatch):
    from server.tests.utils import build_test_app

    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.image_library import store as minio_store

        monkeypatch.setattr(minio_store, "ensure_bucket", lambda b: None)
        monkeypatch.setattr(minio_store, "upload_image", lambda *a, **k: None)
        monkeypatch.setattr(
            baidu,
            "search_landscape_images",
            lambda name, **k: [baidu.BaiduImage("http://i/x.jpg", 1920, 1080, "http://src", "t")],
        )
        monkeypatch.setattr(
            baidu, "download_image", lambda url: (b"\xff\xd8\xff\x00data", "image/jpeg")
        )

        from server.app.modules.articles.ai_format import _maybe_insert_images
        from server.app.modules.image_library.models import StockCategory, StockImage

        content = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "蛋仔派对真好玩"}]}
            ],
        }
        parsed = {"image_positions": [{"index": 0, "game": "蛋仔派对"}]}

        with app.session_factory() as db:
            new_doc, count = _maybe_insert_images(
                content, parsed, None, db, available_categories=[], web_fallback=True
            )
            assert count == 1
            cat = db.query(StockCategory).filter(StockCategory.name == "蛋仔派对").first()
            assert cat is not None and cat.kind == "companion"
            img = db.query(StockImage).filter(StockImage.category_id == cat.id).first()
            assert img is not None and "web_fallback" in (img.tags or [])

        # web_fallback 关：game 字段被忽略，不建栏目、不插图（向后兼容）
        with app.session_factory() as db:
            new_doc2, count2 = _maybe_insert_images(
                content,
                {"image_positions": [{"index": 0, "game": "王者荣耀"}]},
                None,
                db,
                available_categories=[],
                web_fallback=False,
            )
            assert count2 == 0
            assert db.query(StockCategory).filter(StockCategory.name == "王者荣耀").first() is None
    finally:
        app.cleanup()
