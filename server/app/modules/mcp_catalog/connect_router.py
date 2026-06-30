"""MCP 接入指引相关端点。

两组路由分开挂：
- mcp_connect_user_router：/api/mcp/status，user JWT 鉴权（依赖在 main.py include 时通过
  prefix 链路自然继承，与系统 user JWT 路由一致）。
- mcp_connect_health_router：/api/mcp/health，MCP token 鉴权（router-level dependency）。

为什么分两个 router？两条不同的鉴权边界——不能在同一个 router 上同时挂
`Depends(get_current_user)` 和 `Depends(require_mcp_token)`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from server.app.core.config import get_settings
from server.app.core.mcp_auth import require_mcp_token

# 与 server/mcp/tools/ 下三个文件 (action.py / catalog.py / meta.py) 注册的 @mcp.tool
# 数量同步。增减 MCP tool 时改这里，前端「MCP 接入」tab 段 ① 用此数字。
MCP_TOOLS_COUNT = 21


class McpStatusResponse(BaseModel):
    configured: bool
    suggested_base_url: str
    tools_count: int


class McpToolInfo(BaseModel):
    name: str
    group: str  # catalog / action / meta —— 由注册函数所在模块推出
    summary: str  # 工具 docstring 首行（英文），前端中文 gloss 缺失时回落它


class McpHealthResponse(BaseModel):
    ok: bool


# user JWT 鉴权（依赖在 main.py include_router 时注入）
mcp_connect_user_router = APIRouter()


@mcp_connect_user_router.get("/status", response_model=McpStatusResponse)
def get_mcp_status(request: Request) -> McpStatusResponse:
    """[user] 返回 MCP 接入状态：是否配置 token、推荐 base_url、工具数量。"""
    settings = get_settings()
    return McpStatusResponse(
        configured=bool(settings.mcp_token),
        suggested_base_url=str(request.base_url).rstrip("/"),
        tools_count=MCP_TOOLS_COUNT,
    )


@mcp_connect_user_router.get("/tools", response_model=list[McpToolInfo])
def list_mcp_tools() -> list[McpToolInfo]:
    """[user] 列出当前已注册的 MCP 工具（名字 / 分组 / 首行摘要）。

    直接内省 FastMCP 活注册表（与 server/mcp/server.py 里 tools_count 断言读的
    `mcp._tool_manager._tools` 是同一来源），保证前端「MCP 接入」tab 右侧展示的
    工具列表永远等于实际注册的工具，绝不与段 ① 的 tools_count 数字漂移。
    """
    # 懒导入：server.mcp.server 顶部 import 了本模块的 MCP_TOOLS_COUNT，
    # 顶层 import 会形成循环依赖，故放函数内。
    from server.mcp.server import mcp

    infos: list[McpToolInfo] = []
    for name, tool in mcp._tool_manager._tools.items():
        module = getattr(tool.fn, "__module__", "") or ""
        group = module.rsplit(".", 1)[-1]  # server.mcp.tools.catalog -> "catalog"
        summary = ""
        for line in (tool.description or "").splitlines():
            if line.strip():
                summary = line.strip()
                break
        infos.append(McpToolInfo(name=name, group=group, summary=summary))
    infos.sort(key=lambda i: (i.group, i.name))
    return infos


# MCP token 鉴权（router-level dependency）
mcp_connect_health_router = APIRouter(dependencies=[Depends(require_mcp_token)])


@mcp_connect_health_router.get("/health", response_model=McpHealthResponse)
def get_mcp_health() -> McpHealthResponse:
    """[MCP] 健康探针：仅用于 Claude Code Loop 启动时校验 token + base_url 联通。"""
    return McpHealthResponse(ok=True)
