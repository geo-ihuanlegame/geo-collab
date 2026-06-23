# MCP HTTP Transport 接入改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 GEO MCP server 从 stdio 子进程改成 mount 进 GEO FastAPI 的 streamable HTTP sub-app，用户端 `~/.claude.json` 只需填 url + token，无须装 Python / clone 仓库 / 设 PYTHONPATH。

**Architecture:** FastMCP 的 `streamable_http_app()` 作为 ASGI sub-app mount 到 `/mcp` 路径。token 鉴权用 starlette `BaseHTTPMiddleware` 挂在 mount path 前。stdio 入口 (`python -m server.mcp`) 完全保留作为 dev/air-gap 路径。

**Tech Stack:** FastAPI / starlette / FastMCP (`mcp[cli]>=1.0`) / httpx / React 19 + TypeScript。

**Spec:** `docs/superpowers/specs/2026-06-22-mcp-http-transport-design.md`

---

## File Structure

**Backend changes:**
- `server/mcp/config.py` — add `internal_api_url` field
- `server/mcp/tools/{action,catalog,meta}.py` — `_client()` 改用 `internal_api_url`
- `server/mcp/server.py` — 新增 `build_http_app()`、token assert 挪入 `main()`
- `server/app/core/mcp_auth.py` — 抽出 `verify_mcp_token` helper + 新增 `McpTokenMiddleware`
- `server/app/main.py` — `create_app()` 内 mount `/mcp`
- `server/tests/test_mcp_http_mount.py` — 新文件，5 个用例

**Frontend changes:**
- `web/src/features/mcp/McpConnectWorkspace.tsx` — transport toggle (HTTP 默认 / stdio 折叠)、新 HTTP 模板生成函数、段 ② 加 endpoint 提示行

**Docs:**
- `docs/mcp-setup-notes.md` — 重写
- `CLAUDE.md` — 「MCP Server」段同步

**Two commit topology:**
- **C1**(Task 1–8): 后端 mount + 测试。单独 revert: `/mcp` 404，stdio 仍可用，前端段 ② 状态不变
- **C2**(Task 9–14): 前端 + 文档。单独 revert: 前端段 ③ 回旧 stdio 模板，后端 `/mcp` 仍工作

---

## Task 1: 给 McpConfig 加 `internal_api_url` 字段

**Files:**
- Modify: `server/mcp/config.py`
- Test: `server/tests/test_mcp_http_client.py`（追加用例）

**为什么不 fallback 到 `api_base_url`**：生产部署 `GEO_API_BASE_URL` 已是公网域名，self-call 绕公网一圈浪费。`internal_api_url` 缺失时强制走 `127.0.0.1:8000`，老配置直接生效。

- [ ] **Step 1: 在测试文件追加配置测试**

`server/tests/test_mcp_http_client.py` 文件尾追加（保留现有 import 与测试，不改）：

```python
def test_mcp_config_internal_api_url_defaults_to_localhost(monkeypatch):
    """internal_api_url 缺失时回退到 127.0.0.1:8000，不复用 api_base_url。"""
    from server.mcp.config import McpConfig

    monkeypatch.setenv("GEO_MCP_TOKEN", "dummy-token-for-test")
    monkeypatch.setenv("GEO_API_BASE_URL", "https://geo.example.com")
    monkeypatch.delenv("GEO_MCP_INTERNAL_API_URL", raising=False)
    cfg = McpConfig()
    assert cfg.api_base_url == "https://geo.example.com"
    assert cfg.internal_api_url == "http://127.0.0.1:8000"


def test_mcp_config_internal_api_url_respects_env(monkeypatch):
    """显式设 GEO_MCP_INTERNAL_API_URL 时尊重该值。"""
    from server.mcp.config import McpConfig

    monkeypatch.setenv("GEO_MCP_TOKEN", "dummy-token-for-test")
    monkeypatch.setenv("GEO_MCP_INTERNAL_API_URL", "http://localhost:8123")
    cfg = McpConfig()
    assert cfg.internal_api_url == "http://localhost:8123"
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_http_client.py::test_mcp_config_internal_api_url_defaults_to_localhost server/tests/test_mcp_http_client.py::test_mcp_config_internal_api_url_respects_env -q
```

Expected: 两个用例 FAIL，错误信息 `AttributeError: 'McpConfig' object has no attribute 'internal_api_url'`。

- [ ] **Step 3: 给 `McpConfig` 加 `internal_api_url` 字段**

修改 `server/mcp/config.py`：

```python
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
        self.timeout_seconds = float(os.environ.get("GEO_MCP_TIMEOUT_SECONDS", "30"))

    def assert_ready(self) -> None:
        if not self.token:
            raise RuntimeError("GEO_MCP_TOKEN is empty. Set it in Claude Code mcpServers.geo.env.")
```

- [ ] **Step 4: 跑测试，确认通过**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_http_client.py -q
```

Expected: 所有用例 PASS（含新增 2 个 + 现有用例无回归）。

- [ ] **Step 5: 提交**

```bash
git add server/mcp/config.py server/tests/test_mcp_http_client.py
git commit -m "feat(mcp): McpConfig 新增 internal_api_url 字段(self-call 专用)

不 fallback 到 api_base_url——后者生产为公网域名,self-call 绕公网一圈浪费。
缺失时强制走 127.0.0.1:8000,老配置直接生效。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 三个 tool 文件 `_client()` 改用 `internal_api_url`

