# MCP HTTP Transport 接入改造设计稿（2026-06-22）

> 范围：把 GEO MCP server 从「Claude Code 本地 spawn Python 子进程（stdio）」改成「mount 进 GEO FastAPI app 的 streamable HTTP sub-app」。用户端从「装 Python + clone 仓库 + 设 PYTHONPATH」缩到「`~/.claude.json` 填一段 JSON」。
>
> 前置：`docs/superpowers/specs/2026-06-22-mcp-connect-tab-design.md` §1 把 HTTP/SSE transport 列为「非目标 — 留作后续独立 PR」。本稿即该 PR 的兑现。
>
> 配套：`docs/mcp-setup-notes.md`（本稿落地后大改）、`CLAUDE.md` 「MCP Server」段（同步）。

## 1. 背景与目标

### 1.1 触发问题

Windows 宿主无 Python（用户工作流是「Python 工具进 dev 容器跑」）。`~/.claude.json` 配的 `command: "python"` 命中 Microsoft Store 的 App Execution Alias，spawn 出来的子进程直接以 exit code 49 退出，Claude Code `/mcp` 报 `Failed to reconnect to geo: -32000`。

### 1.2 目标

- 用户端**零本地依赖**：不装 Python、不 clone 仓库、不设 PYTHONPATH。`~/.claude.json` 填 url + token 即可
- 鉴权语义不变：复用现有 `GEO_MCP_TOKEN` + `X-MCP-Token` header
- stdio 入口**保留**作为可选 dev / air-gap 路径，老配置不破坏
- 部署成本：零数据迁移、零 docker-compose 改动、零反代规则改动

### 1.3 非目标（明确不做）

- tool handler 重构为直接调 service 层（保留 self-call HTTPx；列入后续 TODO）
- per-user MCP token（仍全员共享；已有独立 issue）
- MCP endpoint rate limit（列入后续 TODO）
- wheel / PyPI 打包（用户端不再装 Python，wheel 失去意义；放弃此方向）

## 2. 整体架构

```
之前:
  Claude Code (host)
    └─ spawn: python -m server.mcp            ← 用户机要装 Python
         └─ httpx → https://geo.../api/...

之后:
  Claude Code (host)
    └─ HTTP POST → https://geo.../mcp         ← 直接打远端,零本地依赖
                       │
                       ▼ (同一个 uvicorn 进程内)
        FastAPI app
          ├─ /api/...           (现有 GEO API)
          └─ /mcp               (FastMCP streamable_http_app)
                └─ tool handler 仍 httpx 调 http://127.0.0.1:8000/api/...
                                                (self-call,POC 期保留)
```

### 2.1 关键不变量

- MCP server 与 GEO API **同一个 uvicorn 进程、同一个域名、同一套 HTTPS 证书**
- 鉴权仍是 hmac compare_digest 检 `X-MCP-Token` 与 `GEO_MCP_TOKEN`
- 17 个 tool 注册逻辑不动，self-call 经 `GeoApiClient` 仍走 httpx
- stdio 入口（`python -m server.mcp`）保留可用

### 2.2 改动总览

| 文件 | 改动 |
|---|---|
| `server/mcp/server.py` | 新增 `build_http_app()`；token assert 从模块顶层挪到 `main()` 内（避免 HTTP 路径下 GEO 启动被 token 缺失阻塞） |
| `server/mcp/config.py` | 新增 `internal_api_url` 字段，读 `GEO_MCP_INTERNAL_API_URL`，缺失 fallback `http://127.0.0.1:8000`（**不**回退到 `GEO_API_BASE_URL`） |
| `server/mcp/tools/{action,catalog,meta}.py` | 三个文件的 `_client()` 函数：base_url 来源从 `cfg.api_base_url` 改成 `cfg.internal_api_url`（`http_client.py` 本身不动） |
| `server/app/core/mcp_auth.py` | 新增 `McpTokenMiddleware`（starlette `BaseHTTPMiddleware`）；提取共享 helper `verify_mcp_token(sent_token) -> bool`，给 middleware 和现有 `require_mcp_token` 复用 |
| `server/app/main.py` | `create_app()` 内：`mcp_app = build_http_app()` → `mcp_app.add_middleware(McpTokenMiddleware)` → `app.mount("/mcp", mcp_app)` |
| `web/src/features/mcp/McpConnectWorkspace.tsx` | 段 ③ 加 HTTP / stdio toggle，默认 HTTP；新增 `buildHttpConfigJson()`，老 `buildConfigJson` 改名 `buildStdioConfigJson`；段 ② 显示 endpoint 行 `${suggestedBaseUrl}/mcp` |
| `docs/mcp-setup-notes.md` | 重写：HTTP 配置 → 3 节，行数控制在 120 内；stdio 章节折叠到「高级」 |
| `CLAUDE.md` | 「MCP Server」段示例 JSON 改 HTTP 形态；「启动方式」改成「mount 进 GEO FastAPI app」 |
| `server/tests/test_mcp_http_mount.py` | 新文件，4 个用例（见 §5.3） |

