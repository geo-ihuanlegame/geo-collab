"""只读 Catalog 类工具。

每个 tool 走 `@mcp.tool` 装饰，签名直接做 LLM-facing schema:
- 参数有默认值则在 LLM prompt 里可省
- 返回 dict 顶层 `{ok, data, error}` —— 失败时 data=None, error=str

tool 一律声明为 `async def`：FastMCP 对同步 tool 是在事件循环里 inline 跑的
(func_metadata.call_fn_with_arg_validation 的同步分支 `return fn(...)`)。HTTP-mount 下
self-call 打的是同进程同一个单 worker uvicorn(127.0.0.1:8000)，同步阻塞会把事件循环
锁死、self-call 永远等不到处理 → 死锁直到超时。改 async + 把阻塞 HTTP 调用经
`anyio.to_thread.run_sync` 丢线程池，事件循环就空出来服务 self-call。详见 _aget。
"""

from __future__ import annotations

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


async def _aget(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """同步 GET 丢线程池跑，避免阻塞事件循环（见模块 docstring 的自调用死锁说明）。"""

    def _impl() -> dict[str, Any]:
        try:
            return _ok(_client().get(path, params=params))
        except ApiError as exc:
            return _fail(str(exc))

    return await anyio.to_thread.run_sync(_impl)


@mcp.tool()
async def list_articles(
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
    return await _aget("/api/mcp/articles", params=params)


@mcp.tool()
async def list_question_pools() -> dict[str, Any]:
    """List all question pools (Feishu-synced topic libraries)."""
    return await _aget("/api/mcp/question-pools")


@mcp.tool()
async def list_question_items(
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
    return await _aget(f"/api/mcp/question-pools/{pool_id}/items", params=params)


@mcp.tool()
async def list_prompt_templates(scope: str = "generation") -> dict[str, Any]:
    """List prompt templates filtered by scope.

    Args:
        scope: One of "generation", "ai_format", "image_search", "image_companion".
               "generation" = article writing prompts (most common for Loops).
    """
    return await _aget("/api/mcp/prompt-templates", params={"scope": scope})


@mcp.tool()
async def list_pipelines(type_filter: str | None = None) -> dict[str, Any]:
    """List all pipelines (智能体 / workflows).

    Args:
        type_filter: Optional pipeline type filter (e.g. "agent" / "workflow").
    """
    params: dict[str, Any] = {}
    if type_filter:
        params["type"] = type_filter
    return await _aget("/api/mcp/pipelines", params=params or None)


@mcp.tool()
async def list_accounts(
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
    return await _aget("/api/mcp/accounts", params=params or None)


@mcp.tool()
async def get_article(article_id: int) -> dict[str, Any]:
    """Get one article by id, including full content_json / content_html / plain_text."""
    return await _aget(f"/api/mcp/articles/{article_id}")


@mcp.tool()
async def list_today_loop_articles(
    decided_by: str = "claude-goal-verifier",
    decision: str = "approved",
    since_hours: int = 24,
    model_label: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Count + list articles that the /goal loop wrote and verifier decided on,
    within a rolling time window.

    Used by the /goal orchestrator as the source-of-truth stop condition,
    independent of the writer subagent's self-report.

    Args:
        decided_by: AutoReviewDecision.decided_by filter. Default
            "claude-goal-verifier" matches the verifier skill convention.
        decision: AutoReviewDecision.decision filter. Default "approved".
        since_hours: Window length in hours. Default 24, cap 168 (1 week).
        model_label: Optional. If supplied, also filter
            Article.metrics.writer_model == model_label.
        limit: Max items in returned list. Default 50, cap 200.

    Returns:
        {"ok": True, "data": {"count": int, "items": [...]}, "error": None}
        on success. items: [{article_id, title, decided_at, score_total}].
    """
    params: dict[str, Any] = {
        "decided_by": decided_by,
        "decision": decision,
        "since_hours": max(1, min(168, since_hours)),
        "limit": max(1, min(200, limit)),
    }
    if model_label:
        params["model_label"] = model_label
    return await _aget("/api/articles/today-loop-decisions", params=params)
