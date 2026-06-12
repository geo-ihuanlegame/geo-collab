"""测试 AI 排版锁处理与正文小标题转换。"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server.app.modules.articles.ai_format import (
    _apply_headings,
    _derive_html_and_text,
    _node_text,
    _top_level_text_nodes,
)
from server.tests.utils import build_test_app


def _create_article(client, content_json: dict | None = None) -> dict:
    response = client.post(
        "/api/articles",
        json={
            "title": "AI format test article",
            "content_json": content_json
            or {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
            },
        },
    )
    assert response.status_code == 200
    return response.json()


def _fake_completion(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _wait_until_unlocked(test_app, article_id: int, timeout: float = 3.0) -> None:
    from server.app.modules.articles.models import Article

    deadline = time.time() + timeout
    while time.time() < deadline:
        with test_app.session_factory() as db:
            article = db.get(Article, article_id)
            if article is not None and not article.ai_checking:
                return
        time.sleep(0.05)
    raise AssertionError("article stayed ai_checking=True")


@pytest.mark.mysql
def test_edit_locked_article_returns_409(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = datetime.now(UTC).replace(tzinfo=None)
            db.commit()

        response = client.put(
            f"/api/articles/{article_id}",
            json={"title": "changed title"},
        )
        assert response.status_code == 409
        assert "AI" in response.json()["detail"]
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_delete_locked_article_returns_409(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = datetime.now(UTC).replace(tzinfo=None)
            db.commit()

        response = client.delete(f"/api/articles/{article_id}")
        assert response.status_code == 409
        assert "AI" in response.json()["detail"]
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_edit_expired_lock_allows_update(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article

        expired_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=121)

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = expired_time
            db.commit()

        response = client.put(
            f"/api/articles/{article_id}",
            json={"title": "expired lock update"},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "expired lock update"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_read_expired_lock_clears_ai_checking(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article

        expired_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=121)
        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = expired_time
            db.commit()

        response = client.get(f"/api/articles/{article_id}")
        assert response.status_code == 200
        assert response.json()["ai_checking"] is False

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            assert db_article.ai_checking is False
            assert db_article.ai_checking_started_at is None
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_edit_unlocked_article_succeeds(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        response = client.put(
            f"/api/articles/{article_id}",
            json={"title": "normal update"},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "normal update"
    finally:
        test_app.cleanup()


def test_top_level_text_nodes_returns_paragraphs_and_headings():
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 1}, "content": []},
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
            {"type": "image", "attrs": {"src": "/x.png"}},
        ],
    }
    result = _top_level_text_nodes(doc)
    assert [item[0] for item in result] == [0, 1]


def test_node_text_joins_text_and_hard_break_nodes():
    node = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "Hello"},
            {"type": "hardBreak"},
            {"type": "text", "text": "World"},
        ],
    }
    assert _node_text(node) == "Hello\nWorld"


def test_apply_headings_converts_paragraph_to_h2():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Title"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Body"}]},
        ],
    }
    result = _apply_headings(doc, heading_indices={0})
    assert result["content"][0]["type"] == "heading"
    assert result["content"][0]["attrs"]["level"] == 2
    assert result["content"][1]["type"] == "paragraph"


def test_apply_headings_preserves_unselected_heading():
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 1},
                "content": [{"type": "text", "text": "Too long sentence."}],
            },
        ],
    }
    result = _apply_headings(doc, heading_indices=set())
    assert result["content"][0]["type"] == "heading"
    assert result["content"][0]["attrs"]["level"] == 1


def test_derive_html_and_text_generates_correct_output():
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 1},
                "content": [{"type": "text", "text": "Title"}],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "Body"}]},
        ],
    }
    html, text = _derive_html_and_text(doc)
    assert html == "<h1>Title</h1><p>Body</p>"
    assert "Title" in text
    assert "Body" in text


@pytest.mark.mysql
def test_ai_format_empty_indices_releases_lock_without_changing_content(monkeypatch):
    test_app = build_test_app(monkeypatch)

    try:
        article = _create_article(test_app.client)
        article_id = article["id"]

        from server.app.modules.articles.ai_format import run_ai_format
        from server.app.modules.articles.models import Article

        lock_started_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            original_content = db_article.content_json
            db_article.ai_checking = True
            db_article.ai_checking_started_at = lock_started_at
            db.commit()

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion",
            lambda **_: _fake_completion('{"heading_indices": []}'),
        )

        run_ai_format(article_id, include_images=False, lock_started_at=lock_started_at)

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            assert db_article.ai_checking is False
            assert db_article.ai_checking_started_at is None
            assert db_article.content_json == original_content
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_ai_format_button_path_triggers_image_insertion_when_categories_selected(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(test_app.client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article
        from server.app.modules.image_library.models import StockCategory

        with test_app.session_factory() as db:
            category = StockCategory(name="covers", bucket_name="covers")
            db.add(category)
            db.flush()
            db_article = db.get(Article, article_id)
            db_article.stock_categories = [category]  # 多对多关联
            db.commit()

        image_insert_called = False

        def fake_maybe_insert_images(*args, **kwargs):
            nonlocal image_insert_called
            image_insert_called = True
            return args[0], 0

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion",
            lambda **_: _fake_completion(
                '{"heading_indices": [0], "image_positions": [{"index": 0, "hint": "风景描写"}]}'
            ),
        )
        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._maybe_insert_images", fake_maybe_insert_images
        )
        # 流程会从环境重新读取配置（get_settings.cache_clear），并且在模拟模型调用前要求有 API key。
        # 这里显式设置，避免测试依赖开发机环境中的 GEO_AI_* 变量。
        monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "test-key")

        response = client.post(f"/api/articles/{article_id}/ai-format")
        assert response.status_code == 202
        _wait_until_unlocked(test_app, article_id)

        assert image_insert_called is True
        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            assert "image" not in db_article.content_json
    finally:
        test_app.cleanup()


# ── _maybe_insert_images 单元测试（不依赖数据库）────────────────────────────


def _make_article_stub(stock_category_id=None, stock_categories=None):
    """构造 Article stub，模拟 ORM 对象的关键属性。"""
    return SimpleNamespace(
        stock_category_id=stock_category_id,
        stock_categories=stock_categories or [],
    )


def _simple_content():
    """最小化的 Tiptap doc，包含两个段落节点（索引 0 和 1）。"""
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "段落一"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "段落二"}]},
        ],
    }


def test_render_ai_format_prompt_injects_category_names_without_private_fields():
    from server.app.modules.articles.ai_format import render_ai_format_prompt

    text_nodes = [
        (0, {"type": "paragraph", "content": [{"type": "text", "text": "原神战斗画面很好看"}]})
    ]
    template = (
        "{% for category in available_categories %}"
        "{{ category.id }} {{ category.name }} {{ category.description }} "
        "{% endfor %}"
        "{% for node in text_nodes %}{{ node.index }} {{ node.text }}{% endfor %}"
    )
    prompt = render_ai_format_prompt(
        template,
        text_nodes=text_nodes,
        available_categories=[{"id": 12, "name": "原神", "description": "开放世界"}],
    )

    assert "12 原神 开放世界" in prompt
    assert "原神战斗画面" in prompt
    assert "bucket" not in prompt
    assert "official_url" not in prompt


def test_headings_only_prompt_includes_node_text():
    """_SYSTEM_PROMPT_HEADINGS_ONLY 是 Jinja 模板，渲染后应包含实际文章节点文本。"""
    from server.app.modules.articles.ai_format import _fallback_prompt

    text_nodes = [
        (0, {"type": "paragraph", "content": [{"type": "text", "text": "第一段正文内容"}]}),
        (1, {"type": "paragraph", "content": [{"type": "text", "text": "第二段正文内容"}]}),
    ]
    prompt = _fallback_prompt(include_images=False, text_nodes=text_nodes)

    assert "第一段正文内容" in prompt
    assert "第二段正文内容" in prompt
    assert "0 [段落]" in prompt
    assert "1 [段落]" in prompt


def test_image_prompt_offers_game_field_when_web_fallback_on():
    """根因修复：自动建陪衬栏目这条路必须被提示词【正文】鼓励、带示例，而不是只靠末尾弱后缀。

    web_fallback=True 时组装出的配图提示词应包含 game 字段用法（让模型点名列表外游戏）；
    关时不出现（向后兼容，纯 category_id 行为）。覆盖 #76 之后的 AI配图 联网兜底根因。
    """
    from server.app.modules.articles.ai_format import _load_ai_format_prompt

    text_nodes = [
        (0, {"type": "paragraph", "content": [{"type": "text", "text": "某款手游真好玩"}]})
    ]
    cats = [{"id": 5, "name": "主推A", "description": None}]

    on = _load_ai_format_prompt(
        None,
        preset_id=None,
        user_id=None,
        include_images=True,
        text_nodes=text_nodes,
        available_categories=cats,
        web_fallback=True,
    )
    off = _load_ai_format_prompt(
        None,
        preset_id=None,
        user_id=None,
        include_images=True,
        text_nodes=text_nodes,
        available_categories=cats,
        web_fallback=False,
    )

    # 开：明确给出 game 字段用法（点名列表外游戏）
    assert '"game"' in on
    assert "用游戏名" in on
    # 关：完全不出现 game 指引，保持纯 category_id
    assert '"game"' not in off
    assert "用游戏名" not in off


def test_headings_only_prompt_ignores_web_fallback():
    """纯小标题识别（include_images=False）不配图，更不应出现联网兜底 game 指引。"""
    from server.app.modules.articles.ai_format import _load_ai_format_prompt

    text_nodes = [(0, {"type": "paragraph", "content": [{"type": "text", "text": "正文"}]})]
    prompt = _load_ai_format_prompt(
        None,
        preset_id=None,
        user_id=None,
        include_images=False,
        text_nodes=text_nodes,
        available_categories=[],
        web_fallback=True,
    )
    assert '"game"' not in prompt


def test_render_ai_format_prompt_strict_undefined_raises():
    from jinja2 import UndefinedError

    from server.app.modules.articles.ai_format import render_ai_format_prompt

    with pytest.raises(UndefinedError):
        render_ai_format_prompt(
            "{{ missing_value }}",
            text_nodes=[],
            available_categories=[],
        )


def test_aggressive_builtin_prompt_variant_and_numeric_overrides():
    """builtin_variant='aggressive' → 积极配图措辞；max_images/min_spacing 覆盖注入占位。

    保守变体不含"积极配图"、保留"图少文多"；两者都保留"不确定不插"准星——激进≠瞎插。
    """
    from server.app.modules.articles.ai_format import _load_ai_format_prompt

    text_nodes = [(0, {"type": "paragraph", "content": [{"type": "text", "text": "某游戏真好玩"}]})]
    cats = [{"id": 5, "name": "主推A", "description": None}]

    aggressive = _load_ai_format_prompt(
        None,
        preset_id=None,
        user_id=None,
        include_images=True,
        text_nodes=text_nodes,
        available_categories=cats,
        max_images=7,
        min_spacing=2,
        builtin_variant="aggressive",
    )
    conservative = _load_ai_format_prompt(
        None,
        preset_id=None,
        user_id=None,
        include_images=True,
        text_nodes=text_nodes,
        available_categories=cats,
        builtin_variant="conservative",
    )

    # 激进：积极配图措辞 + 数字旋钮覆盖生效
    assert "积极配图" in aggressive
    assert "最多 7 张" in aggressive
    assert "不少于 2 个节点" in aggressive
    assert "吃不准" in aggressive  # 准星仍在
    # 保守：旧措辞，无积极配图模式；默认派生上限 3
    assert "积极配图" not in conservative
    assert "图少文多" in conservative
    assert "不超过 3 张" in conservative


def test_maybe_insert_images_hard_caps_at_max_images(monkeypatch):
    """max_images 为硬上限：模型给 4 个位置、max_images=2 → 只插靠前 2 张并提前停止。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    pick_calls = []

    def fake_pick(query, db):
        pick_calls.append(1)
        return 42

    fake_ref = SimpleNamespace(
        id=42, url="/api/stock-images/42/file", filename="t.jpg", width=800, height=600
    )
    monkeypatch.setattr("server.app.modules.articles.ai_format.pick_image_id", fake_pick)
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id", lambda image_id, db: fake_ref
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.has_images_in_content", lambda content: False
    )

    inserted_positions = []

    def fake_insert(content_json, refs, positions):
        inserted_positions.extend(positions)
        return content_json

    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.insert_images_at_positions", fake_insert
    )

    cat = SimpleNamespace(id=1)
    article = _make_article_stub(stock_categories=[cat])
    parsed = {
        "image_positions": [
            {"index": 0, "category_id": 1},
            {"index": 1, "category_id": 1},
            {"index": 2, "category_id": 1},
            {"index": 3, "category_id": 1},
        ]
    }
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None, max_images=2)

    assert count == 2  # 硬截断到 2 张
    assert inserted_positions == [0, 1]  # 取靠前的两个
    assert len(pick_calls) == 2  # 达上限即停，不再为第 3/4 个位置取图


