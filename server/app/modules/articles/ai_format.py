"""
AI 自动排版：让格式模型识别正文里哪些段落该升级成小标题，并（可选）自动配图。

入口 run_ai_format（在 router 的后台线程里跑）：拉文章 → 拍平顶层 paragraph/heading 节点
→ 渲染提示词（DB 模板优先，回退内置）→ 调 LiteLLM（模型/Key 走 GEO_AI_FORMAT_*，Key 缺失时回退 GEO_AI_API_KEY）→ 解析 JSON
（heading_indices / image_positions）→ 只把段落升级为标题、绝不降级已有标题 → 同步回三份正文。

并发/锁语义（关键）：
  - Article.ai_checking / ai_checking_started_at 是这套排版的“锁”，由 router 在触发时置位。
  - lock_started_at 是锁的指纹：模型调用前、写回前都用 _article_lock_matches 复核，
    指纹对不上（被新一轮排版或超时清锁覆盖）就直接放弃本次写回，避免覆盖更新的结果。
  - 全程独立 session（SessionLocal），finally 里 _unlock_ai_format 释放锁并落 error_message。
  - 所有模型/网络/JSON 异常经 _describe_ai_format_error 翻成面向运营的中文提示存到 ai_format_error。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from server.app.core.config import get_settings
from server.app.modules.articles.parser import dumps_content_json, loads_content_json
from server.app.modules.image_library.inserter import (
    has_images_in_content,
    insert_images_at_positions,
)
from server.app.modules.image_library.selector import ImageQuery, fetch_image_by_id, pick_image_id

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_HEADINGS_ONLY = (
    "你是文章正文排版助手，只处理正文顶层节点，不处理文章主标题。\n"
    "\n"
    "【小标题判断标准：语义角色优先】\n"
    "小标题 = 章节标签节点，自身不传递信息，只为后续内容命名。\n"
    "判断方法：把该节点从文章中删掉，若后续段落仍语义完整，它才可能是小标题。\n"
    "\n"
    "IS 小标题（必须同时满足）：\n"
    "- 节点本身不推进任何观点，只起命名/标记作用\n"
    "- 删除该节点后，前后段落无逻辑断层\n"
    "- 常见形态：纯标签（「英雄分类」）、章节编号（「一、xxx」）\n"
    "\n"
    "NOT 小标题（以下任一即排除）：\n"
    "- 节点本身含有观点、数据或论据（哪怕很短）\n"
    "- 节点是上下文论述链中的一环（删除后前后文断裂）\n"
    "- 修辞句、对比句、判断句——无论句式如何，都是正文的一部分\n"
    "- 段落开头有加粗标签+冒号（如「广告克制：……」），整体是正文句，不是标题\n"
    "\n"
    "宁少勿多：不确定是标签还是论述，就不选。不生成新标题，不改写任何文字。\n"
    "\n"
    "【示例一：标签型标题】\n"
    "节点列表：\n"
    "0 [段落] 《王者荣耀》是腾讯旗下一款多人在线竞技手游，上线以来长期占据手游下载榜首位。\n"
    "1 [段落] 英雄分类\n"
    "2 [段落] 游戏目前拥有超过100名英雄，按职业分为战士、法师、射手等六大类，不同职业定位各异。\n"
    "3 [段落] 组队时需要注意职业搭配，保证阵容均衡才能发挥最大战力。\n"
    "4 [段落] 段位体系\n"
    "5 [段落] 排位赛分为青铜、白银、黄金、铂金、钻石、星耀和王者共7个大段位，每个大段位下设多个小段位。\n"
    "分析：节点1「英雄分类」删除后节点2语义不变，是纯标签。节点4同理。\n"
    '返回：{"heading_indices": [1, 4]}\n'
    "\n"
    "【示例二：修辞句 + 加粗标签句都是正文（反面示例）】\n"
    "节点列表：\n"
    "0 [段落] 这款游戏值得推荐的理由，不仅仅是玩法本身。\n"
    "1 [段落] 它不是单纯开店，而是「合成—经营—剧情」三轮驱动。\n"
    "2 [段落] 游戏初期，你需要收集原材料，通过合成系统将其加工成商品，再在小店里出售给顾客。\n"
    "3 [段落] 广告自愿、付费克制的运营节奏：游戏里看广告主要用来换体力，是可选项，不卡进度。\n"
    "4 [段落] 随着经营推进，一条跨越多代人的家族叙事线会缓缓展开，每个 NPC 都有自己的命运弧线。\n"
    "5 [段落] 总体来说，如果你喜欢有深度的经营类游戏，这款值得一试。\n"
    "分析：节点1是修辞句，本身承载观点，删除后前后文断裂；节点3开头有加粗标签+冒号，但整体是一段完整论述，删除后语义缺失。两者均是正文，不是标题。\n"
    '返回：{"heading_indices": []}\n'
    "\n"
    "【示例三：无小标题（全叙述）】\n"
    "节点列表：\n"
    "0 [段落] 在开始正式内容之前，先来了解一下这款游戏的基本背景。\n"
    "1 [段落] 游戏于2022年正式上线，历经两年发展，目前注册用户已超过8000万。\n"
    "2 [段落] 值得关注的是，该游戏在东南亚市场的表现尤为亮眼，在泰国和越南均取得了畅销榜第一名。\n"
    "3 [段落] 开发团队表示，未来将持续更新内容，包括新地图、新角色和新玩法。\n"
    '返回：{"heading_indices": []}\n'
    "\n"
    "【当前任务】\n"
    "正文节点列表：\n"
    "{% for node in text_nodes %}{{ node.index }} {{ node.label }} {{ node.text }}\n{% endfor %}\n"
    '返回：仅返回一行 JSON，不添加任何解释：{"heading_indices": [2, 7]}'
)


def _image_prompt_params(text_nodes: list[tuple[int, dict]]) -> tuple[int, int]:
    """根据文章结构推导 (max_images, min_spacing)。

    清单型文章（Top N、榜单项）通常有多个标题或大量短段落。此类文章最多配
    3 张图，并要求 5 个节点间距，避免图片过密。叙事型文章也使用 5 节点间距
    和最多 3 张图的上限。
    """
    existing_headings = sum(1 for _, n in text_nodes if n.get("type") == "heading")
    short_paragraphs = sum(
        1
        for _, n in text_nodes
        if n.get("type") == "paragraph" and len(_node_text(n).strip()) <= 25
    )
    # 优先使用已有标题估算分节数；不足时再统计可能被 LLM 升级成标题的短段落。
    section_estimate = (
        existing_headings
        if existing_headings >= 3
        else (short_paragraphs if short_paragraphs >= 3 else 0)
    )
    if section_estimate >= 3:
        # 不论分节数多少，最多 3 张图；要求 5 节点间距，避免每隔几句就插图。
        return min(section_estimate, 3), 5
    return 3, 5  # 叙事型文章：同样的间距和 3 张图上限


def _extract_json(raw: str) -> str:
    """从模型响应中提取第一个 JSON 对象。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    return m.group(0) if m else raw


