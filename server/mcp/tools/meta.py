"""评估 / 反馈回流类工具。"""

from __future__ import annotations

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


@mcp.tool()
def score_recent_articles(
    article_ids: list[int],
    dimensions: list[str] | None = None,
) -> dict[str, Any]:
    """LLM-score one or more articles using GEO's ai_format model.

    Args:
        article_ids: Up to 20 article ids per call.
        dimensions: Score dimensions. None = ["factuality", "readability", "style", "policy_safety"].

    Returns:
        results: list of {article_id, score_total, score_breakdown, suggested_decision, reasoning}
    """
    body: dict[str, Any] = {"article_ids": article_ids}
    if dimensions:
        body["dimensions"] = dimensions
    try:
        data = _client().post("/api/articles/score", json=body)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def get_template_performance(
    template_id: int,
    window_days: int = 7,
) -> dict[str, Any]:
    """Aggregate performance for a prompt template's output articles.

    Returns: {template_id, window_days, article_count, avg_views, avg_likes, approval_rate}
    """
    try:
        data = _client().get(
            f"/api/prompt-templates/{template_id}/performance",
            params={"window_days": window_days},
        )
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def get_account_performance(
    account_id: int,
    window_days: int = 7,
) -> dict[str, Any]:
    """Aggregate performance for an account's published articles.

    Returns: {account_id, window_days, publish_count, with_metrics_count, avg_views, avg_likes}
    """
    try:
        data = _client().get(
            f"/api/accounts/{account_id}/performance",
            params={"window_days": window_days},
        )
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def record_publish_metrics(
    record_id: int,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """Record post-publish metrics (views/likes/comments/shares) for a publish record.

    Args:
        record_id: PublishRecord id (from list_articles → check publish history; or platform API).
        metrics: Dict, typically {"views": int, "likes": int, "comments": int, "shares": int}.
                 Merges into the article's metrics JSON column.
    """
    try:
        data = _client().post(
            f"/api/publish-records/{record_id}/metrics",
            json={"metrics": metrics},
        )
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
