"""只读 Catalog 类工具。

每个 tool 走 `@mcp.tool` 装饰，签名直接做 LLM-facing schema:
- 参数有默认值则在 LLM prompt 里可省
- 返回 dict 顶层 `{ok, data, error}` —— 失败时 data=None, error=str
"""

from __future__ import annotations

from typing import Any

from server.mcp.config import get_config
from server.mcp.http_client import ApiError, GeoApiClient
from server.mcp.server import mcp


def _client() -> GeoApiClient:
    cfg = get_config()
    return GeoApiClient(base_url=cfg.api_base_url, token=cfg.token, timeout=cfg.timeout_seconds)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def _fail(error: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": error}


@mcp.tool()
def list_articles(
    status: str | None = None,
    review_status: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List GEO articles with filters.

    Args:
        status: Article workflow status. Common values: "draft", "ready".
        review_status: Editorial review status. Values: "pending", "approved".
        limit: Max number of articles to return (1-100).

    Returns:
        {"ok": True, "data": {"items": [...], "total": N}, "error": None} on success.
        {"ok": False, "data": None, "error": "<message>"} on failure.
    """
    params: dict[str, Any] = {"limit": max(1, min(100, limit))}
    if status:
        params["status"] = status
    if review_status:
        params["review_status"] = review_status
    try:
        data = _client().get("/api/articles", params=params)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def list_question_pools() -> dict[str, Any]:
    """List all question pools (Feishu-synced topic libraries)."""
    try:
        data = _client().get("/api/generation/question-pools")
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def list_question_items(
    pool_id: int,
    limit: int = 20,
    category: str | None = None,
) -> dict[str, Any]:
    """List question items within a pool, optionally filtered by category.

    Args:
        pool_id: Question pool id (from list_question_pools).
        limit: Max items to return (1-100).
        category: Optional category filter (e.g. "未分类" / specific category name).
    """
    params: dict[str, Any] = {"limit": max(1, min(100, limit))}
    if category:
        params["category"] = category
    try:
        data = _client().get(f"/api/generation/question-pools/{pool_id}/items", params=params)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def list_prompt_templates(scope: str = "generation") -> dict[str, Any]:
    """List prompt templates filtered by scope.

    Args:
        scope: One of "generation", "ai_format", "image_search", "image_companion".
               "generation" = article writing prompts (most common for Loops).
    """
    try:
        data = _client().get("/api/prompt-templates", params={"scope": scope})
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def list_pipelines(type_filter: str | None = None) -> dict[str, Any]:
    """List all pipelines (智能体 / workflows).

    Args:
        type_filter: Optional pipeline type filter (e.g. "agent" / "workflow").
    """
    params: dict[str, Any] = {}
    if type_filter:
        params["type"] = type_filter
    try:
        data = _client().get("/api/pipelines", params=params or None)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def list_accounts(
    platform_code: str | None = None,
    distribution_enabled: bool | None = None,
) -> dict[str, Any]:
    """List publishing accounts.

    Args:
        platform_code: Filter by platform (e.g. "toutiao", "wechat_mp").
        distribution_enabled: If true, only accounts available for distribution.
    """
    params: dict[str, Any] = {}
    if platform_code:
        params["platform_code"] = platform_code
    if distribution_enabled is not None:
        params["distribution_enabled"] = str(distribution_enabled).lower()
    try:
        data = _client().get("/api/accounts", params=params or None)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def get_article(article_id: int) -> dict[str, Any]:
    """Get one article by id, including full content_json / content_html / plain_text."""
    try:
        data = _client().get(f"/api/articles/{article_id}")
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
