"""TapTap content_json → contents 转换器单测（纯函数，无需 DB / 网络）。

断言对象 = spike 真实抓包结构（captures/rich_contents.txt）：段落+加粗、一级/二级
标题、有序/无序列表、链接（前后垫空 text）、图片（按 key 取 url，缺则跳过）。
"""

from __future__ import annotations

from server.app.modules.tasks.drivers.taptap_contents import tiptap_to_contents


def _doc(*blocks: dict) -> dict:
    return {"type": "doc", "content": list(blocks)}


def _p(*inline: dict) -> dict:
    return {"type": "paragraph", "content": list(inline)}


def _text(s: str, *marks: dict) -> dict:
    node = {"type": "text", "text": s}
    if marks:
        node["marks"] = list(marks)
    return node


def test_paragraph_bold_then_normal():
    # 真实快照：加粗叶子 + 普通叶子在同一段
    doc = _doc(_p(_text("这是正文带加粗的效果", {"type": "bold"}), _text("这是后接的普通文本")))
    assert tiptap_to_contents(doc, {}) == [
        {
            "type": "paragraph",
            "children": [
                {"text": "这是正文带加粗的效果", "bold": True},
                {"text": "这是后接的普通文本"},
            ],
        }
    ]


def test_heading_levels_and_cap_at_2():
    doc = _doc(
        {
            "type": "heading",
            "attrs": {"level": 1},
            "content": [_text("一级标题", {"type": "bold"})],
        },
        {"type": "heading", "attrs": {"level": 2}, "content": [_text("二级标题")]},
        {"type": "heading", "attrs": {"level": 3}, "content": [_text("三级降级到二级")]},
    )
    assert tiptap_to_contents(doc, {}) == [
        {"type": "heading", "children": [{"text": "一级标题", "bold": True}], "info": {"level": 1}},
        {"type": "heading", "children": [{"text": "二级标题"}], "info": {"level": 2}},
        {"type": "heading", "children": [{"text": "三级降级到二级"}], "info": {"level": 2}},
    ]


def test_ordered_and_bullet_list():
    doc = _doc(
        {
            "type": "orderedList",
            "content": [
                {"type": "listItem", "content": [_p(_text("人之初"))]},
                {"type": "listItem", "content": [_p(_text("性本善"))]},
            ],
        },
        {
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": [_p(_text("你好"))]},
            ],
        },
    )
    assert tiptap_to_contents(doc, {}) == [
        {
            "type": "list",
            "info": {"style": "numbered"},
            "children": [
                {"type": "list-item", "children": [{"text": "人之初"}], "info": {"li-level": 1}},
                {"type": "list-item", "children": [{"text": "性本善"}], "info": {"li-level": 1}},
            ],
        },
        {
            "type": "list",
            "info": {"style": "default"},
            "children": [
                {"type": "list-item", "children": [{"text": "你好"}], "info": {"li-level": 1}},
            ],
        },
    ]


def test_nested_list_li_level():
    doc = _doc(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        _p(_text("外层")),
                        {
                            "type": "bulletList",
                            "content": [
                                {"type": "listItem", "content": [_p(_text("内层"))]},
                            ],
                        },
                    ],
                },
            ],
        },
    )
    out = tiptap_to_contents(doc, {})
    assert out[0]["children"] == [
        {"type": "list-item", "children": [{"text": "外层"}], "info": {"li-level": 1}},
        {"type": "list-item", "children": [{"text": "内层"}], "info": {"li-level": 2}},
    ]


def test_link_padded_with_empty_text():
    # 真实快照：含 link 的段落前后各垫一个空 {"text":""}
    doc = _doc(
        _p(_text("餐厅养成记", {"type": "link", "attrs": {"href": "https://ctycj.3zonegame.com/"}}))
    )
    assert tiptap_to_contents(doc, {}) == [
        {
            "type": "paragraph",
            "children": [
                {"text": ""},
                {
                    "type": "link",
                    "children": [{"text": "餐厅养成记"}],
                    "info": {"url": "https://ctycj.3zonegame.com/"},
                },
                {"text": ""},
            ],
        }
    ]


def test_link_between_text_no_double_padding():
    doc = _doc(
        _p(
            _text("前 "),
            _text("链接", {"type": "link", "attrs": {"href": "https://a.com"}}),
            _text(" 后"),
        )
    )
    assert tiptap_to_contents(doc, {}) == [
        {
            "type": "paragraph",
            "children": [
                {"text": "前 "},
                {"type": "link", "children": [{"text": "链接"}], "info": {"url": "https://a.com"}},
                {"text": " 后"},
            ],
        }
    ]


def test_image_by_key_and_skip_when_missing():
    doc = _doc(
        {"type": "image", "attrs": {"assetId": "abc123"}},
        {"type": "image", "attrs": {"assetId": "deleted999"}},
        {"type": "image", "attrs": {"src": "/api/stock-images/5/file"}},
    )
    urls = {
        "abc123": "https://img2-tc.tapimg.com/moment/a.jpg",
        "stock:5": "https://img2-tc.tapimg.com/moment/b.jpg",
    }
    assert tiptap_to_contents(doc, urls) == [
        {
            "type": "image",
            "info": {"img_url": "https://img2-tc.tapimg.com/moment/a.jpg", "description": ""},
        },
        # deleted999 无 url → 跳过
        {
            "type": "image",
            "info": {"img_url": "https://img2-tc.tapimg.com/moment/b.jpg", "description": ""},
        },
    ]


def test_unknown_mark_degrades_to_plain_text():
    doc = _doc(_p(_text("斜体字", {"type": "italic"})))
    assert tiptap_to_contents(doc, {}) == [{"type": "paragraph", "children": [{"text": "斜体字"}]}]


def test_bold_plus_link_keeps_bold_inside_link():
    doc = _doc(
        _p(_text("粗链", {"type": "bold"}, {"type": "link", "attrs": {"href": "https://a.com"}}))
    )
    assert tiptap_to_contents(doc, {}) == [
        {
            "type": "paragraph",
            "children": [
                {"text": ""},
                {
                    "type": "link",
                    "children": [{"text": "粗链", "bold": True}],
                    "info": {"url": "https://a.com"},
                },
                {"text": ""},
            ],
        }
    ]


def test_blockquote_degrades_to_paragraphs():
    doc = _doc({"type": "blockquote", "content": [_p(_text("引用内容"))]})
    assert tiptap_to_contents(doc, {}) == [
        {"type": "paragraph", "children": [{"text": "引用内容"}]}
    ]


def test_empty_paragraph_preserved_for_spacing():
    doc = _doc(_p(), _p(_text("正文")))
    assert tiptap_to_contents(doc, {}) == [
        {"type": "paragraph", "children": [{"text": ""}]},
        {"type": "paragraph", "children": [{"text": "正文"}]},
    ]


def test_accepts_bare_content_list_and_empty_doc():
    # 容错：直接传 content 列表
    assert tiptap_to_contents([_p(_text("x"))], {}) == [
        {"type": "paragraph", "children": [{"text": "x"}]}
    ]
    # 空文档 → 空 contents
    assert tiptap_to_contents({}, {}) == []
    assert tiptap_to_contents({"type": "doc", "content": []}, {}) == []
