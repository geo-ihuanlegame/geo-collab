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
    """stdio 模式入口（被 Claude Code spawn 时用）。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
