from server.app.modules.articles.ai_format import (
    _normalize_game_name,
    build_image_positions_from_game_list,
)


def _doc(*headings):
    content = []
    for h in headings:
        content.append(
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": h}]}
        )
        content.append({"type": "bulletList", "content": []})  # 制造 index 空洞
    return {"type": "doc", "content": content}


def test_normalize_strips_brackets_and_prefix():
    assert _normalize_game_name("游戏一、《餐厅养成记》") == "餐厅养成记"
    assert _normalize_game_name("《原神》") == "原神"
    assert _normalize_game_name("游戏10、明日方舟") == "明日方舟"


def test_normalize_strips_curly_quotes():
    assert _normalize_game_name("“原神”") == "原神"
    assert _normalize_game_name("游戏二、“明日方舟”") == "明日方舟"


def test_match_by_name_computes_absolute_index_with_gaps():
    doc = _doc("游戏一、《餐厅养成记》", "游戏二、《原神》")  # heading 在 index 0、2
    positions, unmatched = build_image_positions_from_game_list(
        doc, [{"game": "餐厅养成记"}, {"game": "原神"}]
    )
    assert {p["index"] for p in positions} == {0, 2}
    assert unmatched == []


def test_unmatched_game_reported():
    doc = _doc("游戏一、《餐厅养成记》")
    positions, unmatched = build_image_positions_from_game_list(doc, [{"game": "不存在的游戏"}])
    assert positions == []
    assert unmatched == [{"game": "不存在的游戏", "reason": "heading_not_found"}]


def test_index_hint_fallback_when_not_found():
    doc = _doc("游戏一、《餐厅养成记》")
    positions, unmatched = build_image_positions_from_game_list(
        doc, [{"game": "外部游戏", "index": 0}]
    )
    assert positions == [{"index": 0, "game": "外部游戏"}]
    assert unmatched == []


def test_index_conflict_dedup_keeps_first():
    # 同一 heading 文本含两个游戏名 → 解析到同一 index → 去重
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "原神 与 崩坏"}],
            },
        ],
    }
    positions, unmatched = build_image_positions_from_game_list(
        doc, [{"game": "原神"}, {"game": "崩坏"}]
    )
    assert positions == [{"index": 0, "game": "原神"}]
    assert unmatched == [{"game": "崩坏", "reason": "index_conflict"}]


def test_category_id_passthrough():
    doc = _doc("游戏一、《餐厅养成记》")
    positions, _ = build_image_positions_from_game_list(
        doc, [{"game": "餐厅养成记", "category_id": 12}]
    )
    assert positions == [{"index": 0, "game": "餐厅养成记", "category_id": 12}]


def test_game_list_path_passes_require_llm_false_to_prepare(monkeypatch):
    """run_ai_format_from_game_list 确实以 require_llm=False 调用 _ai_format_prepare。

    此 kwarg 是 Fix 1 的核心：确定性路径不应因 LLM key 缺失而 abort。
    通过 spy 捕获 _ai_format_prepare 实际收到的 kwargs 来验证。
    """
    import server.app.modules.articles.ai_format as m

    class _Prep:
        content_json = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": "原神"}],
                },
            ],
        }
        available_categories = []
        image_search_query = None

    captured_kwargs: dict = {}

    def _spy_prepare(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _Prep()

    monkeypatch.setattr(m, "_ai_format_prepare", _spy_prepare)
    monkeypatch.setattr(
        m,
        "_web_fallback_collect_and_write_back",
        lambda article_id, *, out_diagnostics=None, **kw: 0,
    )

    m.run_ai_format_from_game_list(
        1,
        lock_started_at=None,
        game_list=[{"game": "原神"}],
        preset_id=None,
        user_id=1,
        candidate_categories=None,
        max_images=3,
        min_spacing=5,
        builtin_variant="aggressive",
    )

    assert captured_kwargs.get("require_llm") is False, (
        f"expected require_llm=False in _ai_format_prepare call, got {captured_kwargs}"
    )


def test_run_from_game_list_counts_unmatched_as_missed(monkeypatch):
    import server.app.modules.articles.ai_format as m

    class _Prep:
        content_json = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": "游戏一、《餐厅养成记》"}],
                },
            ],
        }
        available_categories = []
        image_search_query = None

    monkeypatch.setattr(m, "_ai_format_prepare", lambda *a, **k: _Prep())

    captured = {}

    def _fake_collect(article_id, *, parsed, out_diagnostics=None, **kw):
        captured["parsed"] = parsed
        captured["heading_indices"] = kw.get("heading_indices")
        if out_diagnostics is not None:
            out_diagnostics["inserted"] = 1
            out_diagnostics["missed_games"] = []
        return 1

    monkeypatch.setattr(m, "_web_fallback_collect_and_write_back", _fake_collect)

    diag: dict = {}
    inserted = m.run_ai_format_from_game_list(
        1,
        lock_started_at=None,
        game_list=[{"game": "餐厅养成记"}, {"game": "查无此游戏"}],
        preset_id=None,
        user_id=1,
        candidate_categories=None,
        max_images=12,
        min_spacing=1,
        builtin_variant="aggressive",
        out_diagnostics=diag,
    )
    assert inserted == 1
    # 合成 parsed 只含命中的那一个
    assert captured["parsed"]["image_positions"] == [{"index": 0, "game": "餐厅养成记"}]
    # 不提升标题
    assert captured["heading_indices"] == set()
    # 计数：expected=2、inserted=1、missed=1、missed_games 含未命中的游戏
    assert diag["requested"] == 2
    assert diag["inserted"] == 1
    assert diag["missed"] == 1
    assert "查无此游戏" in diag["missed_games"]
