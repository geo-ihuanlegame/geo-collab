from server.app.modules.articles.ai_format import (
    _normalize_game_name,
    build_image_positions_from_game_list,
)


def _doc(*headings):
    content = []
    for h in headings:
        content.append({"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": h}]})
        content.append({"type": "bulletList", "content": []})  # 制造 index 空洞
    return {"type": "doc", "content": content}


def test_normalize_strips_brackets_and_prefix():
    assert _normalize_game_name("游戏一、《餐厅养成记》") == "餐厅养成记"
    assert _normalize_game_name("《原神》") == "原神"
    assert _normalize_game_name("游戏10、明日方舟") == "明日方舟"


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
    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "原神 与 崩坏"}]},
    ]}
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