**Files:**
- Modify: `server/mcp/tools/action.py:16-18`
- Modify: `server/mcp/tools/catalog.py:17-19`
- Modify: `server/mcp/tools/meta.py:12-14`

三个文件的 `_client()` 函数体一模一样。改动也一样：`cfg.api_base_url` → `cfg.internal_api_url`。

- [ ] **Step 1: 改 `tools/action.py`**

找到当前的 `_client()`：
```python
def _client() -> GeoApiClient:
    cfg = get_config()
    return GeoApiClient(base_url=cfg.api_base_url, token=cfg.token, timeout=cfg.timeout_seconds)
```

改成：
```python
def _client() -> GeoApiClient:
    cfg = get_config()
    return GeoApiClient(
        base_url=cfg.internal_api_url,
        token=cfg.token,
        timeout=cfg.timeout_seconds,
    )
```

- [ ] **Step 2: 改 `tools/catalog.py`**（同样替换 `base_url=cfg.api_base_url` → `base_url=cfg.internal_api_url`）

- [ ] **Step 3: 改 `tools/meta.py`**（同样替换）

- [ ] **Step 4: 确认 `api_base_url` 没有其他引用**

```bash
docker compose -f docker-compose.dev.yml exec -T app grep -rn "api_base_url" server/mcp/
```

Expected: 只剩 `server/mcp/config.py` 里的定义、`server/tests/test_mcp_http_client.py` 里测试断言。

- [ ] **Step 5: 跑现有 MCP tool 测试，确认无回归**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_catalog.py server/tests/test_mcp_http_client.py -q
```

Expected: 全 PASS。

- [ ] **Step 6: 提交**

```bash
git add server/mcp/tools/action.py server/mcp/tools/catalog.py server/mcp/tools/meta.py
git commit -m "feat(mcp): tool _client() 改用 internal_api_url 走 self-call

为 HTTP transport 同进程 mount 做准备。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 抽出 `verify_mcp_token` 共享 helper

**Files:**
- Modify: `server/app/core/mcp_auth.py`
- Test: `server/tests/test_mcp_auth.py`（追加用例，现有用例不动）

共享 helper 让 `require_mcp_token`(用于 sub-router) 和 `McpTokenMiddleware`(用于 mount sub-app,Task 4 加) 都能用同一份 hmac compare_digest 逻辑。

- [ ] **Step 1: 看现有 `test_mcp_auth.py` 风格**

```bash
docker compose -f docker-compose.dev.yml exec -T app head -40 server/tests/test_mcp_auth.py
```

留意现有 `monkeypatch` / `get_settings.cache_clear()` 用法（settings 是 lru_cache）。

- [ ] **Step 2: 在 `test_mcp_auth.py` 追加测试**

```python
def test_verify_mcp_token_unconfigured(monkeypatch):
    from server.app.core.config import get_settings
    from server.app.core.mcp_auth import verify_mcp_token

    monkeypatch.setenv("GEO_MCP_TOKEN", "")
    get_settings.cache_clear()
    ok, detail = verify_mcp_token("any-token")
    assert ok is False
    assert detail == "MCP token not configured"


def test_verify_mcp_token_mismatch(monkeypatch):
    from server.app.core.config import get_settings
    from server.app.core.mcp_auth import verify_mcp_token

    monkeypatch.setenv("GEO_MCP_TOKEN", "real-token")
    get_settings.cache_clear()
    ok, detail = verify_mcp_token("wrong-token")
    assert ok is False
    assert detail == "invalid MCP token"


def test_verify_mcp_token_match(monkeypatch):
    from server.app.core.config import get_settings
    from server.app.core.mcp_auth import verify_mcp_token

    monkeypatch.setenv("GEO_MCP_TOKEN", "real-token")
    get_settings.cache_clear()
    ok, detail = verify_mcp_token("real-token")
    assert ok is True
    assert detail == ""
```