def _top_level_text_nodes(content_json: dict) -> list[tuple[int, dict]]:
    """返回顶层 paragraph/heading 节点及其原始 content 下标。"""
    content = content_json.get("content") or []
    return [
        (i, node)
        for i, node in enumerate(content)
        if isinstance(node, dict) and node.get("type") in ("paragraph", "heading")
    ]


def _non_empty_text_nodes(content_json: dict) -> list[tuple[int, dict]]:
    return [
        (i, node) for i, node in _top_level_text_nodes(content_json) if _node_text(node).strip()
    ]


def has_ai_format_targets(raw_content_json: Any) -> bool:
    """正文里是否有可排版对象（非空的顶层 paragraph/heading 节点）。空正文时 router 拒绝触发排版。"""
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
# 「积极配图」内置变体：每款明确出现的游戏都配图（保留"不确定不插"准星）。仅 AI配图 节点
# 在 aggressive_images=True（默认）时选用；手动排版/方案配图仍走上面的保守模板。
_AI_FORMAT_WITH_IMAGES_AGGRESSIVE_TEMPLATE = _PROMPT_DIR / "ai_format_with_images_aggressive.j2"


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

    for category in getattr(article, "stock_categories", None) or []:
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
        item = (
            _category_context(category)
            if category is not None
            else {
                "id": legacy_id,
                "name": str(legacy_id),
                "description": None,
            }
        )
        if item is not None:
            result.append(item)
            seen.add(item["id"])

    return result


def all_category_contexts(db: Any) -> list[dict[str, Any]]:
    """返回系统里全部图片栏目（StockCategory）的 {id,name,description} 上下文。

    供方案自动配图：候选栏目取全部 bucket（而非文章已分配的类别），
    让模型按文章游戏内容自行匹配；匹配不上则返回空 image_positions。
    """
    from server.app.modules.image_library.models import StockCategory

    result: list[dict[str, Any]] = []
    for category in db.query(StockCategory).order_by(StockCategory.id.asc()).all():
        item = _category_context(category)
        if item is not None:
            result.append(item)
    return result


def category_contexts_for(
    db: Any,
    *,
    main_category_id: int,
    include_companion: bool = True,
) -> list[dict[str, Any]]:
    """配图节点候选栏目：主推栏目 + (可选)全部 kind=companion 栏目。

    返回 [{id,name,description}, ...]，主推排第一、去重。供 ai_illustrate 节点喂给
    run_ai_format 的 candidate_categories。
    """
    from server.app.modules.image_library.models import StockCategory

    result: list[dict[str, Any]] = []
    seen: set[int] = set()

    main = db.get(StockCategory, main_category_id)
    if main is not None:
        item = _category_context(main)
        if item is not None:
            result.append(item)
            seen.add(item["id"])

    if include_companion:
        companions = (
            db.query(StockCategory)
            .filter(StockCategory.kind == "companion")
            .order_by(StockCategory.id.asc())
            .all()
        )
        for cat in companions:
            item = _category_context(cat)
            if item is not None and item["id"] not in seen:
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
    web_fallback: bool = False,
) -> str:
    derived_max_images, derived_min_spacing = _image_prompt_params(text_nodes)
    template = _template_env().from_string(template_source)
    return template.render(
        text_nodes=_template_text_nodes(text_nodes),
        available_categories=available_categories,
        max_images=max_images if max_images is not None else derived_max_images,
        min_spacing=min_spacing if min_spacing is not None else derived_min_spacing,
        web_fallback=web_fallback,
    )


def _builtin_prompt_template(include_images: bool, variant: str = "conservative") -> str:
    if include_images:
        if variant == "aggressive":
            return _AI_FORMAT_WITH_IMAGES_AGGRESSIVE_TEMPLATE.read_text(encoding="utf-8")
        return _AI_FORMAT_WITH_IMAGES_TEMPLATE.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT_HEADINGS_ONLY


def _fallback_prompt(
    include_images: bool,
    text_nodes: list[tuple[int, dict]] | None = None,
    available_categories: list[dict[str, Any]] | None = None,
    web_fallback: bool = False,
    max_images: int | None = None,
    min_spacing: int | None = None,
    variant: str = "conservative",
) -> str:
    return render_ai_format_prompt(
        _builtin_prompt_template(include_images, variant),
        text_nodes=text_nodes or [],
        available_categories=available_categories or [],
        max_images=max_images,
        min_spacing=min_spacing,
        web_fallback=web_fallback,
    )


