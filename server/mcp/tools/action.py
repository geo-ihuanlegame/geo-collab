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
    return GeoApiClient(base_url=cfg.api_base_url, token=cfg.token, timeout=cfg.timeout_seconds)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def _fail(error: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": error}


# POC 期：调 compose_article 用一个固定 admin user_id 代表 Loop 身份。
# 后续可在 MCP 配置里加 `GEO_MCP_OPERATOR_USER_ID`，这里读环境变量。
_OPERATOR_USER_ID = int(os.environ.get("GEO_MCP_OPERATOR_USER_ID", "1"))


@mcp.tool()
def compose_article(
    question_item_id: int,
    prompt_template_id: int,
    model: str | None = None,
) -> dict[str, Any]:
    """Compose a single article from a question item and a prompt template.

    Bypasses pipeline/scheme orchestration — calls article_writer directly. The article
    is saved with `review_status="pending"` (enters review queue).

    Args:
        question_item_id: From list_question_items.
        prompt_template_id: From list_prompt_templates(scope="generation").
        model: Optional litellm model override; None = use system default writing model.

    Returns:
        {"ok": True, "data": {"article_id": N}, "error": None}
    """
    payload: dict[str, Any] = {
        "question_item_id": question_item_id,
        "prompt_template_id": prompt_template_id,
        "user_id": _OPERATOR_USER_ID,
    }
    if model:
        payload["model"] = model
    try:
        data = _client().post("/api/generation/compose-once", json=payload)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
