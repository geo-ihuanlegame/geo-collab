"""AI配图「联网兜底」测试。

纯逻辑用例（baidu 解析/横版过滤/magic-bytes/拼音 bucket）无需 DB，裸 pytest 即可跑；
集成用例 mock 掉 MinIO + 百度搜图，只需 MySQL（@pytest.mark.mysql）。
"""

import re

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
    # 没配 key → best-effort 返回 []，不抛
    from server.app.core import config

    monkeypatch.setattr(config.Settings, "baidu_api_key", "", raising=False)
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
