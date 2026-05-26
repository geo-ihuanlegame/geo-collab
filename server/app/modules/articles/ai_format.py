from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from server.app.core.config import get_settings
from server.app.modules.articles.parser import dumps_content_json, loads_content_json
from server.app.modules.image_library.inserter import has_images_in_content, insert_images_at_positions
from server.app.modules.image_library.selector import fetch_image_by_id, pick_image_id, ImageQuery

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_HEADINGS_ONLY = (
    "你是文章正文排版助手，只处理正文顶层节点，不处理文章主标题。\n"
    "\n"
    "正文节点列表：\n"
    "{% for node in text_nodes %}{{ node.index }} {{ node.label }} {{ node.text }}\n{% endfor %}\n"
    "找出应设为正文小标题（H1）的节点索引。\n"
    "- 小标题特征：短句、章节引导语、概括性短语，通常不超过 20 字\n"
    "- 不是小标题：完整叙述句、解释说明、数据陈述\n"
    "- 宁少勿多，不确定就不选\n"
    "- 不生成新标题，不改写任何文字\n"
    "\n"
    '返回：仅返回一行 JSON，不添加任何解释：{"heading_indices": [2, 7]}'
)

def _image_prompt_params(text_nodes: list[tuple[int, dict]]) -> tuple[int, int]:
    """Derive (max_images, min_spacing) from article structure.

    List-style articles (Top N, ranked items) typically have several headings or
    many short paragraph nodes.  For those we allow one image per section and
    relax the spacing constraint.  Narrative articles fall back to conservative
    defaults (3 images, 3-node gap).
    """
    existing_headings = sum(1 for _, n in text_nodes if n.get("type") == "heading")
    short_paragraphs = sum(
        1 for _, n in text_nodes
        if n.get("type") == "paragraph" and len(_node_text(n).strip()) <= 25
    )
    # Use existing headings first; fall back to counting short paragraphs that
    # are likely to become headings after the LLM pass.
    section_estimate = existing_headings if existing_headings >= 3 else (
        short_paragraphs if short_paragraphs >= 3 else 0
    )
    if section_estimate >= 3:
        return section_estimate, 3  # one image per section, min spacing 3
    return 3, 3  # conservative defaults for narrative articles


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


_PROMPT_DIR = Path(__file__).with_name("prompts")
_AI_FORMAT_WITH_IMAGES_TEMPLATE = _PROMPT_DIR / "ai_format_with_images.j2"


def _template_env() -> SandboxedEnvironment:
    return SandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _template_text_nodes(text_nodes: list[tuple[int, dict]]) -> list[dict[str, Any]]:
    return [
        {
            "index": index,
            "label": "[小标题]" if node.get("type") == "heading" else "[段落]",
            "text": _node_text(node),
        }
        for index, node in text_nodes
    ]


def _category_context(category: Any) -> dict[str, Any] | None:
    category_id = getattr(category, "id", None)
    if not isinstance(category_id, int):
        return None
    return {
        "id": category_id,
        "name": str(getattr(category, "name", "") or category_id),
        "description": getattr(category, "description", None),
    }


