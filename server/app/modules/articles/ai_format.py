from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from server.app.core.config import get_settings
from server.app.modules.articles.tiptap_Parser import loads_content_json, dumps_content_json

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a document formatter. Given a numbered list of article paragraphs, "
    "identify which indices (0-based) should be formatted as H1 headings. "
    "Headings are short topic-introducing phrases, typically under 20 characters. "
    'Respond ONLY with valid JSON: {"heading_indices": [0, 3]} '
    'or {"heading_indices": []} if none.'
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
            model=settings.ai_model,
            api_key=settings.ai_api_key or None,
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
            article.content_json = dumps_content_json(new_content_json)
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
