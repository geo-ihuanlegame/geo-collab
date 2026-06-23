"""GEO MCP Server — FastMCP 入口。

两条 transport 路径:

1. **HTTP** (推荐, 用户端零本地依赖): GEO `create_app()` 调 `build_http_app()`
   把 FastMCP 的 streamable_http_app() mount 到 /mcp。鉴权由
   `server.app.core.mcp_auth.McpTokenMiddleware` 在 mount 前处理。

2. **stdio** (可选 dev/air-gap): `python -m server.mcp` 走 `__main__.py` → `main()`,
   token 在 main() 内 assert,不影响 HTTP 路径。

工具按三组分文件注册:
    catalog: 只读列表 (list_* / get_*)
    action: 写操作 (compose / illustrate / submit_review / distribute / notify)
    meta: 评估 / 回流 (score / get_*_performance / record_metrics)
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from server.app.modules.mcp_catalog.connect_router import MCP_TOOLS_COUNT

# stateless_http + json_response: streamable_http_app's session manager requires anyio task
# group via its own lifespan event, but mounted sub-apps don't trigger sub-app lifespans.
# Stateless mode bypasses the task group requirement; json_response keeps wire format simple.
# streamable_http_path="/": default is "/mcp", which stacks with our mount("/mcp", ...) into
# external URL "/mcp/mcp/". Setting to "/" makes external URL just "/mcp/".
mcp = FastMCP(
    "geo",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)

# 触发各 tool 模块注册 (导入即调用 @mcp.tool 装饰器)
from server.mcp.tools import action as _action  # noqa: F401,E402
from server.mcp.tools import catalog as _catalog  # noqa: F401,E402
from server.mcp.tools import meta as _meta  # noqa: F401,E402


def _assert_tools_registered(context: str) -> None:
    """统一的双实例 bug 兜底: 注册 tool 数必须 ≥ MCP_TOOLS_COUNT。"""
    actual = len(mcp._tool_manager._tools)
    if actual < MCP_TOOLS_COUNT:
        raise RuntimeError(
            f"MCP {context} with {actual} registered tools "
            f"(expected ≥{MCP_TOOLS_COUNT}). "
            f"Likely the __main__ vs package double-instance bug. "
            f"Use `python -m server.mcp`, not `python -m server.mcp.server`."
        )


def main() -> None:
    """stdio 入口 (可选 dev 路径)。

    token assert 放这里,只在 stdio 启动时校验。HTTP 路径下 token 缺失由 middleware 在
    请求层返回 401,不阻塞 GEO 启动。
    """
    from server.mcp.config import get_config

    get_config()  # 触发 assert_ready: 缺 GEO_MCP_TOKEN 时抛 RuntimeError
    _assert_tools_registered("stdio start")
    mcp.run()


def build_http_app():
    """HTTP transport 入口 (GEO `create_app()` mount 它)。

    不在这里 assert token —— token 缺失时让 McpTokenMiddleware 在请求层返回 401,
    不阻塞整个 GEO 启动。
    """
    _assert_tools_registered("HTTP app build")
    return mcp.streamable_http_app()


if __name__ == "__main__":
    main()
