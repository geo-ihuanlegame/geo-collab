"""MCP token 鉴权依赖与共享 helper。

独立于 user JWT 的 service token:
- 空配置 (`GEO_MCP_TOKEN=""`) 视作"MCP 已禁用",任何带 token 的请求都返回 401。
- 配置非空时,校验请求 header `X-MCP-Token` 是否匹配。
- 使用 `hmac.compare_digest` 做常数时间比较,避免 timing attack。

两个入口共享同一 `verify_mcp_token` helper:
- `require_mcp_token`: FastAPI Depends, 给 sub-router (auto_review_router 等) 用
- `McpTokenMiddleware`: starlette BaseHTTPMiddleware, 给 mount 的 sub-app (/mcp) 用
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from server.app.core.config import get_settings


def verify_mcp_token(sent: str | None) -> tuple[bool, str]:
    """检查 sent token 是否匹配 GEO_MCP_TOKEN。

    返回 (ok, error_detail)。token 未配置时 ok=False, detail="MCP token not configured"。
    空 sent / 不匹配时 ok=False, detail="invalid MCP token"。
    匹配时 ok=True, detail=""。
    """
    configured = get_settings().mcp_token or ""
    if not configured:
        return False, "MCP token not configured"
    if not sent or not hmac.compare_digest(sent, configured):
        return False, "invalid MCP token"
    return True, ""


def require_mcp_token(
    x_mcp_token: str | None = Header(default=None, alias="X-MCP-Token"),
) -> None:
    """FastAPI Depends: 校验 MCP token header。

    用法 (在 router 上挂依赖):
        app.include_router(
            auto_review_router,
            prefix="/api/articles",
            dependencies=[Depends(require_mcp_token)],
        )
    """
    ok, detail = verify_mcp_token(x_mcp_token)
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class McpTokenMiddleware(BaseHTTPMiddleware):
    """检 X-MCP-Token header,失败直接 401,不进入下游 ASGI app。

    用在 mount 的 sub-app 上(starlette ASGI 中间件),给 FastMCP 的 streamable HTTP app 套鉴权。
    与 require_mcp_token(FastAPI Depends) 共享 verify_mcp_token helper。
    """

    async def dispatch(self, request: Request, call_next):
        sent = request.headers.get("X-MCP-Token", "")
        ok, detail = verify_mcp_token(sent)
        if not ok:
            return JSONResponse({"detail": detail}, status_code=401)
        return await call_next(request)
