"""写操作 Action 类工具。

compose / illustrate / submit_review / set_review_status / create_distribute / notify
"""

from __future__ import annotations

import os
from typing import Any

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


# POC 期：调 compose_article 用一个固定 admin user_id 代表 Loop 身份。
# 后续可在 MCP 配置里加 `GEO_MCP_OPERATOR_USER_ID`，这里读环境变量。
_OPERATOR_USER_ID = int(os.environ.get("GEO_MCP_OPERATOR_USER_ID", "1"))


@mcp.tool()
def save_article(
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
    try:
        data = _client().post("/api/articles/save-from-mcp", json=payload)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def illustrate_article(
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
    try:
        data = _client().post(f"/api/articles/{article_id}/illustrate", json=body)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def submit_review_decision(
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
    try:
        data = _client().post(f"/api/articles/{article_id}/auto-review", json=body)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def notify_feishu(
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
    try:
        data = _client().post(
            "/api/system/feishu-notify",
            json={"title": title, "message": message, "level": level},
        )
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def set_review_status(article_id: int, review_status: str) -> dict[str, Any]:
    """Update an article's review_status.

    Args:
        article_id: Target article id.
        review_status: "pending" (enter review queue) or "approved" (move to approved library).

    Note: This uses a dedicated MCP-only endpoint that doesn't require user JWT.
    """
    if review_status not in ("pending", "approved"):
        return _fail(f"invalid review_status: {review_status}")
    try:
        data = _client().post(
            f"/api/articles/{article_id}/set-review-status",
            json={"review_status": review_status},
        )
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def create_distribute_task(
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
    try:
        data = _client().post("/api/tasks/mcp", json=body)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
