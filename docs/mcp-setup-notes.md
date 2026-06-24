# GEO MCP Server · 接入指引

POC 期 MCP server 跟 GEO FastAPI app **同进程 mount**(路径 `/mcp`),用户端不需要装 Python / clone 仓库,`~/.claude.json` 填 url + token 即可。

## 准备

1. **生成 MCP token**:
   - bash: `openssl rand -hex 32`
   - PowerShell: `-join ((48..57) + (97..102) | Get-Random -Count 64 | %{[char]$_})`
2. **GEO 后端 `.env` 加** `GEO_MCP_TOKEN=<token>` 并重启 (`docker compose restart app`)
3. **浏览器**打开 GEO 前端 → 「MCP 接入」tab,确认段 ② 显示 "✓ 服务端 token 已配置"

## 配置 Claude Code (HTTP 推荐)

在 GEO「MCP 接入」tab 段 ③ 选 "HTTP (推荐)",复制 JSON 到 `~/.claude.json`,替换 `<PASTE_YOUR_TOKEN_HERE>`:

```json
{
  "mcpServers": {
    "geo": {
      "transport": "http",
      "url": "https://geo.example.com/mcp",
      "headers": { "X-MCP-Token": "<token>" }
    }
  }
}
```

重启 Claude Code → 输入 `/mcp` → 应看到 `geo: connected` + 17 个工具。

## 公网部署

### 服务端

1. **HTTPS 反代**: Nginx / Caddy 把 `https://geo.example.com` 反代到 `127.0.0.1:8000`,透传 `X-Forwarded-Proto` / `X-Forwarded-Host`。
2. **Nginx 必须关 `/mcp` 路径的 buffering**(streamable HTTP 依赖 chunked):
   ```nginx
   location /mcp {
     proxy_pass http://127.0.0.1:8000;
     proxy_buffering off;
     proxy_request_buffering off;
     proxy_set_header X-Forwarded-Proto $scheme;
     proxy_set_header X-Forwarded-Host  $host;
     proxy_set_header X-Real-IP         $remote_addr;
     proxy_read_timeout 3600;
   }
   ```
   Caddy 默认就关 buffering, 无需特殊配置。
3. **uvicorn proxy headers**: `--proxy-headers --forwarded-allow-ips="*"`。
4. **token 注入**:
   ```bash
   openssl rand -hex 32  # 64 字符 hex
   echo 'GEO_MCP_TOKEN=<token>' >> .env
   docker compose restart app
   ```
5. **MCP 工具自调用地址（仅 HTTP-mount 同进程部署）**: 工具执行时,tool handler 用 httpx 自调用
   `{GEO_MCP_INTERNAL_API_URL}/api/mcp/...`(与 `GEO_API_BASE_URL` 是两个独立变量;后者只给客户端
   `~/.claude.json`)。缺省 `http://127.0.0.1:8000`,`docker-compose.yml` 的 `app` 服务已显式钉死。
   **切勿**把它配成公网域名——容器向自己的公网域名发请求,阿里云 ECS 默认不支持发卡回环(hairpin NAT)
   → 30s 超时(表现为 `GET /api/mcp/...: network error: timed out`,但 `/mcp/` 网关本身 401 正常)。

### 客户端 (每台外部机器)

只需 1 步:复制段 ③ HTTP 模板 → 粘 `~/.claude.json` → 重启 Claude Code。**不需要装 Python**。

## 高级: 本地 dev / air-gap (stdio)

stdio 入口保留可用,适合本机调试 tool 逻辑 / air-gap 不能访问 `/mcp` 的场景。这条路径需要本机装 Python + clone 仓库。

```bash
git clone https://github.com/geo-ihuanlegame/geo-collab.git
cd geo-collab
python -m venv .venv
.venv\Scripts\Activate.ps1     # PowerShell
# 或者 source .venv/bin/activate # bash
pip install -r requirements-mcp.txt
```

`~/.claude.json`(stdio 模式):

```json
{
  "mcpServers": {
    "geo": {
      "command": "python",
      "args": ["-m", "server.mcp"],
      "env": {
        "GEO_MCP_TOKEN": "<token>",
        "GEO_API_BASE_URL": "http://127.0.0.1:8000",
        "PYTHONPATH": "C:\\Users\\<you>\\geo-collab"
      }
    }
  }
}
```

> Windows 路径用双反斜杠。GEO 后端运行在容器、Claude Code 在宿主时,`GEO_API_BASE_URL` 用 `http://host.docker.internal:8000`。

## 调试

- **看 MCP server 日志**:HTTP 路径下,FastMCP 日志合到 GEO uvicorn 输出;stdio 路径下日志写 stderr,Claude Code 面板显示。
- **手动验 HTTP 路径**:
  ```bash
  curl -X POST https://geo.example.com/mcp/ \
    -H "X-MCP-Token: <token>" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
  ```
- **手动验 stdio 路径**:
  ```bash
  docker compose -f docker-compose.dev.yml exec -T -e GEO_MCP_TOKEN=<token> app python -m server.mcp
  ```
  (阻塞等 stdin, Ctrl-C 退出)

## 常见问题

- **`-32000` / `Failed to reconnect`**:99% 是 transport 配错(`command: "python"` 但本机没 Python)。改成 HTTP 模板。
- **`401 MCP token not configured`**: GEO 后端没读到 `GEO_MCP_TOKEN`。重启 uvicorn / docker。
- **`401 invalid MCP token`**: `~/.claude.json` 的 token 与 GEO 后端 `.env` 不一致。
- **HTTP 200 但 tool 调用挂住 / 慢**: Nginx 没关 `proxy_buffering`,streamable 流被阻塞。改 nginx config。
- **`/mcp` 返回 SPA index.html**: mount 顺序错(挂在 SPA fallback 之后)。看 `create_app()` 内 mount 是否在 `app.mount("/assets", ...)` 之前。

## 安全须知

- token 与用户密码同等敏感。**不要** wiki / 群聊 / commit 明文传。
- 公网部署**必须 HTTPS**。明文 HTTP 等于 token 明传。
- POC 期所有客户端共享同一个 token,任一台机器泄露即影响全员。per-user token 见后续 spec。