def test_maybe_insert_images_no_cap_when_max_images_none(monkeypatch):
    """max_images=None（手动排版/方案配图）→ 不硬截断，沿用原行为：模型给几个插几个。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    fake_ref = SimpleNamespace(
        id=42, url="/api/stock-images/42/file", filename="t.jpg", width=800, height=600
    )
    monkeypatch.setattr("server.app.modules.articles.ai_format.pick_image_id", lambda q, db: 42)
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id", lambda image_id, db: fake_ref
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.has_images_in_content", lambda content: False
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.insert_images_at_positions",
        lambda content_json, refs, positions: content_json,
    )

    cat = SimpleNamespace(id=1)
    article = _make_article_stub(stock_categories=[cat])
    parsed = {"image_positions": [{"index": i, "category_id": 1} for i in range(4)]}
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert count == 4  # 不截断


def test_maybe_insert_images_skips_when_no_stock_categories(monkeypatch):
    """stock_categories 为空且 stock_category_id 为 None → 不插图（早期返回）。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    pick_called = []
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.pick_image_id",
        lambda *a, **kw: pick_called.append(1) or None,
    )

    content = _simple_content()
    parsed = {"image_positions": [{"index": 0, "category_id": 1}]}
    article = _make_article_stub()
    result_json, count = _maybe_insert_images(content, parsed, article, db=None)

    assert count == 0
    assert pick_called == []  # 选图函数未被调用


