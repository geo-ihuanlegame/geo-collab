"""MCP server 启动配置（独立于 GEO 后端的 get_settings()）。

不复用 GEO Settings：MCP server 是独立进程，启动时只关心两件事——往哪发请求、用什么 token。
环境变量从 Claude Code 的 ~/.claude.json mcpServers.geo.env 注入。
"""

from __future__ import annotations

import os


class McpConfig:
    def __init__(self) -> None:
        self.token = os.environ.get("GEO_MCP_TOKEN", "")
        self.api_base_url = os.environ.get("GEO_API_BASE_URL", "http://127.0.0.1:8000")
        # 同进程 mount 时 tool handler self-call 用的 base url。
        # 不 fallback 到 api_base_url —— 生产环境 api_base_url 是公网域名,
        # self-call 绕公网一圈浪费,缺失时强制走 127.0.0.1:8000。
        self.internal_api_url = os.environ.get(
            "GEO_MCP_INTERNAL_API_URL",
            "http://127.0.0.1:8000",
        )
        self.timeout_seconds = float(os.environ.get("GEO_MCP_TIMEOUT_SECONDS", "60"))

    def assert_ready(self) -> None:
        if not self.token:
            raise RuntimeError("GEO_MCP_TOKEN is empty. Set it in Claude Code mcpServers.geo.env.")


def get_config() -> McpConfig:
    cfg = McpConfig()
    cfg.assert_ready()
    return cfg
