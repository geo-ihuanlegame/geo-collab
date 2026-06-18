"""头条 creator-ID 纯解析层单测（无 DB、无浏览器，不标 mysql）。"""

import json

from server.app.modules.tasks.drivers.toutiao_creator_id import (
    CreatorIdResult,
    extract_media_id_from_json,
    extract_media_id_from_text,
    is_numeric_id,
    normalize_dom_scan_result,
    parse_creator_info_response,
)

# ── media_id from JSON ────────────────────────────────────────────────────────


def test_media_id_from_flat_json():
    found = extract_media_id_from_json({"media_id": "1234567890"})
    assert found is not None
    assert found.value == "1234567890"
    assert "media_id" in found.source


def test_media_id_from_json_integer_value():
    found = extract_media_id_from_json({"media_id": 1234567890})
    assert found is not None
    assert found.value == "1234567890"


def test_media_id_from_nested_json():
    payload = {"data": {"user": {"media_id": "987654321012"}}}
    found = extract_media_id_from_json(payload)
    assert found is not None
    assert found.value == "987654321012"


def test_media_id_from_json_inside_list():
    payload = {"list": [{"foo": 1}, {"media_id": "11122233344"}]}
    found = extract_media_id_from_json(payload)
    assert found is not None
    assert found.value == "11122233344"


def test_media_id_from_json_skips_invalid_then_finds_valid():
    # 第一个 media_id 非法（含前导 0）→ 跳过、继续找
    payload = {"a": {"media_id": "0123"}, "b": {"media_id": "55566677788"}}
    found = extract_media_id_from_json(payload)
    assert found is not None
    assert found.value == "55566677788"


def test_media_id_absent_returns_none():
    assert extract_media_id_from_json({"foo": "bar", "n": 3}) is None


def test_parse_creator_info_response_valid_json():
    text = json.dumps({"data": {"media_id": "1234567890"}})
    found = parse_creator_info_response(text, "fetch:200")
    assert found is not None
    assert found.value == "1234567890"
    assert found.source == "fetch:200"


# ── text fallback ─────────────────────────────────────────────────────────────


def test_text_fallback_media_id_pair():
    text = 'garbage "media_id": "1234567890" trailing'
    found = extract_media_id_from_text(text, "fetch:body")
    assert found is not None
    assert found.value == "1234567890"


def test_text_fallback_media_id_pair_unquoted_value():
    text = '{"media_id":9988776655}'
    found = extract_media_id_from_text(text, "src")
    assert found is not None
    assert found.value == "9988776655"


def test_text_fallback_label():
    text = "账号信息 头条号ID 1234567890 其它"
    found = extract_media_id_from_text(text, "src")
    assert found is not None
    assert found.value == "1234567890"


def test_text_fallback_none_when_no_match():
    assert extract_media_id_from_text("nothing useful here", "src") is None


def test_parse_creator_info_response_non_json_falls_back_to_text():
    # 非 JSON、但文本里含 media_id 对
    text = 'html<body> "media_id": "1234567890" </body>'
    found = parse_creator_info_response(text, "fetch:body")
    assert found is not None
    assert found.value == "1234567890"


def test_parse_creator_info_response_json_without_media_id_falls_back_to_label():
    # 合法 JSON 但无 media_id 字段；文本兜底命中「头条号ID」label
    text = '{"ok": true, "desc": "头条号ID 1234567890"}'
    found = parse_creator_info_response(text, "fetch:200")
    assert found is not None
    assert found.value == "1234567890"


# ── DOM label scan normalization ──────────────────────────────────────────────


def test_normalize_dom_scan_result_valid():
    found = normalize_dom_scan_result(
        {"value": "1234567890", "evidence": "头条号ID 1234567890"}, "dom:url"
    )
    assert found is not None
    assert found.value == "1234567890"
    assert found.source == "dom:url"
    assert "头条号ID" in found.evidence


def test_normalize_dom_scan_result_invalid_value_returns_none():
    assert normalize_dom_scan_result({"value": "abc", "evidence": "x"}, "dom:url") is None


def test_normalize_dom_scan_result_none_input():
    assert normalize_dom_scan_result(None, "dom:url") is None


# ── numeric validation accept / reject ────────────────────────────────────────


def test_numeric_validation_accepts_min_and_max_length():
    assert is_numeric_id("12345678")  # 8 位（最短）
    assert is_numeric_id("1" + "0" * 29)  # 30 位（最长）
    assert is_numeric_id("1234567890123")
    assert is_numeric_id(1234567890)  # int 也接受


def test_numeric_validation_rejects():
    assert not is_numeric_id("1234567")  # 7 位（太短）
    assert not is_numeric_id("1" + "0" * 30)  # 31 位（太长）
    assert not is_numeric_id("0123456789")  # 前导 0
    assert not is_numeric_id("12345abc")  # 含字母
    assert not is_numeric_id("")  # 空串
    assert not is_numeric_id(None)  # None
    assert not is_numeric_id("  123  ")  # 太短（去空白后 3 位）


def test_numeric_validation_strips_whitespace_for_valid():
    assert is_numeric_id("  1234567890  ")


def test_result_is_frozen_dataclass():
    r = CreatorIdResult("1234567890", "src", "ev")
    assert r.value == "1234567890"
    assert r.source == "src"
    assert r.evidence == "ev"