## 3. 服务端实现细节

### 3.1 `server/mcp/server.py` — 双入口

```python
mcp = FastMCP("geo")

# 触发 tool 注册（不变）
from server.mcp.tools import action, catalog, meta  # noqa


def main() -> None:
    """stdio 入口（可选 dev 路径）。"""
    get_config()  # token assert 放这里，只在 stdio 启动时校验
    if len(mcp._tool_manager._tools) == 0:
        raise RuntimeError(
            "MCP started with 0 registered tools — likely the __main__ vs package "
            "double-instance bug. Use `python -m server.mcp`, not `python -m server.mcp.server`."
        )
    mcp.run()


def build_http_app():
    """HTTP transport 入口（GEO `create_app()` mount 它）。

    不在这里 assert token —— token 缺失时让 `McpTokenMiddleware` 在请求层返回 401，
    不阻塞 GEO 启动。同样的双实例兜底 assert 仍保留。
    """
    if len(mcp._tool_manager._tools) < 17:
        raise RuntimeError(
            f"MCP HTTP app build with {len(mcp._tool_manager._tools)} tools "
            f"(expected ≥17). Tool registration broken."
        )
    return mcp.streamable_http_app()
```

**关键决策**：模块顶部不再 `_cfg = get_config()`。token assert 移入 `main()`；HTTP 路径下 token 校验完全交给 middleware。

### 3.2 `server/mcp/config.py` — `internal_api_url` 字段

```python
class McpConfig:
    def __init__(self) -> None:
        self.token = os.environ.get("GEO_MCP_TOKEN", "")
        self.api_base_url = os.environ.get("GEO_API_BASE_URL", "http://127.0.0.1:8000")
        # 新增：tool handler self-call 时用的 base url；不 fallback 到 api_base_url
        self.internal_api_url = os.environ.get(
            "GEO_MCP_INTERNAL_API_URL",
            "http://127.0.0.1:8000",
        )
        self.timeout_seconds = float(os.environ.get("GEO_MCP_TIMEOUT_SECONDS", "30"))

    def assert_ready(self) -> None:
        if not self.token:
            raise RuntimeError("GEO_MCP_TOKEN is empty. Set it in GEO .env or mcpServers env.")
```

**为什么不回退到 `api_base_url`**：生产部署 `GEO_API_BASE_URL` 已是公网域名（`http://geo.huanchanghuyu.com`），self-call 绕公网一圈 = 多一次反代 + 多一次 TLS。新字段独立、缺失时强制走 `127.0.0.1:8000`，老配置直接生效，新 env 是可选优化。

### 3.3 `server/mcp/tools/*.py` — 单点改动

三个文件里的 `_client()` 函数：
```python
def _client() -> GeoApiClient:
    cfg = get_config()
    return GeoApiClient(
        base_url=cfg.internal_api_url,  # 从 api_base_url 改成 internal_api_url
        token=cfg.token,
        timeout=cfg.timeout_seconds,
    )
```

### 3.4 `server/app/core/mcp_auth.py` — middleware + 共享 helper

```python
import hmac
import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def verify_mcp_token(sent: str) -> tuple[bool, str]:
    """共享 helper：检查 sent token 是否匹配 GEO_MCP_TOKEN。
    
    返回 (ok, error_detail)。token 未配置时 ok=False, detail="MCP token not configured"。
    """
    configured = os.environ.get("GEO_MCP_TOKEN", "")
    if not configured:
        return False, "MCP token not configured"
    if not hmac.compare_digest(configured, sent):
        return False, "invalid MCP token"
    return True, ""


# 现有 require_mcp_token 改为调 verify_mcp_token，行为不变


class McpTokenMiddleware(BaseHTTPMiddleware):
    """检 X-MCP-Token,失败直接 401,不进 MCP sub-app。"""
    async def dispatch(self, request, call_next):
        sent = request.headers.get("X-MCP-Token", "")
        ok, detail = verify_mcp_token(sent)
        if not ok:
            return JSONResponse({"detail": detail}, status_code=401)
        return await call_next(request)
```

### 3.5 `server/app/main.py:create_app()` — mount

`create_app()` 内、所有 API router include 之后、SPA fallback 之前：

```python
from server.mcp.server import build_http_app
from server.app.core.mcp_auth import McpTokenMiddleware

mcp_app = build_http_app()
mcp_app.add_middleware(McpTokenMiddleware)
app.mount("/mcp", mcp_app)
```