def _load_ai_format_prompt(
    db: Any,
    *,
    preset_id: int | None,
    user_id: int | None,
    include_images: bool,
    text_nodes: list[tuple[int, dict]] | None = None,
    available_categories: list[dict[str, Any]] | None = None,
    web_fallback: bool = False,
    max_images: int | None = None,
    min_spacing: int | None = None,
    builtin_variant: str = "conservative",
) -> str:
    """组装完整的 AI 排版/配图系统提示词。

    max_images / min_spacing 非空时覆盖按文章结构推导的默认值（注入提示词的 {{ max_images }} /
    {{ min_spacing }} 占位）；DB 模板与内置模板都吃这层覆盖。builtin_variant 仅在用内置模板
    （preset_id 缺省/不可用）时决定走保守还是「积极配图」变体。

    联网兜底（web_fallback=True 且 include_images）的 game 字段指引在这里统一拼到末尾，
    使本函数返回的就是模型最终看到的完整提示词——调用方（run_ai_format）不再二次拼接。
    """
    if preset_id is None or user_id is None:
        base = _fallback_prompt(
            include_images,
            text_nodes,
            available_categories,
            web_fallback,
            max_images=max_images,
            min_spacing=min_spacing,
            variant=builtin_variant,
        )
    else:
        from server.app.modules.prompt_templates.service import get_visible_prompt_template

        prompt = get_visible_prompt_template(db, preset_id, user_id=user_id, scope="ai_format")
        if prompt is None or not prompt.is_enabled:
            logger.info(
                "ai_format preset %s unavailable; falling back to built-in prompt", preset_id
            )
            base = _fallback_prompt(
                include_images,
                text_nodes,
                available_categories,
                web_fallback,
                max_images=max_images,
                min_spacing=min_spacing,
                variant=builtin_variant,
            )
        else:
            logger.info("ai_format using DB prompt template %s", preset_id)
            base = render_ai_format_prompt(
                prompt.content,
                text_nodes=text_nodes or [],
                available_categories=available_categories or [],
                max_images=max_images,
                min_spacing=min_spacing,
                web_fallback=web_fallback,
            )

    if include_images and web_fallback:
        from server.app.modules.prompt_templates.service import get_active_template_content

        base += get_active_template_content(
            db,
            scope="image_companion",
            user_id=user_id,
            default=_WEB_FALLBACK_PROMPT_SUFFIX,
        )
    return base


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
    """只把段落升级为标题，绝不降级已有标题。

    LLM 只识别哪些段落应成为标题。未被 LLM 选中的已有标题原样保留：提示词说
    “保留”，所以不在 heading_indices 中并不表示要降级。
    """
    content = list(content_json.get("content") or [])
    for i, node in enumerate(content):
        if not isinstance(node, dict):
            continue
        if i in heading_indices and node.get("type") == "paragraph":
            content[i] = _to_heading(node, level=2)
    return {**content_json, "content": content}


def _article_lock_matches(article: Any, lock_started_at: datetime | None) -> bool:
    # 锁指纹比对：本次排版仍持有锁吗？lock_started_at=None 表示不校验（无锁场景，如测试直调）
    if lock_started_at is None:
        return True
    return bool(article.ai_checking and article.ai_checking_started_at == lock_started_at)


class AIFormatConfigurationError(RuntimeError):
    """本地模型配置不完整，导致 AI 排版无法启动时抛出。"""


def _describe_ai_format_error(exc: BaseException) -> str:
    """把底层异常（余额/鉴权/限流/超时/网络/JSON 等）翻成面向运营的中文提示，存进 ai_format_error。"""
    raw = str(exc).strip()
    lower = raw.lower()
    if isinstance(exc, AIFormatConfigurationError):
        return raw
    if (
        "insufficient balance" in lower
        or "payment required" in lower
        or "402" in lower
        or "quota" in lower
    ):
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
    api_base: str | None = None,
) -> Any:
    from litellm import completion

    return completion(
        model=model,
        api_key=api_key,
        messages=messages,
        temperature=0,
        timeout=timeout_seconds,
        api_base=api_base or None,
    )


# 联网兜底提示：仅 web_fallback 开时由 _load_ai_format_prompt 拼到系统提示词末尾。
# 关键：明确告诉模型「列表外的游戏不要放弃配图，改用 game 点名」，并给一个具体示例——
# 否则正文里反复强调的「只能用列表内 category_id」会压过这条，模型对陪衬游戏一律返回空。
_WEB_FALLBACK_PROMPT_SUFFIX = (
    "\n\n【联网兜底（已启用，重要）】\n"
    "本文已开启联网兜底。段落明确属于某款游戏、但该游戏【不在】上方可用栏目列表里时，"
    "不要因为它不在列表就放弃配图，而要在 image_positions 里用游戏名点名"
    "（系统会自动建栏目并联网补图）：\n"
    '  {"index": 段落索引, "game": "游戏中文名"}\n'
    "- 列表内的游戏仍用 category_id；仅当游戏【不在】列表里、且你确有把握时才用 game\n"
    "- game 用规范中文游戏名（如「蛋仔派对」），不带书名号/版本号/多余修饰\n"
    "- 不确定段落属于哪款游戏就不插（宁缺勿错）\n"
    "示例：第 3 段在讲一款叫「蛋仔派对」的游戏，而可用栏目里只有主推游戏、没有它 →\n"
    '  image_positions 里加一项 {"index": 3, "game": "蛋仔派对"}\n'
)


