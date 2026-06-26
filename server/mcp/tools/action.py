"""写操作 Action 类工具。

compose / illustrate / submit_review / set_review_status / create_distribute / notify

tool 一律声明为 `async def` + 把阻塞 HTTP 调用经 `anyio.to_thread.run_sync` 丢线程池，
理由同 catalog.py 模块 docstring：FastMCP 对同步 tool 在事件循环里 inline 跑，而 self-call
打的是同进程单 worker uvicorn(127.0.0.1:8000)，同步阻塞会把循环锁死 → 自调用死锁到超时。
"""

from __future__ import annotations

import os
from typing import Any

import anyio

from server.mcp.config import get_config
from server.mcp.http_client import ApiError, GeoApiClient
from server.mcp.server import mcp


def _client() -> GeoApiClient:
    cfg = get_config()
    return GeoApiClient(
        base_url=cfg.internal_api_url,
        token=cfg.token,
        timeout=cfg.timeout_seconds,
    )


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def _fail(error: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": error}


async def _apost(path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
    """同步 POST 丢线程池跑，避免阻塞事件循环（见 catalog.py 的自调用死锁说明）。"""

    def _impl() -> dict[str, Any]:
        try:
            return _ok(_client().post(path, json=json))
        except ApiError as exc:
            return _fail(str(exc))

    return await anyio.to_thread.run_sync(_impl)


async def _aget(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """同步 GET 丢线程池跑，避免阻塞事件循环（见 catalog.py 的自调用死锁说明）."""

    def _impl() -> dict[str, Any]:
        try:
            return _ok(_client().get(path, params=params))
        except ApiError as exc:
            return _fail(str(exc))

    return await anyio.to_thread.run_sync(_impl)


# POC 期：调 compose_article 用一个固定 admin user_id 代表 Loop 身份。
# 后续可在 MCP 配置里加 `GEO_MCP_OPERATOR_USER_ID`，这里读环境变量。
_OPERATOR_USER_ID = int(os.environ.get("GEO_MCP_OPERATOR_USER_ID", "1"))


@mcp.tool()
async def save_article(
    question_item_id: int,
    prompt_template_id: int,
    title: str,
    markdown_content: str,
    model_label: str | None = None,
) -> dict[str, Any]:
    """Save a Claude Code-generated article (markdown) into GEO.

    This is the **zero-config generation path**: you (the calling Claude Code conversation)
    write the article markdown yourself, then call this tool to persist it. No GEO-side
    LLM call is made—so the host does NOT need GEO_AI_API_KEY configured.

    Workflow for the generation loop:
        1. Call list_question_items / list_prompt_templates to pick a question + template.
        2. Compose the article markdown yourself in this conversation.
        3. Call save_article(question_item_id, prompt_template_id, title, markdown_content).
        4. Then illustrate_article + submit_review_decision as usual.

    Args:
        question_item_id: From list_question_items.
        prompt_template_id: From list_prompt_templates(scope="generation"). The template
            content is what guided your writing—pass its id for traceability.
        title: Article title (1–300 chars). Pass explicitly; do NOT also put a leading
            `# Title` heading in markdown_content (the body should start from the first
            paragraph).
        markdown_content: Full article body in Markdown. Use ## / ### for sub-headings,
            standard MD for lists / bold / etc. Converted to Tiptap JSON + HTML on save.
        model_label: Optional identifier of the writer (e.g. "claude-opus-4-7"). Stored
            in article.metrics['writer_model'] for later analytics.

    Returns:
        {"ok": True, "data": {"article_id": N}, "error": None}
    """
    payload: dict[str, Any] = {
        "question_item_id": question_item_id,
        "prompt_template_id": prompt_template_id,
        "user_id": _OPERATOR_USER_ID,
        "title": title,
        "markdown_content": markdown_content,
    }
    if model_label:
        payload["model_label"] = model_label
    return await _apost("/api/articles/save-from-mcp", json=payload)


@mcp.tool()
async def illustrate_article(
    article_id: int,
    category_ids: list[int] | None = None,
    image_positions: list[int] | None = None,
) -> dict[str, Any]:
    """Insert AI-selected stock images into article body.

    Args:
        article_id: Target article (must exist).
        category_ids: Image library categories to draw from. None = use article's existing tags.
        image_positions: Insertion indices in content array. None = auto [2, 4, 6].
    """
    body: dict[str, Any] = {}
    if category_ids:
        body["category_ids"] = category_ids
    if image_positions:
        body["image_positions"] = image_positions
    return await _apost(f"/api/articles/{article_id}/illustrate", json=body)


@mcp.tool()
async def submit_review_decision(
    article_id: int,
    decision: str,
    score_total: int | None = None,
    score_breakdown: dict[str, int] | None = None,
    reasoning: str | None = None,
    decided_by: str = "claude-code-loop",
) -> dict[str, Any]:
    """Record an auto-review decision for an article.

    Note: this does NOT change `article.review_status` — final human review is still authoritative.
    The decision is persisted for audit / training data.

    Args:
        article_id: Target article.
        decision: One of "approved" / "needs_rewrite" / "rejected".
        score_total: 0-100 weighted score, optional.
        score_breakdown: dict[dimension_key, score_0_100], optional.
        reasoning: 1-2 sentence explanation, optional.
        decided_by: Identifier for the deciding agent (default "claude-code-loop").
    """
    if decision not in ("approved", "needs_rewrite", "rejected"):
        return _fail(f"invalid decision: {decision}")
    body: dict[str, Any] = {"decision": decision, "decided_by": decided_by}
    if score_total is not None:
        body["score_total"] = score_total
    if score_breakdown is not None:
        body["score_breakdown"] = score_breakdown
    if reasoning:
        body["reasoning"] = reasoning
    return await _apost(f"/api/articles/{article_id}/auto-review", json=body)


@mcp.tool()
async def notify_feishu(
    title: str,
    message: str,
    level: str = "info",
) -> dict[str, Any]:
    """Send a Feishu webhook notification.

    Args:
        title: Short header line (e.g. "Loop 完成").
        message: Body text (multi-line OK).
        level: "info" | "warning" | "error" | "done" — controls emoji prefix.
    """
    if level not in ("info", "warning", "error", "done"):
        return _fail(f"invalid level: {level}")
    return await _apost(
        "/api/system/feishu-notify",
        json={"title": title, "message": message, "level": level},
    )


@mcp.tool()
async def set_review_status(article_id: int, review_status: str) -> dict[str, Any]:
    """Update an article's review_status.

    Args:
        article_id: Target article id.
        review_status: "pending" (enter review queue) or "approved" (move to approved library).

    Note: This uses a dedicated MCP-only endpoint that doesn't require user JWT.
    """
    if review_status not in ("pending", "approved"):
        return _fail(f"invalid review_status: {review_status}")
    return await _apost(
        f"/api/articles/{article_id}/set-review-status",
        json={"review_status": review_status},
    )


@mcp.tool()
async def create_distribute_task(
    name: str,
    article_ids: list[int],
    account_ids: list[int],
    platform_code: str = "toutiao",
    stop_before_publish: bool = False,
) -> dict[str, Any]:
    """Create an article_round_robin distribute task.

    Args:
        name: Human-readable task name (e.g. "Daily distribute 2026-06-18").
        article_ids: Articles to distribute (must be review_status="approved" already).
        account_ids: Target accounts. Round-robin maps article->account by sort_order.
        platform_code: "toutiao" / "wechat_mp" etc. Default "toutiao".
        stop_before_publish: If True, task pauses before actual publish (manual confirm needed).
    """
    body = {
        "name": name,
        "article_ids": article_ids,
        "account_ids": account_ids,
        "platform_code": platform_code,
        "user_id": _OPERATOR_USER_ID,
        "stop_before_publish": stop_before_publish,
    }
    return await _apost("/api/tasks/mcp", json=body)


@mcp.tool()
async def install_loop_skills() -> dict[str, Any]:
    """Fetch the /goal Loop skill bundle so Claude Code can install it locally.

    Returns a dict containing all 5 template files (README, slash command, 3 SKILL.md).
    The calling Claude Code session should then use its Write tool to write each
    file to the user's `.claude/` directory.

    Use this when the user asks something like "install geo loop skills" or
    "set me up to use /goal". Before writing files, check whether the user has
    a local `.claude/` directory (project-level or `~/.claude/`) and ask
    which they prefer.

    Returns:
        {"ok": True, "data": {
            "version": str,                # e.g. "2026-06-24-v1"
            "bundle_sha256": str,
            "install_hint": str,           # plain-English placement guidance
            "files": [
                {"path": str, "content": str, "sha256": str, "size": int},
                ...
            ],
        }, "error": None}
    """
    # 后端 /install-payload 已经返回了完整 {ok, data, error} 结构，这里直接透传.
    # _aget 默认会把 GeoApiClient.get 的返回值再 wrap 一层 _ok()，因此
    # 实际拿到的是 {"ok": True, "data": {"ok": True, "data": {...}, "error": None}, "error": None}.
    # 把内层剥出来，让 LLM 看到的契约干净.
    raw = await _aget("/api/mcp/loop-skill-bundle/install-payload")
    if not raw.get("ok"):
        return raw  # 透传 _fail 结构
    inner = raw.get("data") or {}
    if isinstance(inner, dict) and "ok" in inner and "data" in inner:
        return inner  # 后端已经返了 {ok, data, error}
    return raw


@mcp.tool()
async def ai_illustrate_article(
    article_id: int,
    main_category_id: int,
    include_companion: bool = True,
    aggressive_images: bool = True,
    set_cover: bool = True,
    web_fallback: bool = False,
) -> dict[str, Any]:
    """AI-driven illustration + auto cover for one article (Web UI parity).

    Uses GEO's run_ai_format under the hood — the AI model picks which images
    to insert and where, based on article content. Draws images from
    main_category_id + (optionally) all companion categories. Auto-sets cover
    from main_category_id if article has no cover.

    Args:
        article_id: Target article (must exist).
        main_category_id: Stock image library category id ("主推栏目"). The matrix
            section in your writer SKILL.md tells you which id to use.
        include_companion: If True, also draws from all companion categories.
            Default True matches Web UI default.
        aggressive_images: If True, "积极" style (more images, less spacing).
            Default True matches Web UI default.
        set_cover: If True, also picks a random image from main_category_id
            as the article cover (only if cover not already set).
        web_fallback: 联网兜底开关(对齐 Web UI「AI 配图」节点的同名开关)。
            默认 False。开启后,当正文点到的游戏在本地图库【没有】对应栏目、
            或匹配到的栏目【没有图】时,AI 可以用游戏名点名,GEO 会自动建一个
            陪衬栏目 + 走百度(千帆 AI 搜索)联网搜一张横版图补进去——这样图库
            里没有的新游戏也能配上图。**前提**:app 容器配了 GEO_BAIDU_API_KEY;
            best-effort:key 缺失 / 网络失败时静默不补图、不报错(退化为关时行为)。
            想让"图库无图也走百度补图"就传 web_fallback=True。

    Returns:
        {"ok": True, "data": {
            "images_inserted": int,
            "cover_status": str,        # "set" | "skipped_existing" | "no_image" | "error" | "skipped"
            "cover_error": str | None,
            "format_error": str | None,
            "warning": str | None,      # AI decided not to insert / no matching image / etc.
                                        # 0 images + warning != error: writer MUST still surface this
                                        # as an illustration_warnings entry so 0-image articles do
                                        # not enter the review pool silently.
            "requested": int,           # AI 点名且能定位到栏目的位置数（"应该配上图"的张数）
            "missed": int,              # requested 里最终没配上的张数（含联网也没补到）
            "missed_games": list[str],  # 没配上的游戏名/栏目（定位是哪几款没图）
        }, "error": None}

    Caller contract (writer skill):
        以下任一成立 → 加进最终 JSON 的 illustration_warnings 数组,让 orchestrator / 飞书可见:
          - format_error / cover_error / warning 任一非空
          - images_inserted == 0
          - missed > 0（部分配图失败:该配 requested 张、只来 images_inserted 张。
            即便 warning 已含 partial_images 文案,missed 是更结构化的判定依据——
            别因为 images_inserted 非 0 就当完全成功）
        不要把 warning / 部分 miss 当 error 抛——文章本身已落库可用,只是图不全.
    """
    body: dict[str, Any] = {
        "main_category_id": main_category_id,
        "include_companion": include_companion,
        "aggressive_images": aggressive_images,
        "set_cover": set_cover,
        "web_fallback": web_fallback,
    }
    return await _apost(f"/api/articles/{article_id}/ai-illustrate", json=body)