mount path 选 `/mcp`（不是 `/api/mcp`）—— SPA fallback 是「任何非 `/api/` 路径返回 `index.html`」，所以新 mount 必须在 SPA fallback 之前注册。这是 `main.py` 的既定路由顺序约束。

### 3.6 stdio 入口保留 — `server/mcp/__main__.py` 不动

```python
from server.mcp.server import main
main()
```

dev 期：`docker exec -i geo-collab-app-1 python -m server.mcp` 仍可起 stdio MCP server。token 在 `main()` 内 assert，与 HTTP 路径解耦。

## 4. 前端 + 文档

### 4.1 `web/src/features/mcp/McpConnectWorkspace.tsx`

**改动**：
- 老 `buildConfigJson(suggestedBaseUrl)` 改名 `buildStdioConfigJson(suggestedBaseUrl)`，body 不变
- 新增 `buildHttpConfigJson(suggestedBaseUrl)`：

```ts
function buildHttpConfigJson(suggestedBaseUrl: string): string {
  const template = {
    mcpServers: {
      geo: {
        transport: "http",
        url: `${suggestedBaseUrl}/mcp`,
        headers: { "X-MCP-Token": "<PASTE_YOUR_TOKEN_HERE>" },
      },
    },
  };
  return JSON.stringify(template, null, 2);
}
```

- 段 ③ 上方加 transport toggle，state `transport: "http" | "stdio"`，默认 `"http"`
- 选 stdio 时模板下方加红色 hint：「需要在本机装 Python + clone 仓库，仅推荐本机开发 / air-gap 场景。」
- 段 ② 显示 token 状态时在 `suggested_base_url` 下加一行：`MCP endpoint: {suggestedBaseUrl}/mcp`
- 段 ④「测试连接」按钮逻辑不变（仍 POST `/api/mcp/health`）

**不动**：
- `getMcpStatus` / `pingMcpHealth` API
- 后端 `server/app/modules/mcp_catalog/connect_router.py`（`suggested_base_url` 字段不变，`/mcp` 拼接在前端做）

### 4.2 `docs/mcp-setup-notes.md` — 重写

精简到 3 节、≤ 120 行：

```
## 准备
1. GEO 后端 .env 设 GEO_MCP_TOKEN=<token>
2. 浏览器开 GEO「MCP 接入」tab，复制段 ③ HTTP 配置模板

## 配置 Claude Code
~/.claude.json 粘贴模板，替换 <PASTE_YOUR_TOKEN_HERE>。
重启 Claude Code → /mcp 应见 geo: connected + 17 tools。

## 高级：本地 dev / air-gap（stdio）
（现有的 clone + venv + pip + PYTHONPATH 流程保留，标"不推荐"）
```

「公网部署 + 多机接入」节大改：
- 服务端章节保留（HTTPS、`X-Forwarded-Proto`、token 生成）
- 客户端章节砍掉 git clone / venv / PYTHONPATH，只剩「复制 → 粘 → 重启」三步
- **新增红字**：Nginx 反代 `location /mcp` 块必须加 `proxy_buffering off; proxy_request_buffering off;`（streamable HTTP 需要 chunked，Nginx 默认 buffering 会阻塞 stream）

### 4.3 `CLAUDE.md` 「MCP Server」段

- 示例 JSON 改 HTTP 形态
- 「启动方式」说明改：「随 GEO FastAPI 一起启动，mount 在 `/mcp`，无独立进程」
- 「加新 tool 的步骤」第 4 步：「重启 GEO 后端进程 → 重启 Claude Code → `/mcp`」
- 新增一段「stdio 兼容入口」简短说明 `python -m server.mcp` 仍可用

## 5. 测试 / 验收

### 5.1 后端测试 `server/tests/test_mcp_http_mount.py`

| 用例 | 断言 |
|---|---|
| `test_mcp_endpoint_requires_token` | 不带 `X-MCP-Token` POST `/mcp` → 401，detail = "MCP token not configured" 或 "invalid MCP token" |
| `test_mcp_endpoint_invalid_token` | 带错 token → 401, detail = "invalid MCP token" |
| `test_mcp_endpoint_initialize` | 带对 token + 标准 MCP `initialize` JSON-RPC payload → 200, 返回包含 ≥ 17 tool |
| `test_mcp_no_token_configured` | env 没设 `GEO_MCP_TOKEN`，任何带 token 请求 → 401 (`monkeypatch.delenv`) |

测试方式：用 `build_test_app(monkeypatch)` 起 GEO app，httpx `AsyncClient` 直接打 `/mcp`，构造 MCP 标准 JSON-RPC payload。不通过 FastMCP client SDK，纯 HTTP 验证。

