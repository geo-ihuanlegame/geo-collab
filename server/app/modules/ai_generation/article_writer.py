"""方案流生文内核：与会话、方案、LangGraph 解耦的可复用单元。

未来自动化工作流可把"生成一篇文章"当作一个节点直接复用本函数——它只依赖
（模板内容 + 问题文本 + 用户 ID + 会话工厂），不感知方案、运行或会话。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

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


# 取 ```json 围栏块；仅当解析为含 list 型 "games" 的 dict（=我们的哨兵）才认。
# 注：与 ai_format.py:_extract_json 正则近似，但本函数要 span（剥块）+ 最后一个 +
# games 键校验，契约不同，故另写小函数、不跨模块耦合那个私有 helper。
_GAMES_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _split_games_block(md_content: str) -> tuple[str, list[str]]:
    """从模型输出尾部抽 {"games":[...]} 哨兵块 → (剥离后的正文, 游戏名列表)。

    - 仅识别最后一个 ```json 围栏；解析为 dict 且含 list 型 "games" 才认（剥块 + 取名）。
    - 非哨兵 / 坏 JSON / 无块 → 原样返回正文 + []（零回归，绝不误吃正文里的代码块）。
    """
    text = md_content or ""
    matches = list(_GAMES_FENCE_RE.finditer(text))
    if not matches:
        return text, []
    m = matches[-1]
    try:
        obj = json.loads(m.group(1))
    except (ValueError, TypeError):
        return text, []
    if not isinstance(obj, dict) or not isinstance(obj.get("games"), list):
        return text, []
    games = [str(g).strip() for g in obj["games"] if str(g).strip()]
    body = (text[: m.start()] + text[m.end() :]).strip()
    return body, games


def generate_article_from_prompt(
    *,
    session_factory: Callable[[], Any],
    user_id: int,
    template_content: str,
    question_text: str,
    model: str | None = None,
    source_agent_name: str | None = None,
    source_template_name: str | None = None,
    web_search: bool = False,
    deep_thinking: bool = False,
) -> int:
    """组装提示词 → 调用 LLM → 取标题 → 转 Tiptap/HTML → create_article。返回 article_id。

    通用系统提示词（不拼 Skill）。`model` 为方案级 AI 引擎覆盖（None / 空 = 用 settings.ai_model）。
    `source_agent_name` / `source_template_name` 为生文溯源（智能体名 / 模板名），仅落库供列表展示。
    `web_search` / `deep_thinking` 为「模型能力」开关（默认关；ai_compose 节点按 config 开启），
    经 model_capabilities 按 provider 映射到 litellm 调用，best-effort、不支持/失败即回退普通生文。
    异常向上抛（由调用方记任务失败）。每次调用自带独立会话。
    """
    import litellm

    from server.app.modules.ai_generation.converter import markdown_to_html, markdown_to_tiptap
    from server.app.modules.ai_generation.model_capabilities import completion_with_capabilities
    from server.app.modules.ai_models.service import resolve_writing_engine
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    # 模型解析用短生命周期 session（DB 行实时读），绝不跨后面的 litellm 调用持有连接
    _resolve_db = session_factory()
    try:
        model_str, api_key, base_url = resolve_writing_engine(_resolve_db, model)
    finally:
        _resolve_db.close()

    user_prompt = (
        render_question_prompt(template_content, question_text)
        + "\n\n请开始写作。第一行必须是 `# 标题`（井号后留一个空格）。"
        "正文写完后，**若本文是“每款游戏各占一个小标题”的盘点 / 推荐类文章**，"
        "在正文之后另起一行追加一个 json 代码块，按小标题顺序列出每个小标题对应的"
        "规范游戏中文名（与小标题一致即可，带不带《》/“游戏N、”前缀都行，后端会归一化匹配）：\n"
        '```json\n{"games": ["原神", "明日方舟"]}\n```\n'
        '若是没有分款小标题的散文 / 综述，则追加 `{"games": []}`。'
        "该 json 块只用于自动配图、不展示给读者；除此之外不要输出任何前言、解释或额外代码块。"
    )
    response = completion_with_capabilities(
        completion=litellm.completion,
        base_kwargs={
            "model": model_str,
            "messages": [
                {"role": "system", "content": _GENERIC_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "api_key": api_key or None,
            "api_base": base_url or None,
            "timeout": 300,
            "max_tokens": 12000,
        },
        model=model_str,
        web_search=web_search,
        deep_thinking=deep_thinking,
        logger=logger,
    )
    md_content = response.choices[0].message.content or ""

    # 先剥掉模型尾部的 {"games":[...]} 哨兵块，再抽标题正文（否则 json 块会泄进正文）
    article_body, game_names = _split_games_block(md_content)
    game_list = [{"game": g} for g in game_names] or None

    title, body_md = extract_title_and_body(article_body)

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
        # 生文溯源：去规范化存名字，供未审核库 / 列表卡片直接展示
        article.source_agent_name = source_agent_name
        article.source_template_name = source_template_name
        # 显式游戏清单：盘点 / 推荐文写完顺手吐的 game_list 盖进 metrics，供配图侧走确定性落图
        # （散文 / 无块 → game_list None → 不盖 → 消费侧回退现有 run_ai_format，零回归）
        if game_list:
            article.metrics = {**(article.metrics or {}), "game_positions": game_list}
        db.commit()
        return article.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
