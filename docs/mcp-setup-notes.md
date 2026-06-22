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

## 公网部署 + 多机接入

> 本节面向「GEO 已在公网服务器跑起来 + 团队多人各自装 Claude Code 接 MCP」的场景。本机单人开发不需要看这节。

### 服务端（公网部署机，一次性）

1. **HTTPS 反代**：Nginx/Caddy 把 `https://geo.example.com` 反代到 `127.0.0.1:8000`。必须透传 `X-Forwarded-Proto` 与 `X-Forwarded-Host`，否则前端「MCP 接入」tab 的 `suggested_base_url` 会显示成内网地址。
   - Caddy 默认透传，无需配置
   - Nginx 在 `location /` 块加：
     ```nginx
     proxy_set_header X-Forwarded-Proto $scheme;
     proxy_set_header X-Forwarded-Host  $host;
     proxy_set_header X-Real-IP         $remote_addr;
     ```
2. **uvicorn proxy headers**：FastAPI 启动加 `--proxy-headers --forwarded-allow-ips="*"`（docker-compose 里改 `command`）。
3. **token 生成 + 注入**：
   ```bash
   openssl rand -hex 32  # 生成 64 字符 hex token
   echo 'GEO_MCP_TOKEN=<token>' >> .env
   docker compose restart app
   ```
4. **验证**：浏览器开 `https://geo.example.com` → 登录 → 进「MCP 接入」tab，段 ② 应显示「✓ 服务端 token 已配置」+ 建议 base_url 是公网域名。

### 客户端（每台外部机器，一次性）

1. **克隆 + venv**（Windows / macOS / Linux 通用）：
   ```bash
   git clone https://github.com/geo-ihuanlegame/geo-collab.git
   cd geo-collab
   python -m venv .venv
   # PowerShell:
   .venv\Scripts\Activate.ps1
   # bash:
   source .venv/bin/activate
   ```

2. **装 MCP 子集依赖**（不要装全量 requirements.txt——那里有 sqlalchemy / playwright 等不必要的重依赖）：
   ```bash
   pip install -r requirements-mcp.txt
   ```

3. **编辑 `~/.claude.json`**：从 GEO 前端「MCP 接入」tab 段 ③ 复制 JSON 模板，替换三处：
   - `GEO_MCP_TOKEN` → admin 给你的 token
   - `GEO_API_BASE_URL` → 已是公网域名（前端自动填）；若你的 Claude Code 跑在容器里则改 `host.docker.internal`
   - `PYTHONPATH` → 你刚刚 `git clone` 出来的绝对路径，**Windows 用双反斜杠**：`C:\\Users\\<you>\\geo-collab`

4. **重启 Claude Code → `/mcp`**：应看到 `geo: connected` + 工具数与 tab 段 ① 一致。

### 安全须知

- token 与用户密码同等敏感。**不要**在 wiki / 群聊 / commit 明文传，建议 1Password / Bitwarden / 私聊。
- 公网部署**必须 HTTPS**。明文 HTTP 等于 token 明传，被 sniff 就完了。
- POC 期所有客户端共享同一个 token，**任一台机器泄露即影响全员**。下一步引入 per-user token 会单独 spec。

### 一键脚本（可选）

仓库根 `scripts/setup-mcp-client.{sh,ps1}` 提供 clone + venv + pip install + 提示用户编辑 `~/.claude.json` 的一键化流程。本 PR 未包含该脚本——见后续 issue。
