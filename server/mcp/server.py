"""GEO MCP Server — FastMCP stdio 入口。

启动方式（POC 期由 Claude Code 自动 spawn，开发时手测可以）:
    python -m server.mcp.server

工具按三组分文件注册:
    catalog: 只读列表（list_*  / get_*）
    action: 写操作（compose / illustrate / submit_review / distribute / notify）
    meta: 评估 / 回流（score / get_*_performance / record_metrics）
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from server.mcp.config import get_config

# 启动时把 config 加载一次（缺 token 直接抛错，提示用户配 env）
_cfg = get_config()

mcp = FastMCP("geo")

# 触发各 tool 模块注册（导入即调用 @mcp.tool 装饰器）
from server.mcp.tools import action as _action  # noqa: F401,E402
from server.mcp.tools import catalog as _catalog  # noqa: F401,E402
from server.mcp.tools import meta as _meta  # noqa: F401,E402


def main() -> None:
    """stdio 模式入口（被 Claude Code spawn 时用）。

    **必须通过 `python -m server.mcp` 调起**，不要 `python -m server.mcp.server`：
    后者会让 server.py 同时作为 __main__ 和 server.mcp.server 被加载两次、产生两个
    FastMCP 实例，tools 注册在第二个实例上、run 跑第一个，导致 tools/list 返回空。
    入口在 server/mcp/__main__.py，下面这条断言是兜底，防止有人改回老命令时静默失败。
    """
    if len(mcp._tool_manager._tools) == 0:
        raise RuntimeError(
            "MCP started with 0 registered tools — likely invoked as "
            "`python -m server.mcp.server` which triggers the __main__ vs package "
            "dual-import bug. Use `python -m server.mcp` instead."
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