- [ ] **Step 3: 运行新测试，确认失败**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_auth.py -q -k verify_mcp_token
```

Expected: 3 用例 FAIL，`ImportError: cannot import name 'verify_mcp_token' from 'server.app.core.mcp_auth'`。

- [ ] **Step 4: 重构 `server/app/core/mcp_auth.py`**

替换整个文件内容：

```python
"""MCP token 鉴权依赖与共享 helper。

独立于 user JWT 的 service token:
- 空配置 (`GEO_MCP_TOKEN=""`) 视作"MCP 已禁用",任何带 token 的请求都返回 401。
- 配置非空时,校验请求 header `X-MCP-Token` 是否匹配。
- 使用 `hmac.compare_digest` 做常数时间比较,避免 timing attack。

两个入口共享同一 `verify_mcp_token` helper:
- `require_mcp_token`: FastAPI Depends, 给 sub-router (auto_review_router 等) 用
- `McpTokenMiddleware`: starlette BaseHTTPMiddleware, 给 mount 的 sub-app (/mcp) 用
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from server.app.core.config import get_settings


def verify_mcp_token(sent: str | None) -> tuple[bool, str]:
    """检查 sent token 是否匹配 GEO_MCP_TOKEN。

    返回 (ok, error_detail)。token 未配置时 ok=False, detail="MCP token not configured"。
    空 sent / 不匹配时 ok=False, detail="invalid MCP token"。
    匹配时 ok=True, detail=""。
    """
    configured = get_settings().mcp_token or ""
    if not configured:
        return False, "MCP token not configured"
    if not sent or not hmac.compare_digest(sent, configured):
        return False, "invalid MCP token"
    return True, ""


def require_mcp_token(
    x_mcp_token: str | None = Header(default=None, alias="X-MCP-Token"),
) -> None:
    """FastAPI Depends: 校验 MCP token header。

    用法 (在 router 上挂依赖):
        app.include_router(
            auto_review_router,
            prefix="/api/articles",
            dependencies=[Depends(require_mcp_token)],
        )
    """
    ok, detail = verify_mcp_token(x_mcp_token)
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
```

- [ ] **Step 5: 跑测试确认新 helper + 现有 require_mcp_token 行为不变**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_auth.py server/tests/test_mcp_catalog.py server/tests/test_mcp_connect.py -q
```

Expected: 全 PASS（新 3 用例 + 现有用例无回归）。

- [ ] **Step 6: 提交**

```bash
git add server/app/core/mcp_auth.py server/tests/test_mcp_auth.py
git commit -m "refactor(mcp): 抽出 verify_mcp_token 共享 helper

给 require_mcp_token (Depends) 与即将加的 McpTokenMiddleware (starlette) 共用。
返回 (ok, error_detail) 元组,调用方按自己 transport 决定怎么报错。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 新增 `McpTokenMiddleware`

**Files:**
- Modify: `server/app/core/mcp_auth.py`（追加 class）
- Test: `server/tests/test_mcp_http_mount.py`（新文件，3 个 middleware 用例）

starlette `BaseHTTPMiddleware` 实现，复用 `verify_mcp_token`。挂在 mount 的 sub-app 前，token 失败直接 401 不进 FastMCP app。

- [ ] **Step 1: 创建测试文件 `server/tests/test_mcp_http_mount.py`**

```python
"""MCP HTTP transport mount + 中间件测试。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.app.core.config import get_settings
from server.app.core.mcp_auth import McpTokenMiddleware


def _app_with_middleware() -> FastAPI:
    """裸 FastAPI app + McpTokenMiddleware,挂一个 echo endpoint。"""
    app = FastAPI()
    app.add_middleware(McpTokenMiddleware)

    @app.post("/echo")
    async def echo() -> dict:
        return {"ok": True}

    return app


def test_middleware_blocks_request_without_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "right-token")
    get_settings.cache_clear()
    client = TestClient(_app_with_middleware())
    resp = client.post("/echo")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid MCP token"