def test_maybe_insert_images_skips_when_no_category_id(monkeypatch):
    """image_positions 没有 category_id 或 category_id 为 None → 不插图。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    pick_called = []
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.pick_image_id",
        lambda *a, **kw: pick_called.append(1) or None,
    )

    cat = SimpleNamespace(id=1)
    article = _make_article_stub(stock_categories=[cat])

    # 无 category_id 字段
    parsed = {"image_positions": [{"index": 0}]}
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)
    assert count == 0

    # category_id 明确为 None
    parsed2 = {"image_positions": [{"index": 0, "category_id": None}]}
    _, count2 = _maybe_insert_images(_simple_content(), parsed2, article, db=None)
    assert count2 == 0

    assert pick_called == []


def test_maybe_insert_images_skips_when_no_library_match(monkeypatch):
    """category_id 有效但 pick_image_id 返回 None → 不插图。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.pick_image_id",
        lambda query, db: None,
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id",
        lambda *a, **kw: None,
    )

    cat = SimpleNamespace(id=1)
    article = _make_article_stub(stock_categories=[cat])
    parsed = {"image_positions": [{"index": 0, "category_id": 1}]}
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert count == 0


def test_maybe_insert_images_inserts_when_category_id_matches(monkeypatch):
    """category_id 有效，pick_image_id 返回图片 ID → 插入图片，count=1。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    fake_ref = SimpleNamespace(
        id=42, url="/api/stock-images/42/file", filename="test.jpg", width=800, height=600
    )

    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.pick_image_id",
        lambda query, db: 42,
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id",
        lambda image_id, db: fake_ref,
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.has_images_in_content",
        lambda content: False,
    )

    inserted_positions = []

    def fake_insert(content_json, refs, positions):
        inserted_positions.extend(positions)
        return content_json

    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.insert_images_at_positions",
        fake_insert,
    )

    cat = SimpleNamespace(id=1)
    article = _make_article_stub(stock_categories=[cat])
    parsed = {"image_positions": [{"index": 0, "category_id": 1}]}
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert count == 1
    assert 0 in inserted_positions


def test_maybe_insert_images_uses_requested_category_id_from_position(monkeypatch):
    """pick_image_id 收到的 query.category_ids 就是 image_position 中的 category_id。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    received_queries = []

    def fake_pick(query, db):
        received_queries.append(list(query.category_ids))
        return None

    monkeypatch.setattr("server.app.modules.articles.ai_format.pick_image_id", fake_pick)
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id", lambda *a, **kw: None
    )

    cats = [SimpleNamespace(id=10), SimpleNamespace(id=20), SimpleNamespace(id=30)]
    article = _make_article_stub(stock_categories=cats)
    # 两个位置分别指定不同 category
    parsed = {
        "image_positions": [
            {"index": 0, "category_id": 10},
            {"index": 1, "category_id": 20},
        ]
    }
    _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert received_queries == [[10], [20]]


