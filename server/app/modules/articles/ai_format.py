from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from server.app.core.config import get_settings
from server.app.modules.articles.tiptap_Parser import loads_content_json, dumps_content_json

logger = logging.getLogger(__name__)

# 模式 A：无图且有栏目 — 同时识别小标题和插图位置
_SYSTEM_PROMPT_WITH_IMAGES = (
    "你是一个文章排版助手。给定编号的顶层节点列表（每行格式：序号 [类型]: 文本），请：\n"
    "1. 判断哪些节点应格式化为小标题（H1），小标题通常是简短的主题引导句，一般不超过20个字。\n"
    "2. 标出应插入配图的位置（在哪个节点索引之后插图），根据文章内容判断需要几张（如游戏推荐文章，每款游戏对应一张）。\n"
    '只返回合法 JSON，格式为 {"heading_indices": [0,3], "image_positions": [1,4,7]}，不输出任何其他内容。'
)

# 模式 B：已有图或无栏目 — 仅整理小标题（含重新评估已有标题）
_SYSTEM_PROMPT_HEADINGS_ONLY = (
    "你是一个文章排版助手。给定编号的顶层节点列表（每行格式：序号 [类型]: 文本），"
    "判断哪些节点应格式化为小标题（H1），包括验证已有小标题是否合逻辑、是否连贯。"
    '只返回合法 JSON，格式为 {"heading_indices": [0,3]}，没有小标题则返回 {"heading_indices": []}，不输出任何其他内容。'
)


def _top_level_text_nodes(content_json: dict) -> list[tuple[int, dict]]:
    """返回顶层的 paragraph 和 heading 节点，带原始索引。"""
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


def _apply_headings(content_json: dict, heading_indices: set[int]) -> dict:
    content = list(content_json.get("content") or [])
    for i, node in enumerate(content):
        if not isinstance(node, dict):
            continue
        if i in heading_indices and node.get("type") == "paragraph":
            content[i] = _to_heading(node)
        elif i not in heading_indices and node.get("type") == "heading":
            # 恢复为段落（LLM 认为不该是标题）
            content[i] = {"type": "paragraph", "content": node.get("content", [])}
    return {**content_json, "content": content}


def run_ai_format(article_id: int) -> None:
    """AI 格式化：识别小标题，若文章无图且已配置图库栏目则同时插图。"""
    from server.app.db.session import SessionLocal

    db = SessionLocal()
    try:
        from server.app.modules.articles.article_Crud import get_article
        from server.app.modules.image_library.inserter import has_images_in_content, insert_images_at_positions
        from server.app.modules.image_library.selector import ImageQuery, select_images

        article = get_article(db, article_id)
        if article is None or article.is_deleted:
            return

        content_json = loads_content_json(article.content_json)
        text_nodes = _top_level_text_nodes(content_json)
        if not text_nodes:
            return

        has_images = has_images_in_content(content_json)
        mode_a = not has_images and article.stock_category_id is not None
        system_prompt = _SYSTEM_PROMPT_WITH_IMAGES if mode_a else _SYSTEM_PROMPT_HEADINGS_ONLY

        listing = "\n".join(
            f"{i} {_node_label(node)}: {_node_text(node)}" for i, node in text_nodes
        )

        settings = get_settings()
        from litellm import completion

        response = completion(
            model=settings.ai_format_model,
            api_key=settings.ai_format_api_key or None,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": listing},
            ],
            temperature=0,
        )

        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        heading_indices = set(parsed.get("heading_indices", []))

        new_content_json = _apply_headings(content_json, heading_indices)

        if mode_a:
            image_positions: list[int] = parsed.get("image_positions", [])
            if image_positions and article.stock_category_id:
                refs = select_images(
                    ImageQuery(category_id=article.stock_category_id, count=len(image_positions)),
                    db,
                )
                if refs:
                    new_content_json = insert_images_at_positions(new_content_json, refs, image_positions)

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
            f" + {len(refs) if mode_a else 0} images" if mode_a else "",
            article_id,
        )

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