def test_middleware_blocks_request_with_wrong_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "right-token")
    get_settings.cache_clear()
    client = TestClient(_app_with_middleware())
    resp = client.post("/echo", headers={"X-MCP-Token": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid MCP token"


def test_middleware_passes_request_with_correct_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "right-token")
    get_settings.cache_clear()
    client = TestClient(_app_with_middleware())
    resp = client.post("/echo", headers={"X-MCP-Token": "right-token"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_middleware_blocks_request_when_no_token_configured(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "")
    get_settings.cache_clear()
    client = TestClient(_app_with_middleware())
    resp = client.post("/echo", headers={"X-MCP-Token": "anything"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "MCP token not configured"
```

- [ ] **Step 2: 跑测试，确认失败（中间件还没写）**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_http_mount.py -q
```

Expected: 4 用例全 FAIL，`ImportError: cannot import name 'McpTokenMiddleware'`。

- [ ] **Step 3: 给 `server/app/core/mcp_auth.py` 追加 middleware class**

在 `require_mcp_token` 下面追加（不动现有 imports / 函数）：

```python
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class McpTokenMiddleware(BaseHTTPMiddleware):
    """检 X-MCP-Token header,失败直接 401,不进入下游 ASGI app。

    用在 mount 的 sub-app 上(starlette ASGI 中间件),给 FastMCP 的 streamable HTTP app 套鉴权。
    与 require_mcp_token(FastAPI Depends) 共享 verify_mcp_token helper。
    """

    async def dispatch(self, request: Request, call_next):
        sent = request.headers.get("X-MCP-Token", "")
        ok, detail = verify_mcp_token(sent)
        if not ok:
            return JSONResponse({"detail": detail}, status_code=401)
        return await call_next(request)
```

- [ ] **Step 4: 跑测试确认全过**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_http_mount.py -q
```

Expected: 4 用例 PASS。

- [ ] **Step 5: 提交**

```bash
git add server/app/core/mcp_auth.py server/tests/test_mcp_http_mount.py
git commit -m "feat(mcp): 新增 McpTokenMiddleware

starlette BaseHTTPMiddleware 实现,给 mount 的 FastMCP sub-app 挂鉴权。
复用 verify_mcp_token,与 require_mcp_token (FastAPI Depends) 行为一致。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `build_http_app()` + stdio main 重构

**Files:**
- Modify: `server/mcp/server.py`
- Modify: `server/tests/test_mcp_entry.py`（如有 token assert 时机相关的测试需适配）

把模块顶部 `_cfg = get_config()` 删掉(否则 GEO 启动时 GEO_MCP_TOKEN 为空就抛错)、token assert 挪入 `main()`,新增 `build_http_app()` 工厂。

- [ ] **Step 1: 先看 `test_mcp_entry.py` 是否依赖现有 token assert 时机**

```bash
docker compose -f docker-compose.dev.yml exec -T app cat server/tests/test_mcp_entry.py
```

如果该文件假设 `import server.mcp.server` 时不会因 token 缺失抛错,改造不影响;反之需要调整。POC 期该文件主要是 docker 守卫,大概率不受影响。

- [ ] **Step 2: 重写 `server/mcp/server.py`**

完整替换为：

```python
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

mcp = FastMCP("geo")

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
```

注意：原模块顶部的 `_cfg = get_config()` 必须删掉，并且 `get_config` 不在顶层 import（挪到 `main()` 内 import）—— 因为 HTTP 路径下 GEO 启动时 `server.mcp.server` 被 import，不能因 token 缺失就崩。

- [ ] **Step 3: 跑现有 MCP 测试**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_entry.py server/tests/test_mcp_catalog.py -q
```

Expected: 全 PASS。如果 `test_mcp_entry.py` 因 token assert 时机变化失败，按其断言调整相应 monkeypatch。

- [ ] **Step 4: 跑 stdio 启动手测，确认双入口都活**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_MCP_TOKEN=test-abc app python -c "from server.mcp.server import mcp, build_http_app; print('tools:', len(mcp._tool_manager._tools)); app = build_http_app(); print('http_app type:', type(app).__name__)"
```

Expected: stdout `tools: 17` + `http_app type: Starlette` (或 ASGI app 类名)。

- [ ] **Step 5: 提交**

```bash
git add server/mcp/server.py
git commit -m "feat(mcp): 新增 build_http_app() + 移除模块顶层 token assert

HTTP transport 路径: server.mcp.server 被 GEO create_app() import 即返回 streamable
HTTP ASGI app, 不再依赖 GEO_MCP_TOKEN 配置(空 token 由 McpTokenMiddleware 401)。
stdio 路径(python -m server.mcp)token assert 挪到 main() 内,行为不变。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `create_app()` mount `/mcp`

**Files:**
- Modify: `server/app/main.py`（在 AI 模型种子块尾、SPA fallback 之前插入 mount）

挂在 SPA fallback 之前是硬约束——SPA fallback 的 `@app.get("/{full_path:path}")` 会兜住任何非 `/api/` 的 GET 请求，包括 `/mcp`。mount 必须在它之前生效。

- [ ] **Step 1: 在 `test_mcp_http_mount.py` 加 mount 集成测试**

`server/tests/test_mcp_http_mount.py` 文件尾追加：

```python
@pytest.mark.mysql
def test_mcp_endpoint_mounted_with_auth(monkeypatch):
    """create_app() 起的 app 里 /mcp 路径存在 + 走 McpTokenMiddleware。"""
    from server.tests.utils import build_test_app  # noqa: PLC0415

    monkeypatch.setenv("GEO_MCP_TOKEN", "real-token")
    get_settings.cache_clear()

    test_app = build_test_app(monkeypatch)
    try:
        client = TestClient(test_app.app)
        # 不带 token POST /mcp/ — middleware 应拦截。
        # streamable HTTP endpoint 路径是 /mcp/(含尾 slash);不带尾 slash 的 /mcp 会
        # 307 redirect 到 /mcp/ 再走 middleware,两条都应 401。
        resp_unauth = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert resp_unauth.status_code == 401
        detail = resp_unauth.json()["detail"]
        assert detail in ("invalid MCP token", "MCP token not configured")
    finally:
        test_app.cleanup()
```

**注**：`build_test_app` 在 `server/tests/utils.py`(GEO 测试基础设施)。`@pytest.mark.mysql` 因 `build_test_app` 要 MySQL schema,无 `GEO_TEST_DATABASE_URL` 时自动 skip,跟其他 mount 路径测试一致。

- [ ] **Step 2: 跑新用例确认失败（mount 还没加）**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_http_mount.py::test_mcp_endpoint_mounted_with_auth -q
```

Expected: FAIL，可能是 404（`/mcp` 不存在）或 200（被 SPA fallback 兜住）。

- [ ] **Step 3: 在 `server/app/main.py` 加 mount**

找到第 401-411 行（AI 模型注册表种子 try/except 块），在它的 `except ...` 块结尾之后、第 413 行 `try:` (`挂载前端静态文件`) 之前插入：

```python
    # ── MCP HTTP transport mount ────────────────────────────────────────────
    # FastMCP 的 streamable_http_app() mount 到 /mcp,用户端 ~/.claude.json 只需配 url + token,
    # 无须本地装 Python / clone 仓库 / 设 PYTHONPATH。鉴权走 McpTokenMiddleware (复用
    # require_mcp_token 的 hmac compare_digest 逻辑)。
    # **必须**挂在 SPA fallback `@app.get("/{full_path:path}")` 之前 —— 否则非 /api/ 路径
    # 全部被 fallback 兜住,/mcp 永远 404。
    try:
        from server.app.core.mcp_auth import McpTokenMiddleware
        from server.mcp.server import build_http_app

        mcp_app = build_http_app()
        mcp_app.add_middleware(McpTokenMiddleware)
        app.mount("/mcp", mcp_app)
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception(
            "MCP HTTP mount failed — /mcp endpoint disabled"
        )
```

包 try/except 的理由跟前面的 startup blocks 一样：失败只记日志、不阻塞 GEO API 启动。

- [ ] **Step 4: 跑新用例确认通过**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_http_mount.py -q
```

Expected: 全 5 用例 PASS。

- [ ] **Step 5: 手动 curl 验证 happy path（dev 容器内）**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_MCP_TOKEN=test-abc app curl -X POST http://127.0.0.1:8000/mcp/ \
  -H "X-MCP-Token: test-abc" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl-test","version":"0"}}}' \
  -i
```

Expected: HTTP 200,返回 MCP `initialize` 握手响应（含 `protocolVersion`、`capabilities`、`serverInfo`）。如果返回 400 / "Missing session ID" 之类，看 FastMCP 文档配 stateless mode 或在 endpoint 路径后加 `/messages` 子路径。

> **执行 hint**：如果 FastMCP HTTP transport 要求 `Mcp-Session-Id` header，初次 `initialize` 不带 session id 通常合法（server 会在 `initialized` notification 之后建 session）。具体行为依 mcp 包版本,如果手测卡在协议握手,先 `pip show mcp` 看版本、查对应 README streamable HTTP 节。

- [ ] **Step 6: 跑全套 mcp 测试 + lint，确认无回归**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/ -q -k mcp
docker compose -f docker-compose.dev.yml exec -T app ruff check server/
docker compose -f docker-compose.dev.yml exec -T app ruff format --check server/
```

Expected: 全 PASS / ruff 全绿。

- [ ] **Step 7: 提交（C1 后端最终 commit）**

```bash
git add server/app/main.py server/tests/test_mcp_http_mount.py
git commit -m "feat(mcp): create_app() mount FastMCP streamable HTTP app 到 /mcp

用户端 ~/.claude.json 用 {transport: 'http', url: '<base>/mcp', headers: {X-MCP-Token}}
即可接入,无需本地装 Python。stdio 入口 (python -m server.mcp) 保留作为可选 dev 路径。

mount 必须在 SPA fallback 之前,挂 McpTokenMiddleware 兼容鉴权语义。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 后端 mypy + 全套测试最终验证

**Files:** 无新文件，全套验证。

C1 收尾的"全绿"门禁。

- [ ] **Step 1: mypy**

```bash
docker compose -f docker-compose.dev.yml exec -T app pip install mypy >/dev/null 2>&1 || true
docker compose -f docker-compose.dev.yml exec -T app mypy server/app
```

Expected: 无新增 error。`server/app/core/mcp_auth.py` 的 `tuple[bool, str]` 在 Python 3.9+ 通过。

- [ ] **Step 2: 全套后端测试**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/ -q
```

Expected: 全绿，无 skip 超出已知 `@pytest.mark.mysql` 的范围。

- [ ] **Step 3: 如有失败，回 Phase 1 调查**

不要尝试 quick fix。按 systematic-debugging 原则逐层取证后修。

---

## Task 8: 前端 `buildHttpConfigJson` + 重命名 stdio 模板

**Files:**
- Modify: `web/src/features/mcp/McpConnectWorkspace.tsx`（顶部 `buildConfigJson` 函数）

- [ ] **Step 1: 重写顶部 helper（line 18-33）**

把现有 `buildConfigJson` 替换为两个函数：

```ts
function buildHttpConfigJson(suggestedBaseUrl: string): string {
  const base = suggestedBaseUrl || "http://127.0.0.1:8000";
  const template = {
    mcpServers: {
      geo: {
        transport: "http",
        url: `${base}/mcp`,
        headers: { "X-MCP-Token": "<PASTE_YOUR_TOKEN_HERE>" },
      },
    },
  };
  return JSON.stringify(template, null, 2);
}

function buildStdioConfigJson(suggestedBaseUrl: string): string {
  const template = {
    mcpServers: {
      geo: {
        command: "python",
        args: ["-m", "server.mcp"],
        env: {
          GEO_MCP_TOKEN: "<PASTE_YOUR_TOKEN_HERE>",
          GEO_API_BASE_URL: suggestedBaseUrl || "http://127.0.0.1:8000",
          PYTHONPATH: "<PATH_TO_YOUR_LOCAL_geo-collab_CLONE>",
        },
      },
    },
  };
  return JSON.stringify(template, null, 2);
}
```

- [ ] **Step 2: 在组件内加 transport state**

找到组件内部 `const [copied, setCopied] = useState(false);` 一行下方（约 line 52），插入：

```ts
  // Section ③ — transport (HTTP 推荐 / stdio 可选)
  const [transport, setTransport] = useState<"http" | "stdio">("http");
```

- [ ] **Step 3: 把 `configJson` useMemo 改成根据 transport 选模板**

找到现有的 `configJson` useMemo（约 line 79）：
```ts
  const configJson = useMemo(() => buildConfigJson(suggestedBaseUrl), [suggestedBaseUrl]);
```

替换为：
```ts
  const configJson = useMemo(
    () =>
      transport === "http"
        ? buildHttpConfigJson(suggestedBaseUrl)
        : buildStdioConfigJson(suggestedBaseUrl),
    [suggestedBaseUrl, transport],
  );
```

- [ ] **Step 4: typecheck 跑**

```bash
docker compose -f docker-compose.dev.yml exec -T app pnpm --filter @geo/web typecheck
```

Expected: 无 type error。

- [ ] **Step 5: 提交**

```bash
git add web/src/features/mcp/McpConnectWorkspace.tsx
git commit -m "feat(web): 「MCP 接入」tab 模板按 transport 切换

buildHttpConfigJson 输出 streamable HTTP 配置 (transport/url/headers)。
buildStdioConfigJson = 原 buildConfigJson 重命名。
默认 transport='http',下一 commit 加 UI toggle。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: 段 ③ transport toggle UI + stdio 警示

**Files:**
- Modify: `web/src/features/mcp/McpConnectWorkspace.tsx`（段 ③ JSX）

在段 ③ "客户端配置" 标题旁加 toggle、stdio 模式下方加红字 hint、根据 transport 改下方 3 条要点提示。

- [ ] **Step 1: 段 ③ JSX 顶部 toolbar 加 toggle**

找到段 ③ 的 `<h2 style={{ margin: 0 }}>客户端配置</h2>` 那一行（约 line 283），把它和右侧"复制 JSON"按钮包裹的 flex 容器内部改成三段(标题 / toggle / 复制按钮)：

替换原来的 `<h2 style={{ margin: 0 }}>客户端配置</h2>` 那一段及其右侧 button 容器,布局改成:

```tsx
            <h2 style={{ margin: 0 }}>客户端配置</h2>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <div style={{ display: "inline-flex", borderRadius: 8, background: "var(--bg-2)", padding: 2 }}>
                <button
                  type="button"
                  onClick={() => setTransport("http")}
                  style={{
                    padding: "4px 10px",
                    fontSize: 12,
                    borderRadius: 6,
                    border: "none",
                    background: transport === "http" ? "var(--accent)" : "transparent",
                    color: transport === "http" ? "var(--bg)" : "var(--fg-2)",
                    cursor: "pointer",
                  }}
                >
                  HTTP（推荐）
                </button>
                <button
                  type="button"
                  onClick={() => setTransport("stdio")}
                  style={{
                    padding: "4px 10px",
                    fontSize: 12,
                    borderRadius: 6,
                    border: "none",
                    background: transport === "stdio" ? "var(--accent)" : "transparent",
                    color: transport === "stdio" ? "var(--bg)" : "var(--fg-2)",
                    cursor: "pointer",
                  }}
                >
                  stdio（本地 dev）
                </button>
              </div>
              <button
                type="button"
                className="secondaryButton"
                onClick={() => void onCopy()}
                disabled={!status}
              >
                {copied ? (
                  <>
                    <CheckCircle2 size={14} /> 已复制
                  </>
                ) : (
                  <>
                    <Copy size={14} /> 复制 JSON
                  </>
                )}
              </button>
            </div>
```

- [ ] **Step 2: 改 `<pre>` 上面那行 "粘贴以下片段" 提示**

把原来的 `<p>在你的机器上编辑 ~/.claude.json，粘贴以下片段：</p>` 改成基于 transport 切换的两条文案。找到约 line 302-304 替换为：

```tsx
          <p style={{ color: "var(--fg-2)", fontSize: 13, marginBottom: 10 }}>
            {transport === "http"
              ? "在你的机器上编辑 ~/.claude.json，粘贴以下片段（无需本机装 Python）："
              : "在你的机器上编辑 ~/.claude.json，粘贴以下片段（需要本机装 Python + clone 仓库）："}
          </p>
```

- [ ] **Step 3: 替换 `<pre>` 下方 3 条要点提示**

找到 `<ul style={{ marginTop: 14, ... }}>` 那段（约 line 312-326），整段替换为：

```tsx
          {transport === "http" ? (
            <ul style={{ marginTop: 14, paddingLeft: 20, listStyle: "disc", lineHeight: 1.9, fontSize: 13, color: "var(--fg-2)" }}>
              <li>
                <code style={inlineCode}>url</code>：自动填了你浏览器看到的域名;Claude Code
                跑在容器里时把域名换成 <code style={inlineCode}>http://host.docker.internal:8000</code>。
              </li>
              <li>
                <code style={inlineCode}>X-MCP-Token</code>：找 admin 获取。
              </li>
              <li>
                需要 Nginx 反代时，<code style={inlineCode}>location /mcp</code> 块必须加{" "}
                <code style={inlineCode}>proxy_buffering off; proxy_request_buffering off;</code>
                （streamable HTTP 依赖 chunked,默认 buffering 会卡住 stream）。
              </li>
            </ul>
          ) : (
            <>
              <div
                style={{
                  marginTop: 12,
                  color: "var(--red)",
                  background: "var(--red-soft)",
                  border: "1px solid rgba(248,113,113,0.3)",
                  padding: "10px 14px",
                  borderRadius: 10,
                  fontSize: 13,
                  lineHeight: 1.7,
                }}
              >
                <AlertTriangle size={14} style={{ verticalAlign: "-2px", marginRight: 6 }} />
                stdio 模式需要你的机器有 Python + clone 仓库,仅推荐本机开发 / air-gap 场景。
                日常使用请切回 HTTP。
              </div>
              <ul style={{ marginTop: 14, paddingLeft: 20, listStyle: "disc", lineHeight: 1.9, fontSize: 13, color: "var(--fg-2)" }}>
                <li>
                  <code style={inlineCode}>GEO_MCP_TOKEN</code>:找 admin 获取。
                </li>
                <li>
                  <code style={inlineCode}>GEO_API_BASE_URL</code>:默认填你浏览器看到的域名;Claude Code
                  跑在容器里访问宿主时改成{" "}
                  <code style={inlineCode}>http://host.docker.internal:8000</code>。
                </li>
                <li>
                  <code style={inlineCode}>PYTHONPATH</code>:在自己机器上{" "}
                  <code style={inlineCode}>
                    git clone https://github.com/geo-ihuanlegame/geo-collab.git
                  </code>{" "}
                  后填克隆出来的绝对路径;Windows 注意双反斜杠。
                </li>
              </ul>
            </>
          )}
```

- [ ] **Step 4: typecheck + build**

```bash
docker compose -f docker-compose.dev.yml exec -T app pnpm --filter @geo/web typecheck
docker compose -f docker-compose.dev.yml exec -T app pnpm --filter @geo/web build
```

Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add web/src/features/mcp/McpConnectWorkspace.tsx
git commit -m "feat(web): 「MCP 接入」tab 段 ③ 加 transport toggle (HTTP/stdio)

默认 HTTP, 切 stdio 时下方加红字警示。HTTP 模式的要点提示去掉 PYTHONPATH,
新增 Nginx proxy_buffering off 提醒。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: 段 ② endpoint 提示行

**Files:**
- Modify: `web/src/features/mcp/McpConnectWorkspace.tsx`（段 ②，约 line 242-245）

在"建议 base_url"下面加一行"MCP endpoint: {base}/mcp"，方便用户直接确认 URL 形态。

- [ ] **Step 1: 改段 ② "建议 base_url" 那一块**

找到约 line 242-245 的代码：
```tsx
              <div style={{ fontSize: 13, color: "var(--fg-2)" }}>
                <span style={{ marginRight: 8 }}>建议 base_url：</span>
                <code style={inlineCode}>{status.suggested_base_url}</code>
              </div>
```

替换为：
```tsx
              <div style={{ fontSize: 13, color: "var(--fg-2)" }}>
                <span style={{ marginRight: 8 }}>建议 base_url：</span>
                <code style={inlineCode}>{status.suggested_base_url}</code>
              </div>
              <div style={{ fontSize: 13, color: "var(--fg-2)" }}>
                <span style={{ marginRight: 8 }}>MCP endpoint：</span>
                <code style={inlineCode}>{status.suggested_base_url}/mcp</code>
              </div>
```

- [ ] **Step 2: typecheck + build**

```bash
docker compose -f docker-compose.dev.yml exec -T app pnpm --filter @geo/web typecheck && docker compose -f docker-compose.dev.yml exec -T app pnpm --filter @geo/web build
```

Expected: 全绿。

- [ ] **Step 3: 提交**

```bash
git add web/src/features/mcp/McpConnectWorkspace.tsx
git commit -m "feat(web): 段 ② 显示 MCP endpoint URL (base + /mcp)

让用户能直接看到 Claude Code 应该打哪个 URL,而不是只看 base_url 自己拼。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: 重写 `docs/mcp-setup-notes.md`

**Files:**
- Modify: `docs/mcp-setup-notes.md`（完整重写）

HTTP 路径提到最前面、stdio 折叠到"高级"、客户端章节砍掉 git clone 系列步骤、新增 Nginx buffering 红字。

- [ ] **Step 1: 用以下内容完整覆盖 `docs/mcp-setup-notes.md`**

```markdown
# GEO MCP Server · 接入指引

POC 期 MCP server 跟 GEO FastAPI app **同进程 mount**(路径 `/mcp`),用户端不需要装 Python / clone 仓库,`~/.claude.json` 填 url + token 即可。

## 准备

1. **生成 MCP token**：
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
```

- [ ] **Step 2: 浏览器手测前端「MCP 接入」tab,核对文档与 UI 的实际选项一致**

打开 GEO 前端 → 「MCP 接入」tab → 段 ③ 切 HTTP / stdio,确认模板内容跟文档 §配置 Claude Code / §高级 一致。

- [ ] **Step 3: 提交**

```bash
git add docs/mcp-setup-notes.md
git commit -m "docs(mcp): 重写 setup notes — HTTP 推荐, stdio 折叠为高级

新增 Nginx proxy_buffering off 必配说明、HTTP / stdio 双手测 curl,
诊断段加 -32000 (无本地 Python) 案例。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: 同步 `CLAUDE.md`「MCP Server」段

**Files:**
- Modify: `CLAUDE.md`（"## MCP Server" 节）

- [ ] **Step 1: 看现状,找出要改的段落**

```bash
grep -n "MCP Server" CLAUDE.md
grep -n "command\": \"python" CLAUDE.md
grep -n "PYTHONPATH" CLAUDE.md
```

找到「## MCP Server (Claude Code Loop 调用入口)」节、"### 启动方式" 子段示例 JSON、"加新 tool 的步骤" 4 步。

- [ ] **Step 2: 替换"启动方式"子段**

找到 `### 启动方式` 之后到 `### 鉴权边界` 之间的内容,改成:

````markdown
### 启动方式

POC 期 MCP server 跟 GEO FastAPI **同进程 mount** 在 `/mcp` 路径(不再是 stdio 独立进程):

`~/.claude.json`:

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

mount 由 `server.app.main:create_app()` 在 SPA fallback 之前完成。鉴权走 `server.app.core.mcp_auth.McpTokenMiddleware` (复用 `verify_mcp_token` 与 `require_mcp_token` 共享一套 hmac compare_digest)。

stdio 入口 (`python -m server.mcp`) **保留**作为本机 dev / air-gap 路径,token assert 在 `main()` 内,与 HTTP 路径解耦。

详细配置见 `docs/mcp-setup-notes.md`。
````

- [ ] **Step 3: 改"加新 tool 的步骤"第 4 步**

找到 "加新 tool 的步骤" 那段第 4 步 `重启 Claude Code → `/mcp` 验证`,改成:

```markdown
4. 重启 GEO 后端进程(新 tool 注册在后端) → 重启 Claude Code → `/mcp` 验证
```

- [ ] **Step 4: 鉴权边界段同步**

找到 "鉴权边界" 段落,把现有 `dependencies=[Depends(require_mcp_token)]` 解释末尾追加一句:

```markdown
- 同进程 mount 的 FastMCP HTTP sub-app (`/mcp`) 不走 sub-router、用 `McpTokenMiddleware` 实现等价鉴权,语义与 `require_mcp_token` 一致(共享 `verify_mcp_token` helper)。
```

- [ ] **Step 5: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md「MCP Server」段同步 HTTP transport

启动方式改 HTTP mount;加新 tool 第 4 步改成重启 GEO 后端;
鉴权边界段说明 McpTokenMiddleware 与 require_mcp_token 共享 verify_mcp_token。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: 端到端真实接入验收

**Files:** 无新文件。

人工验收。无法自动化（涉及 Windows host 上的 Claude Code 客户端）。

- [ ] **Step 1: 重启 GEO 后端进程**

```bash
docker compose -f docker-compose.dev.yml restart app
```

等 healthcheck 绿。看 GEO 启动日志,确认无 `MCP HTTP mount failed` exception。

- [ ] **Step 2: 浏览器开 GEO 前端「MCP 接入」tab**

- 段 ② 应显示 "✓ 服务端 token 已配置" + base_url + **MCP endpoint 行**(`<base>/mcp`)
- 段 ③ 默认显示 HTTP 模板,toggle 切 stdio 显示原 stdio 模板 + 红色警示
- 段 ③ 复制 HTTP JSON,粘到 host 上的 `~/.claude.json`,替换 `<PASTE_YOUR_TOKEN_HERE>` 为实际 token
- 段 ④ 粘 token 点测试,应返回 "✓ token 正确,网络可达"

- [ ] **Step 3: Windows host 重启 Claude Code**

- 输入 `/mcp` 应显示 `geo: connected` + 17 个工具
- 调 `list_articles(limit=5)` 应返回 GEO 实际数据
- 调 `list_question_pools()` 验证 catalog 类工具

- [ ] **Step 4: stdio 路径回归(dev 容器内)**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_MCP_TOKEN=<token> app python -c "from server.mcp.server import mcp, main; print('tools:', len(mcp._tool_manager._tools))"
```

Expected: `tools: 17`。

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_MCP_TOKEN=<token> app timeout 3 python -m server.mcp || true
```

Expected: stdio 进程能起、阻塞等 stdin 后 3s 被 timeout kill (退出码 124),没有 Python traceback。

- [ ] **Step 5: 关掉 GEO_MCP_TOKEN 看 401 路径**

```bash
docker compose -f docker-compose.dev.yml exec -T -e GEO_MCP_TOKEN= app curl -X POST http://127.0.0.1:8000/mcp/ -H "X-MCP-Token: anything" -i 2>&1 | head -20
```

Expected: HTTP 401, `{"detail":"MCP token not configured"}`。

- [ ] **Step 6: 收尾。所有手测项打钩后,无新文件变更,不需要 commit。**

如果某项失败:回到对应 task,按 systematic-debugging 流程定位根因。

---

## Self-Review Checklist

写完 plan 后过一遍,确认:

- [ ] 每个 task 都有具体的 file path + 完整 code block(无 "TBD" / "类似 Task N")
- [ ] Spec §2.2 改动总览表的所有文件都被某个 task 覆盖:
  - `server/mcp/server.py` → Task 5 ✓
  - `server/mcp/config.py` → Task 1 ✓
  - `server/mcp/tools/{action,catalog,meta}.py` → Task 2 ✓
  - `server/app/core/mcp_auth.py` → Task 3 (`verify_mcp_token`) + Task 4 (`McpTokenMiddleware`) ✓
  - `server/app/main.py` → Task 6 ✓
  - `web/src/features/mcp/McpConnectWorkspace.tsx` → Task 8 / 9 / 10 ✓
  - `docs/mcp-setup-notes.md` → Task 11 ✓
  - `CLAUDE.md` → Task 12 ✓
  - `server/tests/test_mcp_http_mount.py` → Task 4 (middleware) + Task 6 (mount) ✓
- [ ] Spec §5.1 列的 4 + 1 测试用例都被实现:
  - `test_mcp_endpoint_requires_token` → Task 4 `test_middleware_blocks_request_without_token` ✓
  - `test_mcp_endpoint_invalid_token` → Task 4 `test_middleware_blocks_request_with_wrong_token` ✓
  - `test_mcp_endpoint_initialize` → Task 6 Step 5 手动 curl(自动化测试简化为 Task 6 `test_mcp_endpoint_mounted_with_auth` 验 mount 存在 + 鉴权链路)。**注**: spec 原本要求自动化 happy path 测试,但 FastMCP streamable HTTP 协议 session 行为复杂,plan 退化为「自动测拦截 + 手动测握手」组合,降低执行风险
  - `test_mcp_no_token_configured` → Task 4 `test_middleware_blocks_request_when_no_token_configured` ✓
- [ ] Spec §6 两条独立 commit 拓扑落到 plan:
  - C1 后端 = Task 1-7 各自 commit (实际更细,但全归在 C1 范围内,任一 revert 不破前端)
  - C2 前端+文档 = Task 8-12 各自 commit
- [ ] 风险点 (spec §7) 均有缓解动作:
  - 双实例 bug → Task 5 `_assert_tools_registered` ✓
  - token middleware 穿透 → Task 4 测试 + Task 6 mount 顺序 ✓
  - Nginx buffering → Task 11 docs ✓
  - `GEO_MCP_INTERNAL_API_URL` 漏配 → Task 1 fallback ✓
  - stdio 老配置失效 → Task 5 main 保留 + Task 9 toggle ✓
- [ ] 类型 / 方法名跨 task 一致(`build_http_app` / `McpTokenMiddleware` / `verify_mcp_token` / `_assert_tools_registered`)

✅ 自审 OK,plan 可执行。
