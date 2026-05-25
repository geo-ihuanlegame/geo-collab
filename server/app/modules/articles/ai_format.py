from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from server.app.core.config import get_settings
from server.app.modules.articles.parser import dumps_content_json, loads_content_json

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

_SYSTEM_PROMPT_WITH_IMAGES = (
    "你是正文排版助手。你只处理文章正文里的顶层节点，不处理文章主标题。\n"
    "输入是若干行，格式为：<顶层 content 原始索引> [段落/小标题]: 文本。\n"
    "任务：\n"
    "1. 判断哪些原始索引应该设置为正文小标题（H1）。\n"
    "2. 判断哪些原始索引后面适合插入配图（段落之后、相邻索引间隔不少于 2）。\n"
    "规则：\n"
    "- 不生成新标题，不改写文本，不改文章主标题。\n"
    "- 只返回顶层 content 的原始索引，不要重新编号。\n"
    "- 小标题宁少勿多，不确定就不选。\n"
    "- 配图位置：每 3-5 个段落最多 1 张，全文不超过 3 张，不在标题节点之后放图。\n"
    '返回：只返回一行 JSON，格式固定为 {"heading_indices": [2,7], "image_positions": [4,9]}；'
    "没有则对应字段返回空数组。"
)


def _fallback_prompt(include_images: bool) -> str:
    return _SYSTEM_PROMPT_WITH_IMAGES if include_images else _SYSTEM_PROMPT_HEADINGS_ONLY


def _load_ai_format_prompt(
    db: Any,
    *,
    preset_id: int | None,
    user_id: int | None,
    include_images: bool,
) -> str:
    if preset_id is None or user_id is None:
        return _fallback_prompt(include_images)

    from server.app.modules.prompt_templates.service import get_visible_prompt_template

    prompt = get_visible_prompt_template(db, preset_id, user_id=user_id, scope="ai_format")
    if prompt is None or not prompt.is_enabled:
        logger.info("ai_format preset %s unavailable; falling back to built-in prompt", preset_id)
        return _fallback_prompt(include_images)

    logger.info("ai_format using DB prompt template %s", preset_id)
    return prompt.content


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


def _non_empty_text_nodes(content_json: dict) -> list[tuple[int, dict]]:
    return [(i, node) for i, node in _top_level_text_nodes(content_json) if _node_text(node).strip()]


def has_ai_format_targets(raw_content_json: Any) -> bool:
    if isinstance(raw_content_json, str):
        content_json = loads_content_json(raw_content_json)
    elif isinstance(raw_content_json, dict):
        content_json = raw_content_json
    else:
        content_json = {}
    return bool(_non_empty_text_nodes(content_json))


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
    """Only upgrade paragraphs to headings; never demote existing headings.

    The LLM identifies which paragraphs should become headings.  Existing
    headings NOT selected by the LLM are preserved as-is — the prompt says
    "保留" (retain), so absence from heading_indices does NOT mean demote.
    """
    content = list(content_json.get("content") or [])
    for i, node in enumerate(content):
        if not isinstance(node, dict):
            continue
        if i in heading_indices and node.get("type") == "paragraph":
            content[i] = _to_heading(node)
    return {**content_json, "content": content}


def _article_lock_matches(article: Any, lock_started_at: datetime | None) -> bool:
    if lock_started_at is None:
        return True
    return bool(article.ai_checking and article.ai_checking_started_at == lock_started_at)


class AIFormatConfigurationError(RuntimeError):
    """Raised when AI format cannot start because local model config is incomplete."""


def _describe_ai_format_error(exc: BaseException) -> str:
    raw = str(exc).strip()
    lower = raw.lower()
    if isinstance(exc, AIFormatConfigurationError):
        return raw
    if "insufficient balance" in lower or "payment required" in lower or "402" in lower or "quota" in lower:
        return "AI 排版失败：DeepSeek 账户余额不足，请充值或更换 API Key。"
    if (
        "unauthorized" in lower
        or "authentication" in lower
        or "invalid api key" in lower
        or "invalid_api_key" in lower
        or "401" in lower
    ):
        return "AI 排版失败：API Key 无效或无权限，请检查 GEO_AI_FORMAT_API_KEY。"
    if "rate limit" in lower or "too many requests" in lower or "429" in lower:
        return "AI 排版失败：模型服务触发限流，请稍后重试。"
    if "model" in lower and ("not found" in lower or "does not exist" in lower or "404" in lower):
        return "AI 排版失败：模型名称无效，请检查 GEO_AI_FORMAT_MODEL。"
    if "timeout" in lower or "timed out" in lower or "read timed out" in lower:
        return "AI 排版失败：模型服务响应超时，请稍后重试。"
    if "connection" in lower or "network" in lower or "name resolution" in lower:
        return "AI 排版失败：无法连接模型服务，请检查服务器网络。"
    if isinstance(exc, json.JSONDecodeError) or "json" in lower:
        return "AI 排版失败：模型返回格式异常，请重试。"
    return "AI 排版失败：后台任务异常，请查看 app 容器日志。"


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


def _unlock_ai_format(
    db: Any,
    article_id: int,
    lock_started_at: datetime | None,
    *,
    error_message: str | None = None,
) -> None:
    from server.app.modules.articles.service import get_article

    article = get_article(db, article_id)
    if article is None or not _article_lock_matches(article, lock_started_at):
        return
    article.ai_checking = False
    article.ai_checking_started_at = None
    if error_message is not None:
        article.ai_format_error = error_message
    db.commit()


def run_ai_format(
    article_id: int,
    *,
    include_images: bool = False,
    lock_started_at: datetime | None = None,
    preset_id: int | None = None,
    user_id: int | None = None,
) -> None:
    """Identify body subheadings and write the updated Tiptap document back to the article."""
    db = None
    error_message: str | None = None
    try:
        from server.app.db.session import SessionLocal
        db = SessionLocal()
        from server.app.modules.articles.service import get_article

        article = get_article(db, article_id)
        if article is None or article.is_deleted:
            return
        if not _article_lock_matches(article, lock_started_at):
            logger.info("ai_format skipped stale lock before model call for article %s", article_id)
            return

        content_json = loads_content_json(article.content_json)
        text_nodes = _non_empty_text_nodes(content_json)
        if not text_nodes:
            logger.info("ai_format skipped article %s: no non-empty paragraph/heading nodes", article_id)
            return

        listing = "\n".join(
            f"{i} {_node_label(node)}: {_node_text(node)}" for i, node in text_nodes
        )

        get_settings.cache_clear()
        settings = get_settings()
        api_key = settings.ai_format_api_key or settings.ai_api_key or None
        if not api_key:
            raise AIFormatConfigurationError("AI 排版失败：未配置 API Key，请设置 GEO_AI_FORMAT_API_KEY。")

        system_prompt = _load_ai_format_prompt(
            db,
            preset_id=preset_id,
            user_id=user_id,
            include_images=include_images,
        )
        response = _call_litellm_completion(
            model=settings.ai_format_model,
            api_key=api_key,
            messages=[
                {"role": "system", "content": system_prompt},
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

    except Exception as exc:
        if db is not None:
            db.rollback()
        error_message = _describe_ai_format_error(exc)
        logger.exception("ai_format failed for article %s", article_id)
    finally:
        if db is not None:
            try:
                _unlock_ai_format(
                    db,
                    article_id,
                    lock_started_at,
                    error_message=error_message,
                )
            except Exception:
                db.rollback()
                logger.exception("ai_format unlock failed for article %s", article_id)
            db.close()
