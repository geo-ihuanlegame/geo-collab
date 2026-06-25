"""tiptap_to_wechat_html 单测：纯函数，无 DB / 无网络。

覆盖：标题层级、行内混排不拆段、列表嵌套、引用 / 代码块、按 key 换图、
缺 url 跳过、未知节点降级、转义、空文档。
"""

from server.app.modules.tasks.drivers.wechat_html import tiptap_to_wechat_html


def _doc(*blocks):
    return {"type": "doc", "content": list(blocks)}


def _p(*inline):
    return {"type": "paragraph", "content": list(inline)}


def _text(t, *marks):
    node = {"type": "text", "text": t}
    if marks:
        node["marks"] = [{"type": m} for m in marks]
    return node


def test_headings_keep_all_levels():
    doc = _doc(
        {"type": "heading", "attrs": {"level": 1}, "content": [_text("一级")]},
        {"type": "heading", "attrs": {"level": 3}, "content": [_text("三级")]},
        {"type": "heading", "attrs": {"level": 6}, "content": [_text("六级")]},
    )
    html = tiptap_to_wechat_html(doc)
    assert "<h1>一级</h1>" in html
    assert "<h3>三级</h3>" in html
    assert "<h6>六级</h6>" in html


def test_inline_mixed_marks_stay_in_one_paragraph():
    # "普通 + 加粗 + 普通" 必须在同一个 <p> 内，不再被拆成多段
    doc = _doc(_p(_text("前"), _text("粗", "bold"), _text("后")))
    html = tiptap_to_wechat_html(doc)
    assert html == "<p>前<strong>粗</strong>后</p>"


def test_bold_italic_nesting():
    doc = _doc(_p(_text("x", "bold", "italic")))
    html = tiptap_to_wechat_html(doc)
    assert html == "<p><strong><em>x</em></strong></p>"


def test_inline_code_and_link():
    link_node = {
        "type": "text",
        "text": "点我",
        "marks": [{"type": "link", "attrs": {"href": "https://e.com/a?b=1"}}],
    }
    doc = _doc(_p(_text("代码", "code")), _p(link_node))
    html = tiptap_to_wechat_html(doc)
    assert "<code>代码</code>" in html
    assert '<a href="https://e.com/a?b=1">点我</a>' in html


def test_bullet_list_and_nested():
    doc = _doc(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [_text("一")]},
                        {
                            "type": "bulletList",
                            "content": [
                                {
                                    "type": "listItem",
                                    "content": [{"type": "paragraph", "content": [_text("一一")]}],
                                }
                            ],
                        },
                    ],
                },
                {"type": "listItem", "content": [{"type": "paragraph", "content": [_text("二")]}]},
            ],
        }
    )
    html = tiptap_to_wechat_html(doc)
    assert html == "<ul><li>一<ul><li>一一</li></ul></li><li>二</li></ul>"


def test_ordered_list():
    doc = _doc(
        {
            "type": "orderedList",
            "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [_text("甲")]}]},
            ],
        }
    )
    assert tiptap_to_wechat_html(doc) == "<ol><li>甲</li></ol>"


def test_blockquote_and_codeblock():
    doc = _doc(
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [_text("引用")]}]},
        {"type": "codeBlock", "content": [_text("a < b")]},
    )
    html = tiptap_to_wechat_html(doc)
    assert "<blockquote><p>引用</p></blockquote>" in html
    assert "<pre><code>a &lt; b</code></pre>" in html


def test_image_url_swapped_by_node_key():
    doc = _doc({"type": "image", "attrs": {"assetId": "a1"}})
    html = tiptap_to_wechat_html(doc, {"a1": "https://mmbiz.qpic.cn/1.jpg"})
    assert '<img src="https://mmbiz.qpic.cn/1.jpg" style="max-width:100%;">' in html


def test_image_without_url_is_skipped():
    doc = _doc({"type": "image", "attrs": {"assetId": "a1"}}, _p(_text("正文")))
    html = tiptap_to_wechat_html(doc, {})
    assert "<img" not in html
    assert "<p>正文</p>" in html


def test_unknown_block_degrades_to_paragraph():
    doc = _doc({"type": "weirdBlock", "content": [_text("怪块")]})
    assert tiptap_to_wechat_html(doc) == "<p>怪块</p>"


def test_text_escaped():
    doc = _doc(_p(_text('a<b>&"c')))
    html = tiptap_to_wechat_html(doc)
    assert "a&lt;b&gt;&amp;" in html
    assert "&quot;" in html  # double-quote must be escaped for safe href embedding


def test_empty_paragraph_becomes_br():
    doc = _doc(_p())
    assert tiptap_to_wechat_html(doc) == "<p><br></p>"


def test_list_item_non_paragraph_block_child():
    # listItem whose child is a codeBlock (not paragraph/list) — exercises _list_html else-branch
    doc = _doc(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "codeBlock", "content": [_text("x = 1")]},
                    ],
                }
            ],
        }
    )
    html = tiptap_to_wechat_html(doc)
    assert "<li>" in html
    assert "<pre><code>x = 1</code></pre>" in html


def test_empty_doc_returns_empty_string():
    assert tiptap_to_wechat_html({"type": "doc", "content": []}) == ""
    assert tiptap_to_wechat_html({}) == ""
    assert tiptap_to_wechat_html([]) == ""