def _available_categories_for_article(article: Any, db: Any | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()

    for category in (getattr(article, "stock_categories", None) or []):
        item = _category_context(category)
        if item is not None and item["id"] not in seen:
            result.append(item)
            seen.add(item["id"])

    legacy_id = getattr(article, "stock_category_id", None)
    if isinstance(legacy_id, int) and legacy_id not in seen:
        category = getattr(article, "stock_category", None)
        if category is None and db is not None:
            from server.app.modules.image_library.models import StockCategory

            category = db.get(StockCategory, legacy_id)
        item = _category_context(category) if category is not None else {
            "id": legacy_id,
            "name": str(legacy_id),
            "description": None,
        }
        if item is not None:
            result.append(item)
            seen.add(item["id"])

    return result


def render_ai_format_prompt(
    template_source: str,
    *,
    text_nodes: list[tuple[int, dict]],
    available_categories: list[dict[str, Any]],
    max_images: int | None = None,
    min_spacing: int | None = None,
) -> str:
    derived_max_images, derived_min_spacing = _image_prompt_params(text_nodes)
    template = _template_env().from_string(template_source)
    return template.render(
        text_nodes=_template_text_nodes(text_nodes),
        available_categories=available_categories,
        max_images=max_images if max_images is not None else derived_max_images,
        min_spacing=min_spacing if min_spacing is not None else derived_min_spacing,
    )


def _builtin_prompt_template(include_images: bool) -> str:
    if include_images:
        return _AI_FORMAT_WITH_IMAGES_TEMPLATE.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT_HEADINGS_ONLY


def _fallback_prompt(
    include_images: bool,
    text_nodes: list[tuple[int, dict]] | None = None,
    available_categories: list[dict[str, Any]] | None = None,
) -> str:
    return render_ai_format_prompt(
        _builtin_prompt_template(include_images),
        text_nodes=text_nodes or [],
        available_categories=available_categories or [],
    )


def _load_ai_format_prompt(
    db: Any,
    *,
    preset_id: int | None,
    user_id: int | None,
    include_images: bool,
    text_nodes: list[tuple[int, dict]] | None = None,
    available_categories: list[dict[str, Any]] | None = None,
) -> str:
    if preset_id is None or user_id is None:
        return _fallback_prompt(include_images, text_nodes, available_categories)

    from server.app.modules.prompt_templates.service import get_visible_prompt_template

    prompt = get_visible_prompt_template(db, preset_id, user_id=user_id, scope="ai_format")
    if prompt is None or not prompt.is_enabled:
        logger.info("ai_format preset %s unavailable; falling back to built-in prompt", preset_id)
        return _fallback_prompt(include_images, text_nodes, available_categories)

    logger.info("ai_format using DB prompt template %s", preset_id)
    return render_ai_format_prompt(
        prompt.content,
        text_nodes=text_nodes or [],
        available_categories=available_categories or [],
    )


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
    if has_images_in_content(content_json):
        return content_json, 0

    category_ids: list[int] = [cat["id"] for cat in _available_categories_for_article(article, db)]
    if not category_ids:
        return content_json, 0

    image_positions_raw = parsed.get("image_positions", [])
    if not isinstance(image_positions_raw, list) or not image_positions_raw:
        return content_json, 0

    positions: list[int] = []
    requested_category_ids: list[int | None] = []
    for item in image_positions_raw:
        if isinstance(item, dict):
            idx = item.get("index")
            category_id = item.get("category_id")
            if isinstance(idx, int):
                positions.append(idx)
                requested_category_ids.append(category_id if isinstance(category_id, int) else None)
        elif isinstance(item, int):
            positions.append(item)
            requested_category_ids.append(None)

    if not positions:
        return content_json, 0

    valid_category_ids = set(category_ids)
    matched_refs = []
    matched_positions = []
    used_ids: list[int] = []
    for pos, requested_category_id in zip(positions, requested_category_ids):
        if requested_category_id is None or requested_category_id not in valid_category_ids:
            continue
        image_id = pick_image_id(
            ImageQuery(category_ids=[requested_category_id], excluded_ids=used_ids), db
        )
        if image_id is None:
            continue
        ref = fetch_image_by_id(image_id, db)
        if ref is not None:
            used_ids.append(image_id)
            matched_refs.append(ref)
            matched_positions.append(pos)

    if not matched_refs:
        return content_json, 0

    return insert_images_at_positions(content_json, matched_refs, matched_positions), len(matched_refs)


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

        get_settings.cache_clear()
        settings = get_settings()
        api_key = settings.ai_format_api_key or settings.ai_api_key or None
        if not api_key:
            raise AIFormatConfigurationError("AI 排版失败：未配置 API Key，请设置 GEO_AI_FORMAT_API_KEY。")

        available_categories = _available_categories_for_article(article, db) if include_images else []
        system_prompt = _load_ai_format_prompt(
            db,
            preset_id=preset_id,
            user_id=user_id,
            include_images=include_images,
            text_nodes=text_nodes,
            available_categories=available_categories,
        )
        response = _call_litellm_completion(
            model=settings.ai_format_model,
            api_key=api_key,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "请按上述要求完成分析，仅返回 JSON。"},
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
