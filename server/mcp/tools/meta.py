"""评估 / 反馈回流类工具。"""

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
