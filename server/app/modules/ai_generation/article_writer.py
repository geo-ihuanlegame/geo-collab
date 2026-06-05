"""方案流生文内核：与 session/scheme/LangGraph 解耦的可复用单元。

未来自动化 pipeline 可把"生成一篇文章"当作一个节点直接复用本函数——它只依赖
（模板内容 + 问题文本 + 用户 id + session 工厂），不感知方案/运行/会话。
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable
from typing import Any

_QUESTION_PLACEHOLDER = "{{问题}}"

_GENERIC_SYSTEM_PROMPT = (
    "你是一位专业的内容写作者。根据下方的写作要求，撰写一篇高质量的文章。"
    "使用 Markdown 格式输出正文，包含标题（# 一级标题）。"
)


def render_question_prompt(template_content: str, question_text: str) -> str:
    """把问题文本注入模板。

    - 含 {{问题}} 占位符：仅替换占位符（高级用法，问题插入位置由产品自定）。
    - 不含占位符：把编号问题块前置到模板正文之前，桥接句带问题条数。
    - 问题为空：只返回模板正文，不插入问题块。

    `question_text` 已由上游（format_question_group / _render_questions）渲染成
    "1. …\n2. …" 编号列表；这里只负责拼接，不再编号。
    """
    if _QUESTION_PLACEHOLDER in template_content:
        return template_content.replace(_QUESTION_PLACEHOLDER, question_text)
    if not question_text.strip():
        return template_content
    n = len(re.findall(r"(?m)^\s*\d+\.", question_text))
    return (
        f"基于以下 {n} 个问题，结合参考这些问题生成 1 篇文章：\n\n"
        f"{question_text}\n\n{template_content}"
    )


def _inject_api_key(api_key: str) -> None:
    if api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
        os.environ.setdefault("OPENAI_API_KEY", api_key)


def generate_article_from_prompt(
    *,
    session_factory: Callable[[], Any],
    user_id: int,
    template_content: str,
    question_text: str,
    model: str | None = None,
) -> int:
    """组 prompt → LLM → 取标题 → 转 Tiptap/HTML → create_article。返回 article_id。

    通用系统提示词（不拼 Skill）。`model` 为方案级 AI 引擎覆盖（None / 空 = 用 settings.ai_model）。
    异常向上抛（由调用方记 task 失败）。每次调用自带独立 session。
    """
    import litellm

    from server.app.core.config import get_settings
    from server.app.modules.ai_generation.converter import markdown_to_html, markdown_to_tiptap
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    settings = get_settings()
    _inject_api_key(settings.ai_api_key)

    user_prompt = (
        render_question_prompt(template_content, question_text)
        + "\n\n请开始写作（只输出 Markdown 正文，含 # 一级标题，不要解释）："
    )
    response = litellm.completion(
        model=(model or "").strip() or settings.ai_model,
        messages=[
            {"role": "system", "content": _GENERIC_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        api_key=settings.ai_api_key or None,
        timeout=300,
        max_tokens=12000,
    )
    md_content = response.choices[0].message.content or ""

    lines = md_content.strip().splitlines()
    title = "无题"
    body_lines = lines
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip() or "无题"
        body_lines = lines[1:]
    body_md = "\n".join(body_lines).strip()

    article_payload = ArticleCreate(
        title=title,
        content_json=markdown_to_tiptap(body_md),
        content_html=markdown_to_html(body_md),
        plain_text=body_md,
        word_count=len(body_md),
        client_request_id=str(uuid.uuid4()),
    )

    db = session_factory()
    try:
        article = create_article(db, user_id, article_payload)
        # AI 生文一律未审：不依赖 run 后 mark_pending_and_group 翻转
        article.review_status = "pending"
        db.commit()
        return article.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
