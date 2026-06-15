"""方案流生文内核：与会话、方案、LangGraph 解耦的可复用单元。

未来自动化工作流可把"生成一篇文章"当作一个节点直接复用本函数——它只依赖
（模板内容 + 问题文本 + 用户 ID + 会话工厂），不感知方案、运行或会话。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

_QUESTION_PLACEHOLDER = "{{问题}}"

_GENERIC_SYSTEM_PROMPT = (
    "你是一位专业的内容写作者。根据下方的写作要求，撰写一篇高质量的文章。"
    "使用 Markdown 格式输出正文。"
    "输出的第一行必须是文章标题，格式为 `# 标题`（井号后留一个空格、再写标题），"
    "标题之前不要有任何前言、说明或代码块标记。"
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


_HEADING_RE = re.compile(r"^#{1,6}\s*(.*)$")
_BOLD_TITLE_RE = re.compile(r"^\*\*(.+?)\*\*$")
_FALLBACK_TITLE_MAX = 60
_TITLE_MAX = 300  # 与 Article.title String(300) / ArticleCreate max_length 对齐


def _truncate_title(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _first_nonblank_index(lines: list[str], start: int = 0) -> int:
    i = start
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i


def _strip_code_fence(lines: list[str]) -> list[str]:
    """模型常把整篇输出包进 ``` 代码围栏。剥掉开头围栏行及其末尾配对围栏行。"""
    start = _first_nonblank_index(lines)
    if start >= len(lines) or not lines[start].lstrip().startswith("```"):
        return lines
    trimmed = lines[:start] + lines[start + 1 :]
    end = len(trimmed) - 1
    while end >= 0 and not trimmed[end].strip():
        end -= 1
    if end >= 0 and trimmed[end].strip() == "```":
        trimmed = trimmed[:end] + trimmed[end + 1 :]
    return trimmed


def extract_title_and_body(md_content: str) -> tuple[str, str]:
    """从模型 Markdown 输出里稳健抽取 (标题, 正文)。

    原逻辑只认第一行精确 "# "，模型一旦吐 ##/无空格/加粗/代码围栏/空标题，标题就退化
    成「无题」。这里按容错优先级抽取：
    1. 剥掉整篇外层 ``` 代码围栏；
    2. 首个非空行是任意级 heading（`#`~`######`，允许井号后无空格）→ 取其文本为标题、
       从正文剔除该行；空标题标记（如 "# "）则跳过、继续往下找；
    3. 首个非空行是整行加粗 `**标题**` → 取内层为标题、剔除该行；
    4. 兜底：用首个非空行文本当标题（截断），但**正文完整保留该行**，绝不丢内容；
    5. 通篇为空 → 才回落「无题」。

    标题非空且 ≤300（schema 上限）；兜底标题再额外截到 60 以免整段长句当标题。
    """
    text = (md_content or "").strip()
    if not text:
        return "无题", ""

    lines = _strip_code_fence(text.splitlines())
    i = _first_nonblank_index(lines)
    if i >= len(lines):
        return "无题", ""

    first = lines[i].strip()
    heading = _HEADING_RE.match(first)
    if heading is not None:
        inner = heading.group(1).strip()
        if inner:
            body = "\n".join(lines[i + 1 :]).strip()
            return _truncate_title(inner, _TITLE_MAX), body
        # 空标题标记（"# " / "#"）：丢弃该行，从下一非空行重新判定
        lines = lines[i + 1 :]
        i = _first_nonblank_index(lines)
        if i >= len(lines):
            return "无题", ""
        first = lines[i].strip()

    bold = _BOLD_TITLE_RE.match(first)
    if bold is not None and bold.group(1).strip():
        body = "\n".join(lines[i + 1 :]).strip()
        return _truncate_title(bold.group(1), _TITLE_MAX), body

    # 兜底：首个非空行当标题，正文保留全文（含该行）——只取标题不剜内容
    body = "\n".join(lines[i:]).strip()
    return _truncate_title(first, _FALLBACK_TITLE_MAX), body


def generate_article_from_prompt(
    *,
    session_factory: Callable[[], Any],
    user_id: int,
    template_content: str,
    question_text: str,
    model: str | None = None,
) -> int:
    """组装提示词 → 调用 LLM → 取标题 → 转 Tiptap/HTML → create_article。返回 article_id。

    通用系统提示词（不拼 Skill）。`model` 为方案级 AI 引擎覆盖（None / 空 = 用 settings.ai_model）。
    异常向上抛（由调用方记任务失败）。每次调用自带独立会话。
    """
    import litellm

    from server.app.core.config import resolve_engine
    from server.app.modules.ai_generation.converter import markdown_to_html, markdown_to_tiptap
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    model_str, api_key, base_url = resolve_engine(model)

    user_prompt = (
        render_question_prompt(template_content, question_text)
        + "\n\n请开始写作。第一行必须是 `# 标题`（井号后留一个空格），"
        "不要输出任何前言、解释或 ``` 代码块标记，只输出 Markdown 正文："
    )
    response = litellm.completion(
        model=model_str,
        messages=[
            {"role": "system", "content": _GENERIC_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        api_key=api_key or None,
        api_base=base_url or None,
        timeout=300,
        max_tokens=12000,
    )
    md_content = response.choices[0].message.content or ""

    title, body_md = extract_title_and_body(md_content)

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
        # AI 生文一律未审：不依赖运行后的 mark_pending_and_group 翻转
        article.review_status = "pending"
        db.commit()
        return article.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
