from server.app.modules.articles.ai_format import _derive_html_and_text


def _doc(*nodes):
    return {"type": "doc", "content": list(nodes)}


def test_bulletlist_preserved_in_html_and_text():
    doc = _doc(
        {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [{"type": "text", "text": "游戏一、《餐厅养成记》"}],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "平台", "marks": [{"type": "bold"}]},
                                {"type": "text", "text": "：全渠道"},
                            ],
                        }
                    ],
                },
            ],
        },
    )
    html, text = _derive_html_and_text(doc)
    assert "<ul>" in html and "<li>" in html
    assert "<strong>平台</strong>" in html
    assert "全渠道" in html
    assert "平台：全渠道" in text


def test_image_node_preserved_in_html():
    doc = _doc(
        {"type": "paragraph", "content": [{"type": "text", "text": "正文"}]},
        {"type": "image", "attrs": {"src": "/api/stock-images/816/file", "alt": "封面"}},
    )
    html, _ = _derive_html_and_text(doc)
    assert "<img" in html and "/api/stock-images/816/file" in html


def test_marks_and_hardbreak():
    doc = _doc(
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "斜", "marks": [{"type": "italic"}]},
                {"type": "hardBreak"},
                {
                    "type": "text",
                    "text": "链",
                    "marks": [{"type": "link", "attrs": {"href": "https://x.com"}}],
                },
            ],
        }
    )
    html, _ = _derive_html_and_text(doc)
    assert "<em>斜</em>" in html
    assert "<br>" in html
    assert '<a href="https://x.com">链</a>' in html


def test_multi_mark_on_one_run_nesting():
    from server.app.modules.articles.ai_format import _derive_html_and_text

    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "链",
                        "marks": [
                            {"type": "bold"},
                            {"type": "link", "attrs": {"href": "https://x.com"}},
                        ],
                    },
                ],
            }
        ],
    }
    html, _ = _derive_html_and_text(doc)
    # 当前实现：mark 按列表顺序逐层包裹，link 最后处理故在最外层
    assert html == '<p><a href="https://x.com"><strong>链</strong></a></p>'


def test_ordered_blockquote_codeblock():
    doc = _doc(
        {
            "type": "orderedList",
            "content": [
                {
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "一"}]}],
                },
            ],
        },
        {
            "type": "blockquote",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "引"}]}],
        },
        {"type": "codeBlock", "content": [{"type": "text", "text": "code()"}]},
    )
    html, text = _derive_html_and_text(doc)
    assert "<ol>" in html and "<blockquote>" in html and "<pre><code>" in html
    assert "一" in text and "引" in text and "code()" in text