def test_maybe_insert_images_uses_requested_category_id(monkeypatch):
    """模型返回 category_id 时，仅在该栏目中随机取图（使用 pick_image_id）。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    received = []

    def fake_pick(query, db):
        received.append(list(query.category_ids))
        return 42

    fake_ref = SimpleNamespace(
        id=42, url="/api/stock-images/42/file", filename="test.jpg", width=800, height=600
    )
    monkeypatch.setattr("server.app.modules.articles.ai_format.pick_image_id", fake_pick)
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id", lambda image_id, db: fake_ref
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.has_images_in_content", lambda content: False
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.insert_images_at_positions",
        lambda content_json, refs, positions: content_json,
    )

    cats = [SimpleNamespace(id=10), SimpleNamespace(id=20)]
    article = _make_article_stub(stock_categories=cats)
    parsed = {"image_positions": [{"index": 0, "category_id": 20}]}
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert count == 1
    assert received == [[20]]


def test_maybe_insert_images_skips_unavailable_requested_category(monkeypatch):
    """模型返回未关联 category_id 时跳过，不调用 pick_image_id。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    pick_called = []
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.pick_image_id",
        lambda *args, **kwargs: pick_called.append(args) or None,
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id", lambda *a, **kw: None
    )

    article = _make_article_stub(stock_categories=[SimpleNamespace(id=10)])
    parsed = {"image_positions": [{"index": 0, "category_id": 99}]}
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert count == 0
    assert pick_called == []


