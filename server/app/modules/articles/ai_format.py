from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from server.app.core.config import get_settings
from server.app.modules.articles.tiptap_Parser import dumps_content_json, loads_content_json

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_HEADINGS_ONLY = (
    "你是正文排版助手。你只处理文章正文里的顶层节点，不处理文章主标题。\n"
    "输入是若干行，格式为：<顶层 content 原始索引> [段落/小标题]: 文本。\n"
    "任务：判断哪些原始索引应该保留或设置为正文小标题（H1）。\n"
    "规则：\n"
    "1. 不生成新标题，不改写文本，不改文章主标题。\n"
    "2. 只返回顶层 content 的原始索引，不要重新编号。\n"
    "3. 小标题通常是短句、章节引导、概括性短语，不是完整叙述句。\n"
    "4. 宁少勿多，不确定就不要选。\n"
    '返回：只返回一行 JSON，格式固定为 {"heading_indices": [2,7]}；没有则返回 {"heading_indices": []}。'
)


def _extract_json(raw: str) -> str:
    """Extract the first JSON object from a model response."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    return m.group(0) if m else raw


def _top_level_text_nodes(content_json: dict) -> list[tuple[int, dict]]:
    """Return top-level paragraph/heading nodes with their original content indices."""
    content = content_json.get("content") or []
    return [
        (i, node)
        for i, node in enumerate(content)
        if isinstance(node, dict) and node.get("type") in ("paragraph", "heading")
    ]


def _node_text(node: dict) -> str:
    parts = []
    for child in node.get("content") or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") == "text":
            parts.append(child.get("text", ""))
        elif child.get("type") == "hardBreak":
            parts.append("\n")
    return "".join(parts)


def _node_label(node: dict) -> str:
    return "[小标题]" if node.get("type") == "heading" else "[段落]"


def _to_heading(node: dict, level: int = 1) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": node.get("content", [])}


def _to_paragraph(node: dict) -> dict:
    return {"type": "paragraph", "content": node.get("content", [])}


def _node_html(node: dict) -> str:
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


def _normalize_heading_indices(value: Any, valid_indices: set[int]) -> set[int]:
    if not isinstance(value, list):
        return set()
    result: set[int] = set()
    for item in value:
        if isinstance(item, int) and item in valid_indices:
            result.add(item)
    return result


def _apply_headings(content_json: dict, heading_indices: set[int]) -> dict:
    content = list(content_json.get("content") or [])
    for i, node in enumerate(content):
        if not isinstance(node, dict):
            continue
        if i in heading_indices and node.get("type") == "paragraph":
            content[i] = _to_heading(node)
        elif i not in heading_indices and node.get("type") == "heading":
            content[i] = _to_paragraph(node)
    return {**content_json, "content": content}


def _article_lock_matches(article: Any, lock_started_at: datetime | None) -> bool:
    if lock_started_at is None:
        return True
    return bool(article.ai_checking and article.ai_checking_started_at == lock_started_at)


def _call_litellm_completion(
    *,
    model: str,
    api_key: str | None,
    messages: list[dict[str, str]],
    timeout_seconds: int,
) -> Any:
    from litellm import completion

    return completion(
        model=model,
        api_key=api_key,
        messages=messages,
        temperature=0,
        timeout=timeout_seconds,
    )


def _maybe_insert_images(content_json: dict, parsed: dict, article: Any, db: Any) -> tuple[dict, int]:
    from server.app.modules.image_library.inserter import has_images_in_content, insert_images_at_positions
    from server.app.modules.image_library.selector import ImageQuery, select_images

    if has_images_in_content(content_json) or article.stock_category_id is None:
        return content_json, 0

    image_positions = parsed.get("image_positions", [])
    if not isinstance(image_positions, list) or not image_positions:
        return content_json, 0

    refs = select_images(
        ImageQuery(category_id=article.stock_category_id, count=len(image_positions)),
        db,
    )
    if not refs:
        return content_json, 0
    return insert_images_at_positions(content_json, refs, image_positions), len(refs)


def _unlock_ai_format(db: Any, article_id: int, lock_started_at: datetime | None) -> None:
    from server.app.modules.articles.article_Crud import get_article

    article = get_article(db, article_id)
    if article is None or not _article_lock_matches(article, lock_started_at):
        return
    article.ai_checking = False
    article.ai_checking_started_at = None
    db.commit()


def run_ai_format(
    article_id: int,
    *,
    include_images: bool = False,
    lock_started_at: datetime | None = None,
) -> None:
    """Identify body subheadings and write the updated Tiptap document back to the article."""
    from server.app.db.session import SessionLocal

    db = SessionLocal()
    try:
        from server.app.modules.articles.article_Crud import get_article

        article = get_article(db, article_id)
        if article is None or article.is_deleted:
            return
        if not _article_lock_matches(article, lock_started_at):
            logger.info("ai_format skipped stale lock before model call for article %s", article_id)
            return

        content_json = loads_content_json(article.content_json)
        text_nodes = _top_level_text_nodes(content_json)
        if not text_nodes:
            return

        listing = "\n".join(
            f"{i} {_node_label(node)}: {_node_text(node)}" for i, node in text_nodes
        )

        settings = get_settings()
        response = _call_litellm_completion(
            model=settings.ai_format_model,
            api_key=settings.ai_format_api_key or None,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT_HEADINGS_ONLY},
                {"role": "user", "content": listing},
            ],
            timeout_seconds=settings.ai_format_timeout_seconds,
        )

        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(_extract_json(raw))
        valid_indices = {i for i, _ in text_nodes}
        heading_indices = _normalize_heading_indices(parsed.get("heading_indices", []), valid_indices)

        new_content_json = _apply_headings(content_json, heading_indices)
        image_count = 0
        if include_images:
            new_content_json, image_count = _maybe_insert_images(new_content_json, parsed, article, db)

        db.refresh(article)
        if not _article_lock_matches(article, lock_started_at):
            logger.info("ai_format skipped stale lock before write for article %s", article_id)
            return

        new_html, new_text = _derive_html_and_text(new_content_json)
        article.content_json = dumps_content_json(new_content_json)
        article.content_html = new_html
        article.plain_text = new_text
        article.version += 1
        article.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        logger.info(
            "ai_format applied %d headings%s to article %s",
            len(heading_indices),
            f" + {image_count} images" if image_count else "",
            article_id,
        )

    except Exception:
        db.rollback()
        logger.exception("ai_format failed for article %s", article_id)
    finally:
        try:
            _unlock_ai_format(db, article_id, lock_started_at)
        except Exception:
            db.rollback()
            logger.exception("ai_format unlock failed for article %s", article_id)
        db.close()
