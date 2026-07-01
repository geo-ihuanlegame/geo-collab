"""Repair generated articles polluted by literal JSON-escaped quotes.

Usage:
    python -m server.scripts.repair_article_escaped_quotes
    python -m server.scripts.repair_article_escaped_quotes --apply --ids 1425

The default mode is a dry run.  Only ``--apply`` commits changes.
"""

from __future__ import annotations

import argparse
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# Standalone scripts need all ORM modules imported before mapper configuration.
import server.app.modules.accounts.models  # noqa: F401,E402
import server.app.modules.ai_generation.models  # noqa: F401,E402
import server.app.modules.articles.models  # noqa: F401,E402
import server.app.modules.audit.models  # noqa: F401,E402
import server.app.modules.image_library.models  # noqa: F401,E402
import server.app.modules.prompt_templates.models  # noqa: F401,E402
import server.app.modules.skills.models  # noqa: F401,E402
import server.app.modules.system.models  # noqa: F401,E402
import server.app.modules.tasks.models  # noqa: F401,E402
from server.app.modules.ai_generation.markdown_sanitizer import normalize_markdown_content
from server.app.modules.articles.models import Article
from server.app.modules.articles.parser import dumps_content_json, loads_content_json


def _normalize_tiptap_text_nodes(node: Any) -> bool:
    changed = False
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str):
            normalized = normalize_markdown_content(text)
            if normalized != text:
                node["text"] = normalized
                changed = True
        content = node.get("content")
        if isinstance(content, list):
            for child in content:
                changed = _normalize_tiptap_text_nodes(child) or changed
    elif isinstance(node, list):
        for child in node:
            changed = _normalize_tiptap_text_nodes(child) or changed
    return changed


def _repair_article(article: Article) -> bool:
    changed = False

    for attr in ("plain_text", "content_html"):
        raw = getattr(article, attr)
        if not isinstance(raw, str):
            continue
        normalized = normalize_markdown_content(raw)
        if normalized != raw:
            setattr(article, attr, normalized)
            changed = True

    try:
        content_json = loads_content_json(article.content_json)
    except Exception:
        content_json = {}
    if content_json and _normalize_tiptap_text_nodes(content_json):
        article.content_json = dumps_content_json(content_json)
        changed = True

    if changed:
        article.word_count = len(article.plain_text or "")
    return changed


def repair_articles(session: Session, *, ids: list[int] | None = None) -> list[int]:
    stmt = select(Article)
    if ids:
        stmt = stmt.where(Article.id.in_(ids))
    else:
        stmt = stmt.where(Article.is_deleted.is_(False))

    repaired: list[int] = []
    for article in session.execute(stmt).scalars().all():
        if _repair_article(article):
            repaired.append(article.id)
    return repaired


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair article bodies containing literal JSON-escaped quotes."
    )
    parser.add_argument("--apply", action="store_true", help="Commit the repaired rows.")
    parser.add_argument("--ids", nargs="*", type=int, help="Optional article IDs to inspect.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    from server.app.db.session import SessionLocal

    with SessionLocal() as session:
        repaired = repair_articles(session, ids=args.ids)
        if args.apply:
            session.commit()
            verb = "repaired"
        else:
            session.rollback()
            verb = "would repair"

    print(f"{verb} {len(repaired)} article(s)")
    if repaired:
        print("article ids: " + ", ".join(str(article_id) for article_id in repaired))


if __name__ == "__main__":
    main()