def test_maybe_insert_images_old_format_integers_skipped(monkeypatch):
    """旧格式（纯整数数组）→ 无 category_id → 全部跳过，不插图。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    pick_called = []
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.pick_image_id",
        lambda *a, **kw: pick_called.append(1) or None,
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id", lambda *a, **kw: None
    )

    cat = SimpleNamespace(id=1)
    article = _make_article_stub(stock_categories=[cat])
    parsed = {"image_positions": [0, 1]}  # 旧格式：纯整数，无 category_id
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert count == 0
    assert pick_called == []


def test_maybe_insert_images_fallback_to_old_stock_category_id(monkeypatch):
    """stock_categories 为空但 stock_category_id 有值 → 兼容旧字段，category_id 匹配时插图。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    received_ids = []

    def fake_pick(query, db):
        received_ids.extend(query.category_ids)
        return 99

    fake_ref = SimpleNamespace(
        id=99, url="/api/stock-images/99/file", filename="test.jpg", width=800, height=600
    )
    monkeypatch.setattr("server.app.modules.articles.ai_format.pick_image_id", fake_pick)
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id", lambda image_id, db: fake_ref
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.has_images_in_content", lambda content: False
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.insert_images_at_positions",
        lambda content_json, refs, positions: content_json,
    )

    # stock_categories 为空，只有旧字段 stock_category_id=99
    article = _make_article_stub(stock_category_id=99, stock_categories=[])
    # image_position 指向旧字段的 category_id
    parsed = {"image_positions": [{"index": 0, "category_id": 99}]}
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)

    # 应当把旧的 stock_category_id 包含进 category_ids，并且 pick_image_id 被调用
    assert 99 in received_ids
    assert count == 1


@pytest.mark.mysql
def test_all_category_contexts_returns_all_buckets(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_format import all_category_contexts
        from server.app.modules.image_library.models import StockCategory

        with test_app.session_factory() as db:
            db.add(StockCategory(name="王者荣耀", bucket_name="wzry", description="MOBA"))
            db.add(StockCategory(name="原神", bucket_name="ys", description=None))
            db.commit()

        with test_app.session_factory() as db:
            cats = all_category_contexts(db)

        names = {c["name"] for c in cats}
        assert names == {"王者荣耀", "原神"}
        assert all(set(c.keys()) == {"id", "name", "description"} for c in cats)
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_run_ai_format_uses_candidate_categories_when_article_has_none(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        from server.app.modules.articles import ai_format as aif
        from server.app.modules.articles.ai_format import run_ai_format

        article = _create_article(
            client,
            {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "王者荣耀是一款 MOBA 手游。"}],
                    },
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "它有上百名英雄。"}],
                    },
                ],
            },
        )
        article_id = article["id"]

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion",
            lambda **kw: _fake_completion(
                '{"heading_indices": [], "image_positions": [{"index": 1, "category_id": 777}]}'
            ),
        )
        monkeypatch.setattr(aif, "pick_image_id", lambda query, db: 1001)
        monkeypatch.setattr(
            aif,
            "fetch_image_by_id",
            lambda image_id, db: SimpleNamespace(
                url="http://img/1001.png", alt="王者荣耀", width=800, height=600
            ),
        )
        inserted = {}
        monkeypatch.setattr(
            aif,
            "insert_images_at_positions",
            lambda content_json, refs, positions: (
                inserted.update({"refs": refs, "positions": positions}) or content_json
            ),
        )
        # run_ai_format 会重新读取配置（get_settings.cache_clear），并且在模拟模型调用前要求有 API key。
        # 这里显式设置，避免测试依赖开发机环境中的 GEO_AI_* 变量（CI 中没有）。
        monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "test-key")

        candidate = [{"id": 777, "name": "王者荣耀", "description": "MOBA"}]
        run_ai_format(article_id, include_images=True, candidate_categories=candidate)

        assert inserted.get("positions") == [1]
        assert len(inserted.get("refs", [])) == 1
    finally:
        test_app.cleanup()
