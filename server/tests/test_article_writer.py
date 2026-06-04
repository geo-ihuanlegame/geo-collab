"""article_writer.render_question_prompt 纯函数测试（无 DB）。

覆盖问题词注入的三种分支：
- 模板不含 {{问题}}：编号问题块前置到正文前，桥接句带问题条数。
- 模板含 {{问题}}：仅替换占位符，不额外前置。
- 问题为空：只用模板正文，不插入空问题块。
"""

from server.app.modules.ai_generation.article_writer import render_question_prompt


def test_prepends_numbered_questions_before_template_with_count():
    template = "【产品提示词正文】围绕用户问题写作。"
    questions = "1. 怎样选游戏\n2. 免费良心"

    result = render_question_prompt(template, questions)

    assert result.startswith("基于以下 2 个问题，结合参考这些问题生成 1 篇文章：")
    # 真实问题排在产品提示词正文之前
    assert result.index("1. 怎样选游戏") < result.index(template)
    assert result.endswith(template)


def test_question_count_tracks_number_of_questions():
    result = render_question_prompt("正文", "1. a\n2. b\n3. c")
    assert result.startswith("基于以下 3 个问题，")


def test_keeps_placeholder_replacement_when_present():
    template = "写一篇：{{问题}}，谢谢。"

    result = render_question_prompt(template, "1. a\n2. b")

    assert result == "写一篇：1. a\n2. b，谢谢。"
    assert "基于以下" not in result


def test_empty_questions_returns_template_only():
    template = "纯正文，无问题。"

    assert render_question_prompt(template, "") == template
    assert render_question_prompt(template, "   \n  ") == template
