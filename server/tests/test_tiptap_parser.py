from __future__ import annotations
import types
from server.app.modules.articles.tiptap_Parser import parse_body_segments, BodySegment


def _article(content_json="", plain_text="", html="", body_assets=None):
    return types.SimpleNamespace(
        content_json=content_json,
        plain_text=plain_text,
        content_html=html,
        body_assets=body_assets or [],
    )


def test_text_paragraph():
    content = '{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"Hello"}]}]}'
    segs = parse_body_segments(_article(content_json=content))
    texts = [s.text for s in segs if s.kind == "text"]
    assert any("Hello" in t for t in texts)


def test_image_segment_has_asset_id():
    content = '{"type":"doc","content":[{"type":"image","attrs":{"assetId":"abc123"}}]}'
    segs = parse_body_segments(_article(content_json=content))
    img_segs = [s for s in segs if s.kind == "image"]
    assert len(img_segs) == 1
    assert img_segs[0].image_asset_id == "abc123"
    assert img_segs[0].image_path is None


def test_body_segments_preserve_text_image_order_and_duplicate_images():
    content = (
        '{"type":"doc","content":['
        '{"type":"paragraph","content":[{"type":"text","text":"before"}]},'
        '{"type":"image","attrs":{"assetId":"repeat-asset"}},'
        '{"type":"paragraph","content":[{"type":"text","text":"after"}]},'
        '{"type":"image","attrs":{"assetId":"repeat-asset"}}'
        ']}'
    )

    segs = parse_body_segments(_article(content_json=content))
    meaningful = [s for s in segs if not (s.kind == "text" and not s.text.strip())]

    assert [(s.kind, s.text, s.image_asset_id) for s in meaningful] == [
        ("text", "before", None),
        ("image", "", "repeat-asset"),
        ("text", "after", None),
        ("image", "", "repeat-asset"),
    ]
    assert [s.image_asset_id for s in meaningful if s.kind == "image"] == [
        "repeat-asset",
        "repeat-asset",
    ]


def test_fallback_to_plain_text():
    segs = parse_body_segments(_article(plain_text="fallback"))
    assert len(segs) == 1
    assert segs[0].kind == "text"
    assert segs[0].text == "fallback"


def test_empty_article_returns_empty():
    segs = parse_body_segments(_article())
    assert segs == []


def test_hard_break_produces_newline():
    content = '{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"A"},{"type":"hardBreak"},{"type":"text","text":"B"}]}]}'
    segs = parse_body_segments(_article(content_json=content))
    full = "".join(s.text for s in segs if s.kind == "text")
    assert "A" in full and "B" in full and "\n" in full


def test_heading_segment_has_heading_level():
    import json
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 1},
                "content": [{"type": "text", "text": "My Title"}],
            }
        ],
    }
    segs = parse_body_segments(_article(content_json=json.dumps(doc)))
    text_segs = [s for s in segs if s.kind == "text" and s.text != "\n"]
    assert text_segs[0].heading_level == 1


def test_bold_mark_sets_bold_true():
    import json
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "bold", "marks": [{"type": "bold"}]},
                ],
            }
        ],
    }
    segs = parse_body_segments(_article(content_json=json.dumps(doc)))
    text_segs = [s for s in segs if s.kind == "text" and s.text != "\n"]
    assert text_segs[0].bold is True