### 5.2 回归测试（确保现有 MCP 路径不破）

- `test_mcp_catalog.py` / `test_mcp_action.py` / `test_mcp_meta.py` 不修改、全绿
- `test_mcp_entry` （现有的 docker 守卫测试）不修改、全绿
- `test_mcp_auth.py` 如果存在，verify_mcp_token 共享 helper 不改变行为

### 5.3 手动验收清单

**后端 dev 容器**:
```bash
docker compose -f docker-compose.dev.yml exec app curl -X POST http://127.0.0.1:8000/mcp \
  -H "X-MCP-Token: $GEO_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
```
应返回 200 + 协议握手响应。

**前端**:
- `pnpm --filter @geo/web typecheck` 绿
- `pnpm --filter @geo/web build` 绿
- 浏览器开「MCP 接入」tab：段 ③ 默认 HTTP 模板；toggle 切 stdio 看到红字 hint；段 ④「测试连接」点了仍能 ping 通

**真实接入**（Windows host）:
- `~/.claude.json` 替换 HTTP 模板、填 token
- 重启 Claude Code → `/mcp` 显示 `geo: connected` + 17 tools
- 在 Claude Code 里调 `list_articles(limit=5)` 返回数据
- stdio 老配置（命令行进 dev 容器 `python -m server.mcp`）仍可起、tools = 17

### 5.4 lint / type-check / 全套测试

```bash
ruff check server/
ruff format --check server/
mypy server/app
pytest server/tests/ -q  # 含新 test_mcp_http_mount.py
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```

## 6. 回滚

改造分两条独立 commit:

| Commit | 内容 | 单独 revert 影响 |
|---|---|---|
| **C1** | 后端 mount + `core/mcp_auth.py` middleware + `server/mcp/*` 改动 + 后端测试 | revert → `/mcp` 404；stdio 仍可用；前端「MCP 接入」tab 段 ② 状态不变（`/api/mcp/status` 不依赖 HTTP mount） |
| **C2** | 前端 `McpConnectWorkspace.tsx` HTTP 模板 + `mcp-setup-notes.md` + `CLAUDE.md` | revert → 前端段 ③ 回到 stdio 模板；后端 `/mcp` 仍正常工作。无本机 Python 的用户需手动从文档 / Slack 抄 HTTP 配置形态，但功能上不被 C2 revert 阻塞 |

**部署时序**：先 deploy C1 + 用 `curl` 验证 `/mcp` 联通（不需要等前端）→ 再 deploy C2。

**零数据迁移** —— 整个改造没动任何表。

## 7. 风险点 + 缓解

| 风险 | 触发条件 | 缓解 |
|---|---|---|
| FastMCP 双实例 bug 在 HTTP 路径下复发 | `build_http_app()` 调用时 `streamable_http_app()` 内部实例切换 | `build_http_app()` 入口 assert tools ≥ 17；`test_mcp_endpoint_initialize` 验证返回 tool 数 |
| token middleware 穿透 | streamable HTTP sub-app 内部多 endpoint，middleware 顺序错误绕开 | 测试覆盖 unauth `POST`/`GET`/`OPTIONS`；middleware 用 `mcp_app.add_middleware()` 在 mount 之前加 |
| self-call 死锁 | tool handler sync httpx → 同进程 FastAPI；uvicorn worker 线程池满 | uvicorn 默认 threadpool=40，POC 期单用户场景远低于。监控加 `tool_call_duration_ms` 日志，超 5s 告警 |
| Nginx 未关 buffering | streamable HTTP 依赖 chunked，nginx 默认 `proxy_buffering on` 拖延 stream | `docs/mcp-setup-notes.md` 服务端章节红字提示；提供 nginx `location /mcp` 片段 |
| `GEO_MCP_INTERNAL_API_URL` 漏配 | 老 docker-compose 没 set 此 env，但代码改用了 `internal_api_url` | `McpConfig` fallback `http://127.0.0.1:8000`（不回退到 `api_base_url`），缺失 = 用安全默认值 |
| 现有 stdio 配置突然失效 | 用户已经按 stdio 老文档配过 ~/.claude.json | stdio 入口完全保留、不删；老配置继续工作。前端段 ③ toggle 让他们看到 HTTP 模板并主动迁移 |

## 8. 后续 TODO（本稿不做）

- tool handler 直接调 service 层，绕开 self-call HTTPx（P2，工作量 ≈ 17 tool × refactor + dependency injection 改造）
- per-user MCP token（P1，已有独立 issue）
- MCP endpoint rate limit（`slowapi` 已挂，`@limiter.limit("60/minute")` 一行事；P2）
- self-call 改成 `httpx.AsyncClient` + asyncio loop（去掉 sync httpx 占 threadpool 槽位）
