from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from server.app.core.config import get_settings
from server.app.modules.articles.tiptap_Parser import loads_content_json, dumps_content_json

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是一个文章排版助手。给定一组编号段落，判断哪些段落应该格式化为小标题（H1）。"
    "小标题通常是简短的主题引导句，一般不超过20个字。"
    "只返回合法 JSON，格式为 {\"heading_indices\": [0, 3]}，没有小标题则返回 {\"heading_indices\": []}。"
    "不要输出任何其他内容。"
)


def _top_level_paragraphs(content_json: dict) -> list[tuple[int, dict]]:
    content = content_json.get("content") or []
    return [
        (i, node)
        for i, node in enumerate(content)
        if isinstance(node, dict) and node.get("type") == "paragraph"
    ]


def _paragraph_text(node: dict) -> str:
    return "".join(
        child.get("text", "")
        for child in (node.get("content") or [])
        if isinstance(child, dict) and child.get("type") == "text"
    )


def _to_heading(node: dict, level: int = 1) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": node.get("content", [])}


def _node_text(node: dict) -> str:
    """Extract plain text from a single block node's children."""
    parts = []
    for child in node.get("content") or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") == "text":
            parts.append(child.get("text", ""))
        elif child.get("type") == "hardBreak":
            parts.append("\n")
    return "".join(parts)


def _node_html(node: dict) -> str:
    """Render a single block node to simple HTML (heading or paragraph)."""
    inner_parts = []
    for child in node.get("content") or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") != "text":
            continue
        text = child.get("text", "")
        marks = child.get("marks") or []
        is_bold = any(isinstance(m, dict) and m.get("type") == "bold" for m in marks)
        inner_parts.append(f"<strong>{text}</strong>" if is_bold else text)
    inner = "".join(inner_parts)
    node_type = node.get("type")
    if node_type == "heading":
        level = (node.get("attrs") or {}).get("level", 1)
        return f"<h{level}>{inner}</h{level}>"
    return f"<p>{inner}</p>"


def _derive_html_and_text(content_json: dict) -> tuple[str, str]:
    """Regenerate content_html and plain_text from updated content_json."""
    html_parts: list[str] = []
    text_parts: list[str] = []
    for node in content_json.get("content") or []:
        if not isinstance(node, dict):
            continue
        ntype = node.get("type")
        if ntype in ("heading", "paragraph"):
            html_parts.append(_node_html(node))
            t = _node_text(node)
            if t.strip():
                text_parts.append(t)
    return "".join(html_parts), "\n".join(text_parts)


def _apply_headings(content_json: dict, heading_indices: set[int]) -> dict:
    content = list(content_json.get("content") or [])
    for i, node in enumerate(content):
        if i in heading_indices and isinstance(node, dict) and node.get("type") == "paragraph":
            content[i] = _to_heading(node)
    return {**content_json, "content": content}


def run_ai_format(article_id: int) -> None:
    """Run AI heading detection for article_id. Always unlocks the article when done."""
    from server.app.db.session import SessionLocal

    db = SessionLocal()
    try:
        from server.app.modules.articles.article_Crud import get_article

        article = get_article(db, article_id)
        if article is None or article.is_deleted:
            return

        content_json = loads_content_json(article.content_json)
        paragraphs = _top_level_paragraphs(content_json)
        if not paragraphs:
            return

        listing = "\n".join(f"{i}: {_paragraph_text(node)}" for i, node in paragraphs)

        settings = get_settings()
        from litellm import completion

        response = completion(
            model=settings.ai_format_model,
            api_key=settings.ai_format_api_key or None,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": listing},
            ],
            temperature=0,
        )

        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        heading_indices = set(parsed.get("heading_indices", []))

        if heading_indices:
            new_content_json = _apply_headings(content_json, heading_indices)
            new_html, new_text = _derive_html_and_text(new_content_json)
            article.content_json = dumps_content_json(new_content_json)
            article.content_html = new_html
            article.plain_text = new_text
            article.version += 1
            article.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            logger.info("ai_format applied %d headings to article %s", len(heading_indices), article_id)

    except Exception:
        logger.exception("ai_format failed for article %s", article_id)
    finally:
        try:
            from server.app.modules.articles.article_Crud import get_article as _get

            article = _get(db, article_id)
            if article is not None:
                article.ai_checking = False
                article.ai_checking_started_at = None
                db.commit()
        except Exception:
            logger.exception("ai_format unlock failed for article %s", article_id)
        db.close()
