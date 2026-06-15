"""article_writer.extract_title_and_body 纯函数测试（无 DB）。

覆盖标题提取的加固分支与兜底：原逻辑只认第一行精确 "# "，模型一旦吐出
##/无空格/加粗/代码围栏/前言，标题就退化成「无题」。这里钉死每条容错路径。
"""

from server.app.modules.ai_generation.article_writer import extract_title_and_body


def test_standard_h1_heading():
    title, body = extract_title_and_body("# 我的标题\n正文内容")
    assert title == "我的标题"
    assert body == "正文内容"


def test_h2_heading_used_as_title():
    title, body = extract_title_and_body("## 二级当标题\n正文")
    assert title == "二级当标题"
    assert body == "正文"


def test_h3_heading_used_as_title():
    title, _ = extract_title_and_body("### 三级标题\n正文")
    assert title == "三级标题"


def test_hash_without_space():
    title, body = extract_title_and_body("#无空格标题\n正文")
    assert title == "无空格标题"
    assert body == "正文"


def test_bold_line_as_title():
    title, body = extract_title_and_body("**加粗标题**\n正文")
    assert title == "加粗标题"
    assert body == "正文"


def test_strips_code_fence_wrapper_with_lang():
    md = "```markdown\n# 围栏标题\n正文第一段\n\n正文第二段\n```"
    title, body = extract_title_and_body(md)
    assert title == "围栏标题"
    assert body == "正文第一段\n\n正文第二段"
    assert "```" not in body


def test_strips_plain_code_fence():
    md = "```\n# 围栏标题\n正文\n```"
    title, body = extract_title_and_body(md)
    assert title == "围栏标题"
    assert body == "正文"


def test_skips_leading_blank_lines():
    title, _ = extract_title_and_body("\n\n# 标题\n正文")
    assert title == "标题"


def test_fallback_first_nonblank_line_keeps_body():
    """无任何 heading：用首行非空文本当标题，且正文完整保留首行（不丢内容）。"""
    md = "这是一段没有标题的开头。\n第二段内容。"
    title, body = extract_title_and_body(md)
    assert title == "这是一段没有标题的开头。"
    assert body == md  # 首行仍在正文里，内容不丢


def test_fallback_truncates_long_first_line():
    long_line = "一" * 80
    title, _ = extract_title_and_body(long_line)
    assert title.endswith("…")
    assert len(title) <= 60


def test_empty_content_returns_placeholder():
    assert extract_title_and_body("") == ("无题", "")
    assert extract_title_and_body("   \n  ") == ("无题", "")


def test_empty_heading_falls_through_to_next_line():
    """模型吐出空标题标记 '# '：跳过它，用下一行非空文本兜底，而不是落「无题」。"""
    title, _ = extract_title_and_body("# \n真正的第一行内容\n更多")
    assert title == "真正的第一行内容"


def test_heading_title_capped_at_300():
    title, _ = extract_title_and_body("# " + "标" * 400)
    assert len(title) <= 300