def _parse_image_positions(raw: Any) -> list[tuple[int, int | None, str | None]]:
    """解析 image_positions：每项 (index, category_id|None, game_name|None)。"""
    out: list[tuple[int, int | None, str | None]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            idx = item.get("index")
            if not isinstance(idx, int):
                continue
            cat = item.get("category_id")
            game = item.get("game")
            out.append(
                (
                    idx,
                    cat if isinstance(cat, int) else None,
                    game.strip() if isinstance(game, str) and game.strip() else None,
                )
            )
        elif isinstance(item, int):
            out.append((item, None, None))
    return out


def _web_fallback_fill_category(
    db: Any, category: Any, image_search_query: str | None = None
) -> int | None:
    """为某栏目联网搜一张横版图入库，返回新图 id；失败/无图返回 None（best-effort，不抛）。

    image_search_query 来自数据库可编辑的搜图关键词模板（image_search scope），None 时 baidu 用默认模板。
    """
    from server.app.modules.image_library.service import store_image_bytes
    from server.app.shared import baidu

    try:
        name = str(getattr(category, "name", "") or "")
        # image_search_query 为 None 时不带该 kwarg，让 baidu 用自身默认模板
        candidates = (
            baidu.search_landscape_images(name)
            if image_search_query is None
            else baidu.search_landscape_images(name, query_template=image_search_query)
        )
        for cand in candidates:
            downloaded = baidu.download_image(cand.url)
            if downloaded is None:
                continue
            data, mime = downloaded
            img = store_image_bytes(
                db,
                category,
                data,
                mime,
                source_url=cand.source_url,
                width=cand.width,
                height=cand.height,
            )
            if img is not None:
                return img.id
    except Exception:
        logger.exception(
            "web_fallback fetch failed for category %s", getattr(category, "name", "?")
        )
    return None


def _store_prefetched_image(
    db: Any, category: Any, queue: list[tuple[bytes, str, Any]]
) -> int | None:
    """从该栏目的内存下载队列取一张落库（store_image_bytes），返回新图 id；队列空 / 落库失败返回 None。

    供 web_fallback 路径用：联网搜图 / 下载已在无连接段先做完（队列里是内存字节），这里只在短 session
    内做 MinIO 上传 + 建 StockImage 记录——本函数绝不触联网搜图 / 下载。
    """
    from server.app.modules.image_library.service import store_image_bytes

    while queue:
        data, mime, cand = queue.pop(0)
        img = store_image_bytes(
            db,
            category,
            data,
            mime,
            source_url=cand.source_url,
            width=cand.width,
            height=cand.height,
        )
        if img is not None:
            return img.id
    return None


def _maybe_insert_images(
    content_json: dict,
    parsed: dict,
    article: Any,
    db: Any,
    *,
    available_categories: list[dict[str, Any]] | None = None,
    web_fallback: bool = False,
    image_search_query: str | None = None,
    max_images: int | None = None,
    prefetched_downloads: dict[int, list[tuple[bytes, str, Any]]] | None = None,
    out_diagnostics: dict[str, Any] | None = None,
) -> tuple[dict, int]:
    """按模型给的 image_positions 插图，返回 (新文档, 实插图数)。

    正文已含图则不动。每个位置优先用候选列表里的 category_id；web_fallback 开时，模型也可用
    game 游戏名点名库里没有的游戏 → get-or-create 陪衬栏目；选中的栏目没图时（含新建的）联网
    搜图补一张。选不到的位置静默跳过。web_fallback 关时行为与改造前一致（game 字段被忽略）。

    max_images 非空时是【硬上限】：取靠前的至多 N 个位置，达到即停止扫描（不再为后续位置
    联网搜图，省调用）。这层兜底独立于提示词文案——即便模型/自定义模板没遵守上限也不会超。
    max_images=None（手动排版/方案配图）保持原行为：不硬截断，全凭模型返回的位置数。

    prefetched_downloads（仅 web_fallback 多段式路径传，按 category_id 分桶的内存下载队列）非空时：
    某栏目无图需补图时，不在此处联网搜图 / 下载（那是慢 IO，已在无连接段做完），而是从队列取一张
    内存字节走 store_image_bytes 落库——保证本函数（在短 session 内）不持连接做联网下载（Task 1b）。
    None（手动排版 / 方案配图等同步路径）保持原行为：缺图时就地 _web_fallback_fill_category 联网补。

    out_diagnostics（非 None 时）：本函数返 0 张图前会写入 `skip_reason` 键，值为
    `already_has_images` / `no_valid_categories` / `ai_returned_no_positions` /
    `no_match_in_categories` 之一，供上层把 "AI 决策为空" 与 "真的 error" 区分上报。
    happy-path（插入 ≥ 1 张）不写入。
    """
    if has_images_in_content(content_json):
        if out_diagnostics is not None:
            out_diagnostics["skip_reason"] = "already_has_images"
        return content_json, 0

    from server.app.modules.image_library.models import StockCategory
    from server.app.modules.image_library.service import get_or_create_companion_category

    cats = (
        available_categories
        if available_categories is not None
        else _available_categories_for_article(article, db)
    )
    valid_category_ids = {cat["id"] for cat in cats}
    if not valid_category_ids and not web_fallback:
        if out_diagnostics is not None:
            out_diagnostics["skip_reason"] = "no_valid_categories"
        return content_json, 0

    positions = _parse_image_positions(parsed.get("image_positions", []))
    if not positions:
        if out_diagnostics is not None:
            out_diagnostics["skip_reason"] = "ai_returned_no_positions"
        return content_json, 0

    matched_refs: list[Any] = []
    matched_positions: list[int] = []
    used_ids: list[int] = []
    for idx, req_cat_id, game in positions:
        if max_images is not None and len(matched_refs) >= max_images:
            break  # 已达硬上限：停止扫描，后续位置不再取图/联网搜图
        category = None  # ORM 对象，仅 web_fallback 取图/新建游戏分支才需要
        target_cat_id: int | None = None
        if req_cat_id is not None and req_cat_id in valid_category_ids:
            target_cat_id = req_cat_id  # 现有栏目：直接用 id，保持改造前行为（不碰 db）
        elif web_fallback and game:
            category = get_or_create_companion_category(db, game)
            target_cat_id = category.id if category is not None else None
        if target_cat_id is None:
            continue

        image_id = pick_image_id(
            ImageQuery(category_ids=[target_cat_id], excluded_ids=used_ids), db
        )
        if image_id is None and web_fallback:
            # 栏目里没图（含刚新建的）：补一张。
            if category is None:
                category = db.get(StockCategory, target_cat_id)
            if category is not None:
                if prefetched_downloads is not None:
                    # 多段式：联网下载已在无连接段做完，这里只从内存队列落库（不触网）
                    image_id = _store_prefetched_image(
                        db, category, prefetched_downloads.get(target_cat_id, [])
                    )
                else:
                    # 同步路径：就地联网搜图 + 下载 + 落库（旧行为，仅非多段式调用方走到）
                    image_id = _web_fallback_fill_category(db, category, image_search_query)
        if image_id is None:
            continue

        ref = fetch_image_by_id(image_id, db)
        if ref is not None:
            used_ids.append(image_id)
            matched_refs.append(ref)
            matched_positions.append(idx)

    if not matched_refs:
        if out_diagnostics is not None:
            out_diagnostics["skip_reason"] = "no_match_in_categories"
        return content_json, 0

    return insert_images_at_positions(content_json, matched_refs, matched_positions), len(
        matched_refs
    )


def _unlock_ai_format(
    db: Any,
    article_id: int,
    lock_started_at: datetime | None,
    *,
    error_message: str | None = None,
) -> None:
    """释放排版锁（清 ai_checking）。仅当锁指纹仍匹配本次才动，避免误清新一轮排版的锁。"""
    from server.app.modules.articles.service import get_article

    article = get_article(db, article_id)
    # 指纹不匹配（已被新一轮排版接管或被超时清锁）：不碰，直接返回
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
    candidate_categories: list[dict[str, Any]] | None = None,
    web_fallback: bool = False,
    max_images: int | None = None,
    min_spacing: int | None = None,
    builtin_variant: str = "conservative",
) -> int:
    """识别正文小标题，并把更新后的 Tiptap 文档写回文章。返回实际插入并落库的图片数。

    candidate_categories 非 None 时用它当配图候选栏目（方案自动配图用全部 bucket）；
    None 时回退到文章已分配的类别（手动 AI 排版按钮的现状行为）。

    web_fallback=True（仅 AI配图 节点开关）时：允许模型点名库里没有的陪衬游戏，
    缺图则联网搜图补充（见 _maybe_insert_images）。默认 False，其它调用方行为不变。

    max_images / min_spacing（仅 AI配图 节点传）覆盖按文章结构推导的默认配图数量/间距，并作为
    插图阶段的硬上限；缺省 None 时维持原行为（按结构推导、不硬截断）。builtin_variant 仅在用内置
    模板时区分保守 / 「积极配图」变体，默认 conservative——手动排版、方案配图均不变。

    返回值仅供调用方观测（如 ai_illustrate 节点回传 images_inserted）；任何跳过/失败均返回 0，
    失败详情照旧落到 article.ai_format_error。多数调用方忽略返回值，行为不变。
    """
    # web_fallback=False（scheme 配图 / 手动排版）：三段式，慢 IO（LLM）期间不持 DB 连接（Task 1a）。
    # web_fallback=True（AI配图 节点）：多段式——慢 IO（LLM + 联网搜图下载）期间均不持连接（Task 1b）。
    if web_fallback:
        return _run_ai_format_web_fallback(
            article_id,
            include_images=include_images,
            lock_started_at=lock_started_at,
            preset_id=preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
            max_images=max_images,
            min_spacing=min_spacing,
            builtin_variant=builtin_variant,
        )

    # 段1（短借连接）：读 + 第一道锁检查 + 拼提示词，随即归还连接
    try:
        prep = _ai_format_prepare(
            article_id,
            lock_started_at=lock_started_at,
            include_images=include_images,
            preset_id=preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
            max_images=max_images,
            min_spacing=min_spacing,
            builtin_variant=builtin_variant,
        )
    except Exception as exc:
        _ai_format_finalize_error(article_id, lock_started_at, exc)
        return 0
    if prep is None:
        return 0

    # 段2（无连接）：调 LLM + 解析 + 应用小标题。此处不得持有任何 DB 连接。
    try:
        response = _call_litellm_completion(
            model=prep.model,
            api_key=prep.api_key,
            messages=[
                {"role": "system", "content": prep.system_prompt},
                {"role": "user", "content": "请按上述要求完成分析，仅返回 JSON。"},
            ],
            timeout_seconds=prep.timeout_seconds,
            api_base=prep.base_url,
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(_extract_json(raw))
        heading_indices = _normalize_heading_indices(
            parsed.get("heading_indices", []), prep.valid_indices
        )
        new_content_json = _apply_headings(prep.content_json, heading_indices)
    except Exception as exc:
        _ai_format_finalize_error(article_id, lock_started_at, exc)
        return 0

    # 段3（短借连接）：配图（快 DB）+ 第二道锁检查 + 写回
    try:
        return _ai_format_write_back(
            article_id,
            lock_started_at=lock_started_at,
            new_content_json=new_content_json,
            parsed=parsed,
            available_categories=prep.available_categories,
            include_images=include_images,
            heading_indices=heading_indices,
            max_images=max_images,
        )
    except Exception as exc:
        _ai_format_finalize_error(article_id, lock_started_at, exc)
        return 0


class _AiFormatPrep:
    """段1 产物：模型调用所需的纯数据（不含 ORM/session），可安全跨段、跨「无连接」窗口传递。

    image_search_query 仅 web_fallback 路径用（联网搜图关键词，来自可编辑模板）；其它路径为 None。
    """

    __slots__ = (
        "content_json",
        "valid_indices",
        "system_prompt",
        "available_categories",
        "model",
        "api_key",
        "base_url",
        "timeout_seconds",
        "image_search_query",
    )

    def __init__(
        self,
        *,
        content_json: dict,
        valid_indices: set[int],
        system_prompt: str,
        available_categories: list[dict[str, Any]],
        model: str,
        api_key: str,
        timeout_seconds: int,
        base_url: str | None = None,
        image_search_query: str | None = None,
    ) -> None:
        self.content_json = content_json
        self.valid_indices = valid_indices
        self.system_prompt = system_prompt
        self.available_categories = available_categories
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.image_search_query = image_search_query


def _ai_format_prepare(
    article_id: int,
    *,
    lock_started_at: datetime | None,
    include_images: bool,
    preset_id: int | None,
    user_id: int | None,
    candidate_categories: list[dict[str, Any]] | None,
    max_images: int | None,
    min_spacing: int | None,
    builtin_variant: str,
    web_fallback: bool = False,
) -> _AiFormatPrep | None:
    """段1（短借连接）：读文章 + 第一道锁检查 + 拼提示词，return 前归还连接。

    返回 None = 本次应安静跳过（无文章 / 锁失配 / 无文本节点；无文本节点时已清本次锁，与旧行为一致）。
    缺 API Key 抛 AIFormatConfigurationError，由调用方走 _ai_format_finalize_error 落错 + 解锁。

    web_fallback=True 时一并解析联网搜图关键词模板（image_search scope）随 prep 带出，
    使后续下载段无需再开连接读模板。
    """
    from server.app.db.session import SessionLocal
    from server.app.modules.articles.service import get_article

    db = SessionLocal()
    try:
        article = get_article(db, article_id)
        if article is None or article.is_deleted:
            return None
        # 第一道锁检查：模型调用前。锁已被接管/超时清掉就别白跑一次昂贵的模型请求
        if not _article_lock_matches(article, lock_started_at):
            logger.info("ai_format skipped stale lock before model call for article %s", article_id)
            return None

        content_json = loads_content_json(article.content_json)
        text_nodes = _non_empty_text_nodes(content_json)
        if not text_nodes:
            logger.info(
                "ai_format skipped article %s: no non-empty paragraph/heading nodes", article_id
            )
            _unlock_ai_format(db, article_id, lock_started_at)
            return None

        # 清 lru_cache 再取：拿运行时最新配置（Key 启动时不校验，运维可能中途改 env）
        get_settings.cache_clear()
        # 格式·配图模型走 DB 注册表（scope=ai_format 默认行）；无行回落 settings.ai_format_*
        from server.app.modules.ai_models.service import resolve_format_engine

        format_model, format_key, format_base_url, format_timeout = resolve_format_engine(db, None)
        api_key = format_key or None
        if not api_key:
            raise AIFormatConfigurationError(
                "AI 排版失败：未配置 API Key，请设置 GEO_AI_FORMAT_API_KEY。"
            )

        if not include_images:
            available_categories: list[dict[str, Any]] = []
        elif candidate_categories is not None:
            available_categories = candidate_categories
        else:
            available_categories = _available_categories_for_article(article, db)
        system_prompt = _load_ai_format_prompt(
            db,
            preset_id=preset_id,
            user_id=user_id,
            include_images=include_images,
            text_nodes=text_nodes,
            available_categories=available_categories,
            web_fallback=web_fallback,
            max_images=max_images,
            min_spacing=min_spacing,
            builtin_variant=builtin_variant,
        )
        # 搜图关键词：优先用数据库可编辑模板（image_search scope），缺省回退内置默认。
        # 陪衬游戏提示词（image_companion）已在 _load_ai_format_prompt 内按可编辑模板拼接。
        image_search_query: str | None = None
        if include_images and web_fallback:
            from server.app.modules.prompt_templates.service import get_active_template_content
            from server.app.shared.baidu import DEFAULT_IMAGE_SEARCH_QUERY

            image_search_query = get_active_template_content(
                db,
                scope="image_search",
                user_id=user_id,
                default=DEFAULT_IMAGE_SEARCH_QUERY,
            )
        return _AiFormatPrep(
            content_json=content_json,
            valid_indices={i for i, _ in text_nodes},
            system_prompt=system_prompt,
            available_categories=available_categories,
            model=format_model,
            api_key=api_key,
            base_url=format_base_url,
            timeout_seconds=format_timeout,
            image_search_query=image_search_query,
        )
    finally:
        db.close()


def _ai_format_write_back(
    article_id: int,
    *,
    lock_started_at: datetime | None,
    new_content_json: dict,
    parsed: dict,
    available_categories: list[dict[str, Any]],
    include_images: bool,
    heading_indices: set[int],
    max_images: int | None,
) -> int:
    """段3（短借连接）：第二道锁检查 + 配图（仅快 DB）+ 写回三份正文 + 清锁，单 session。

    重开 session 后用 get_article 重新取（不能 refresh 段1 的 detached 对象）。第二道锁检查
    失配 = 模型耗时期间被新一轮排版/超时接管，放弃写回、不动锁（非本次所有，与旧行为一致）。
    """
    from server.app.db.session import SessionLocal
    from server.app.modules.articles.service import get_article

    db = SessionLocal()
    try:
        article = get_article(db, article_id)
        if article is None or not _article_lock_matches(article, lock_started_at):
            logger.info("ai_format skipped stale lock before write for article %s", article_id)
            return 0

        image_count = 0
        image_diag: dict[str, Any] = {}
        if include_images:
            new_content_json, image_count = _maybe_insert_images(
                new_content_json,
                parsed,
                article,
                db,
                available_categories=available_categories,
                web_fallback=False,
                image_search_query=None,
                max_images=max_images,
                out_diagnostics=image_diag,
            )

        new_html, new_text = _derive_html_and_text(new_content_json)
        article.content_json = dumps_content_json(new_content_json)
        article.content_html = new_html
        article.plain_text = new_text
        article.version += 1
        article.updated_at = datetime.now(UTC).replace(tzinfo=None)
        # 写回成功的同时清锁（指纹刚校验过仍属本次），单次提交
        article.ai_checking = False
        article.ai_checking_started_at = None
        # include_images=True 但 0 张图落库时,把 _maybe_insert_images 给出的 skip_reason
        # 写到 ai_format_error 列,加 [illustration_skip] 前缀让 ai_illustrate_svc 区分
        # "AI 决策为空" 与 "真的 error",最终透传给 MCP loop 让 writer 把它作为 warning 上报.
        if include_images and image_count == 0 and image_diag.get("skip_reason"):
            skip_reason = image_diag["skip_reason"]
            article.ai_format_error = f"[illustration_skip] {skip_reason}"
            logger.warning(
                "ai_format inserted 0 images for article %s (skip_reason=%s)",
                article_id,
                skip_reason,
            )
        db.commit()
        logger.info(
            "ai_format applied %d headings%s to article %s",
            len(heading_indices),
            f" + {image_count} images" if image_count else "",
            article_id,
        )
        return image_count
    finally:
        db.close()


def _ai_format_finalize_error(
    article_id: int, lock_started_at: datetime | None, exc: BaseException
) -> None:
    """任一段异常的收尾（短借连接）：落 ai_format_error + 解锁（仅当锁指纹仍属本次）。"""
    from server.app.db.session import SessionLocal

    error_message = _describe_ai_format_error(exc)
    logger.exception("ai_format failed for article %s", article_id)
    db = SessionLocal()
    try:
        _unlock_ai_format(db, article_id, lock_started_at, error_message=error_message)
    except Exception:
        db.rollback()
        logger.exception("ai_format unlock failed for article %s", article_id)
    finally:
        db.close()


def _run_ai_format_web_fallback(
    article_id: int,
    *,
    include_images: bool,
    lock_started_at: datetime | None,
    preset_id: int | None,
    user_id: int | None,
    candidate_categories: list[dict[str, Any]] | None,
    max_images: int | None,
    min_spacing: int | None,
    builtin_variant: str,
) -> int:
    """web_fallback=True（AI配图 节点）多段式：慢 IO（LLM + 联网搜图下载）期间都不持 DB 连接（Task 1b）。

    段1（短借）：读 + 第一道锁检查 + 拼提示词（含 image_search 模板）→ close。
    段2（无连接）：调 LLM + 解析 + 应用小标题。
    段3=决策（短借）：按 image_positions 决策每个位置「用现有图 id / 联网补图（带栏目）」，get-or-create
                      陪衬栏目的写也收在这段；max_images 硬上限、used_ids 去重语义与旧 _maybe_insert_images 一致 → close。
    段4=下载（无连接）：把需要联网补图的位置逐个搜图 + 下载到【内存】。
    段5=落库写回（短借）：store_image_bytes 落库 + fetch_image_by_id + 第二道锁检查 + 插图 + 写回三份正文 + 清锁。
    """
    # 段1（短借连接）：读 + 第一道锁检查 + 拼提示词（含联网搜图模板），随即归还连接
    try:
        prep = _ai_format_prepare(
            article_id,
            lock_started_at=lock_started_at,
            include_images=include_images,
            preset_id=preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
            max_images=max_images,
            min_spacing=min_spacing,
            builtin_variant=builtin_variant,
            web_fallback=True,
        )
    except Exception as exc:
        _ai_format_finalize_error(article_id, lock_started_at, exc)
        return 0
    if prep is None:
        return 0

    # 段2（无连接）：调 LLM + 解析 + 应用小标题。此处不得持有任何 DB 连接。
    try:
        response = _call_litellm_completion(
            model=prep.model,
            api_key=prep.api_key,
            messages=[
                {"role": "system", "content": prep.system_prompt},
                {"role": "user", "content": "请按上述要求完成分析，仅返回 JSON。"},
            ],
            timeout_seconds=prep.timeout_seconds,
            api_base=prep.base_url,
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(_extract_json(raw))
        heading_indices = _normalize_heading_indices(
            parsed.get("heading_indices", []), prep.valid_indices
        )
        new_content_json = _apply_headings(prep.content_json, heading_indices)
    except Exception as exc:
        _ai_format_finalize_error(article_id, lock_started_at, exc)
        return 0

    if not include_images:
        # 无配图：等价于 web_fallback=False 的写回（无任何下载）
        try:
            return _ai_format_write_back(
                article_id,
                lock_started_at=lock_started_at,
                new_content_json=new_content_json,
                parsed=parsed,
                available_categories=prep.available_categories,
                include_images=False,
                heading_indices=heading_indices,
                max_images=max_images,
            )
        except Exception as exc:
            _ai_format_finalize_error(article_id, lock_started_at, exc)
            return 0

    # 段3=决策（短借连接）+ 段4=下载（无连接）+ 段5=落库写回（短借连接）
    try:
        return _web_fallback_collect_and_write_back(
            article_id,
            lock_started_at=lock_started_at,
            new_content_json=new_content_json,
            parsed=parsed,
            available_categories=prep.available_categories,
            heading_indices=heading_indices,
            image_search_query=prep.image_search_query,
            max_images=max_images,
        )
    except Exception as exc:
        _ai_format_finalize_error(article_id, lock_started_at, exc)
        return 0


class _WebFallbackPlan:
    """段3 决策产物（纯数据，不含 ORM/session）：插图所需的全部决策，供下载段 + 落库段消费。

    - existing：已选定的现有图 [(node_index, image_id), ...]，落库段直接 fetch 插入。
    - fetches：需联网补图的位置 [(node_index, category_id), ...]，下载段逐个搜图下载到内存。
      category_id 指向决策段已 get-or-create 好（且 committed）的栏目，落库段据它 store。
    """

    __slots__ = ("existing", "fetches")

    def __init__(
        self,
        *,
        existing: list[tuple[int, int]],
        fetches: list[tuple[int, int]],
    ) -> None:
        self.existing = existing
        self.fetches = fetches


def _web_fallback_decide(
    db: Any,
    *,
    content_json: dict,
    parsed: dict,
    available_categories: list[dict[str, Any]],
    max_images: int | None,
) -> _WebFallbackPlan:
    """段3=决策（短借连接）：逐位置决定「用现有图 id / 联网补图（带栏目 id）」。

    与旧 _maybe_insert_images(web_fallback=True) 同语义：现有栏目优先按 id 直接用、其它走 get-or-create
    陪衬栏目；选不到现有图（含刚建的空栏目）则记一笔联网补图请求。max_images 硬上限、used_ids 去重一致。
    决策段会建陪衬栏目（DB 写），但绝不触网；联网搜图 / 下载留到无连接的段4。
    """
    from server.app.modules.image_library.service import get_or_create_companion_category

    valid_category_ids = {cat["id"] for cat in available_categories}
    positions = _parse_image_positions(parsed.get("image_positions", []))

    existing: list[tuple[int, int]] = []
    fetches: list[tuple[int, int]] = []
    used_ids: list[int] = []
    for idx, req_cat_id, game in positions:
        if max_images is not None and (len(existing) + len(fetches)) >= max_images:
            break  # 已达硬上限：停止扫描，后续位置不再取图/联网搜图
        target_cat_id: int | None = None
        if req_cat_id is not None and req_cat_id in valid_category_ids:
            target_cat_id = req_cat_id  # 现有栏目：直接用 id
        elif game:
            category = get_or_create_companion_category(db, game)
            target_cat_id = category.id if category is not None else None
        if target_cat_id is None:
            continue

        image_id = pick_image_id(
            ImageQuery(category_ids=[target_cat_id], excluded_ids=used_ids), db
        )
        if image_id is not None:
            used_ids.append(image_id)
            existing.append((idx, image_id))
        else:
            # 栏目里没图（含刚新建的）：记一笔联网补图请求，下载留到无连接的段4
            fetches.append((idx, target_cat_id))

    return _WebFallbackPlan(existing=existing, fetches=fetches)


def _web_fallback_download(
    fetch_targets: list[tuple[int, int]],
    *,
    category_names: dict[int, str],
    image_search_query: str | None,
) -> dict[int, list[tuple[bytes, str, Any]]]:
    """段4=下载（无连接）：按 category_id 分桶联网搜图 + 下载到【内存】，每个待补图位置补一张。

    入参 fetch_targets=[(node_index, category_id), ...]；category_names 给搜图用的栏目名（决策段已读出）。
    返回 {category_id: [(data, mime, cand), ...]}——FIFO 队列，落库段（_maybe_insert_images）按需 pop。
    搜不到/下载失败的位置静默丢弃（best-effort，与旧行为一致），整段绝不持有 DB 连接。
    """
    from server.app.shared import baidu

    downloaded: dict[int, list[tuple[bytes, str, Any]]] = {}
    for _node_index, category_id in fetch_targets:
        name = category_names.get(category_id, "")
        try:
            candidates = (
                baidu.search_landscape_images(name)
                if image_search_query is None
                else baidu.search_landscape_images(name, query_template=image_search_query)
            )
            for cand in candidates:
                result = baidu.download_image(cand.url)
                if result is None:
                    continue
                data, mime = result
                downloaded.setdefault(category_id, []).append((data, mime, cand))
                break
        except Exception:
            logger.exception("web_fallback fetch failed for category %s", name or category_id)
    return downloaded


def _web_fallback_collect_and_write_back(
    article_id: int,
    *,
    lock_started_at: datetime | None,
    new_content_json: dict,
    parsed: dict,
    available_categories: list[dict[str, Any]],
    heading_indices: set[int],
    image_search_query: str | None,
    max_images: int | None,
) -> int:
    """串起段3（决策，短借）→ 段4（下载，无连接）→ 段5（落库写回，短借）。

    正文已含图则不动（与旧 _maybe_insert_images 一致）。返回实际插入并落库的图片数（images_inserted 语义不变）。

    段5 复用 _maybe_insert_images（插图的唯一权威），但喂入段4 下载好的内存图队列（prefetched_downloads），
    使其落库段只做 MinIO 上传 + 选图 + 插入、绝不联网下载——慢 IO 已全在无连接段消化。
    """
    from server.app.db.session import SessionLocal
    from server.app.modules.articles.service import get_article
    from server.app.modules.image_library.models import StockCategory

    if has_images_in_content(new_content_json):
        # 正文已含图：不配图，等价于无图写回（保持 _maybe_insert_images 的「已含图则不动」语义）
        return _ai_format_write_back(
            article_id,
            lock_started_at=lock_started_at,
            new_content_json=new_content_json,
            parsed=parsed,
            available_categories=available_categories,
            include_images=False,
            heading_indices=heading_indices,
            max_images=max_images,
        )

    # 段3=决策（短借连接）：决定每个位置用现有图还是联网补图；建陪衬栏目；读补图栏目名后归还连接
    db = SessionLocal()
    try:
        plan = _web_fallback_decide(
            db,
            content_json=new_content_json,
            parsed=parsed,
            available_categories=available_categories,
            max_images=max_images,
        )
        category_names: dict[int, str] = {}
        for _idx, cat_id in plan.fetches:
            if cat_id not in category_names:
                cat = db.get(StockCategory, cat_id)
                category_names[cat_id] = str(getattr(cat, "name", "") or "") if cat else ""
    finally:
        db.close()

    # 段4=下载（无连接）：把待补图位置逐个搜图 + 下载到内存队列。此处不得持有任何 DB 连接。
    prefetched = _web_fallback_download(
        plan.fetches,
        category_names=category_names,
        image_search_query=image_search_query,
    )

    # 段5=落库写回（短借连接）：第二道锁检查 + _maybe_insert_images（落库内存图 + 选图 + 插入）+ 写回 + 清锁
    db = SessionLocal()
    try:
        article = get_article(db, article_id)
        if article is None or not _article_lock_matches(article, lock_started_at):
            logger.info("ai_format skipped stale lock before write for article %s", article_id)
            return 0

        image_diag: dict[str, Any] = {}
        new_content_json_final, image_count = _maybe_insert_images(
            new_content_json,
            parsed,
            article,
            db,
            available_categories=available_categories,
            web_fallback=True,
            image_search_query=image_search_query,
            max_images=max_images,
            prefetched_downloads=prefetched,
            out_diagnostics=image_diag,
        )

        new_html, new_text = _derive_html_and_text(new_content_json_final)
        article.content_json = dumps_content_json(new_content_json_final)
        article.content_html = new_html
        article.plain_text = new_text
        article.version += 1
        article.updated_at = datetime.now(UTC).replace(tzinfo=None)
        # 写回成功的同时清锁（指纹刚校验过仍属本次），单次提交
        article.ai_checking = False
        article.ai_checking_started_at = None
        # 与 _ai_format_write_back 对齐:0 张图落库时把 skip_reason 写到 ai_format_error 列,
        # 加 [illustration_skip] 前缀让 ai_illustrate_svc 区分 "AI 决策为空" 与真错.
        if image_count == 0 and image_diag.get("skip_reason"):
            skip_reason = image_diag["skip_reason"]
            article.ai_format_error = f"[illustration_skip] {skip_reason}"
            logger.warning(
                "ai_format inserted 0 images for article %s (skip_reason=%s)",
                article_id,
                skip_reason,
            )
        db.commit()
        logger.info(
            "ai_format applied %d headings%s to article %s",
            len(heading_indices),
            f" + {image_count} images" if image_count else "",
            article_id,
        )
        return image_count
    finally:
        db.close()
