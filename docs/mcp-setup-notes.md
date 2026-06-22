# GEO MCP Server · 本机开发配置

## 准备

1. 生成 MCP token:
   - bash: `openssl rand -hex 32`
   - PowerShell: `-join ((48..57) + (97..102) | Get-Random -Count 64 | %{[char]$_})`
2. GEO 后端 `.env` 加 `GEO_MCP_TOKEN=<token>` 并重启
3. Claude Code 配 `~/.claude.json` 的 `mcpServers.geo`，env 里同样设 `GEO_MCP_TOKEN`

## 配置示例

`~/.claude.json` 的 `mcpServers.geo`:

```json
{
  "mcpServers": {
    "geo": {
      "command": "python",
      "args": ["-m", "server.mcp"],
      "env": {
        "GEO_MCP_TOKEN": "<32-byte hex token>",
        "GEO_API_BASE_URL": "http://127.0.0.1:8000",
        "PYTHONPATH": "C:\\\\Users\\\\admin\\\\Desktop\\\\geo-collab"
      }
    }
  }
}
```

> **重要**：`PYTHONPATH` 要指向 geo-collab 仓库根。Windows 下注意双反斜杠转义。

## 调试

- 看 MCP server 日志：FastMCP stdio 把 server 端日志写到 stderr（Claude Code 会有面板显示）
- 手动起 MCP server（dev 容器内验证 import 链）：
  ```bash
  docker compose -f docker-compose.dev.yml exec -T -e GEO_MCP_TOKEN=test-abc app python -m server.mcp
  ```
  （阻塞等 stdin，确认 import 路径正确；Ctrl-C 退出）
- 列已注册 tool：
  ```bash
  docker compose -f docker-compose.dev.yml exec -T -e GEO_MCP_TOKEN=test-abc app python -c "from server.mcp.server import mcp; print(sorted(t.name for t in mcp._tool_manager._tools.values()))"
  ```
- **不要用 `python -m server.mcp.server`**：会触发 Python `__main__` vs 包模块双实例 bug——
  server.py 被加载两遍、`mcp = FastMCP(...)` 各建一个实例，结果 tools 注册在一个上、
  `mcp.run()` 跑在另一个上，stdio `tools/list` 返回空。server.py `main()` 现在带兜底断言，
  老命令会直接抛 RuntimeError 而不是静默 "no tools"。

## 验收（在 Claude Code 里）

1. 重启 Claude Code → 输入 `/mcp` → 应看到 `geo: connected` 和工具列表
2. 让 Claude 调用 `list_articles(limit=5)` → 应返回 GEO 实际数据（即使空也算 ok）

## 常见问题

- **`401 MCP token not configured`**：GEO 后端没读到 `GEO_MCP_TOKEN`（启动时漏 export / .env 没生效，需重启 uvicorn）
- **`401 invalid MCP token`**：两边 token 不一致（`~/.claude.json` 和 GEO 后端 .env）
- **`network error`**：GEO 后端没起 / 端口不对 / Claude Code 跑在宿主而 GEO 跑在容器（注意 `GEO_API_BASE_URL` 用 `http://host.docker.internal:8000` 而不是 `127.0.0.1` 如果有这层 isolation）
- **Claude Code 看不到 `geo` server**：`~/.claude.json` JSON 格式不对 / Claude Code 没重启
- **`RuntimeError: GEO_MCP_TOKEN is empty`**：MCP server 进程启动时 env 没传入（`~/.claude.json` 里的 `env` 字段忘了配）
- **`/mcp` 显示 `geo · connected · no tools`**：99% 是 MCP 命令配成了 `python -m server.mcp.server`（要 `python -m server.mcp`）。改完 `~/.claude.json` 重启 Claude Code 即可。
