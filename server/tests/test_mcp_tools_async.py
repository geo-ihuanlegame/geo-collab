"""回归守卫：所有 MCP tool 必须是 async。

为什么这条很重要：FastMCP 对同步 tool 是在事件循环里 inline 跑的
(mcp.server.fastmcp.utilities.func_metadata.FuncMetadata.call_fn_with_arg_validation
的同步分支直接 `return fn(...)`)。HTTP-mount 部署下 tool 的 self-call 打的是同进程
同一个单 worker uvicorn(127.0.0.1:8000)——同步阻塞会把事件循环锁死、self-call 永远
等不到处理 → 自调用死锁，线上表现为所有 MCP 工具 30s timeout（2026-06-23 的故障根因）。

这个测试确保以后新增 tool 不会再退回同步实现。无需 DB / 无需 GEO_MCP_TOKEN：
只 import server.mcp.*（config/http_client/server/tools），均不建 DB 引擎、不在 import
期校验 token。
"""

from __future__ import annotations

import inspect


def test_all_mcp_tools_are_async() -> None:
    # import server.mcp.server 即触发三组 tool 模块 import + @mcp.tool 注册
    from server.mcp.server import mcp

    tools = mcp._tool_manager._tools
    assert tools, "没有注册到任何 MCP tool —— import 链路坏了"

    sync_tools = sorted(
        name
        for name, tool in tools.items()
        if not inspect.iscoroutinefunction(getattr(tool, "fn", None))
    )
    assert not sync_tools, (
        "这些 MCP tool 是同步的，会在 self-call 时死锁单 worker 事件循环，"
        f"必须改成 async def + anyio.to_thread.run_sync：{sync_tools}"
    )
