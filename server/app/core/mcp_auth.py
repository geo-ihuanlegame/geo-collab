"""MCP token 鉴权依赖。

独立于 user JWT 的 service token：
- 空配置 (`GEO_MCP_TOKEN=""`) 视作"MCP 已禁用"，任何带 token 的请求都返回 401。
- 配置非空时，校验请求 header `X-MCP-Token` 是否匹配。
- 使用 `hmac.compare_digest` 做常数时间比较，避免 timing attack。
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from server.app.core.config import get_settings


def require_mcp_token(
    x_mcp_token: str | None = Header(default=None, alias="X-MCP-Token"),
) -> None:
    """FastAPI Depends：校验 MCP token header。

    用法（在 router 上挂依赖）：
        app.include_router(
            auto_review_router,
            prefix="/api/articles",
            dependencies=[Depends(require_mcp_token)],
        )
    """
    configured = get_settings().mcp_token or ""
    if not configured:
        # 空配置 = MCP 禁用，所有请求一律拒绝
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MCP token not configured",
        )
    if not x_mcp_token or not hmac.compare_digest(x_mcp_token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid MCP token",
        )
