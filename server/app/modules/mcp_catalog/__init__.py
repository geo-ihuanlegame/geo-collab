"""MCP catalog 只读路由模块（service token 鉴权）。

把跨模块的只读 list / get 端点集中在 `/api/mcp/...` 下，给 Claude Code Loop 直接拉数据用。
所有端点通过 `require_mcp_token` 鉴权，不走 user JWT cookie。
"""
