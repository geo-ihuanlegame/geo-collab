# Claude Code Loop + GEO MCP · Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 7 天 POC——让 Claude Code 作为 Loop 大脑，通过新建的 GEO MCP server 调用现有 GEO 能力（生文 / 配图 / 分发）+ 三块新能力（自动审核 / 评估器 / 反馈回流），跑通生文 Loop 和发文 Loop，关键节点推飞书 webhook。

**Architecture:** GEO 后端 (FastAPI/SQLAlchemy/MySQL) 新增两个薄业务模块（auto_review / performance）+ 6 个新 API。独立的 `server/mcp/` Python 进程跑 FastMCP stdio server，把现有 + 新增 GEO API 包装成 ~15 个 atomic tools。Claude Code 本机通过 `~/.claude.json` 的 mcpServers 配置自动 spawn MCP server，用 `/loop` + Loop 配方 markdown 跑业务流程。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy / Alembic / MySQL 8 / `mcp[cli]` (FastMCP) / `httpx` / `litellm` / `pytest`

**Spec:** `docs/superpowers/specs/2026-06-18-claude-code-loop-with-geo-mcp-design.md`

**Branch:** `feat/geo-mcp-loop`（已从 main 切出，2 commit ahead，含本 plan 上游的 spec）

---

## File Structure

### 新增（按 Phase 顺序）

| File | Phase | 责任 |
|------|-------|------|
| `requirements.txt`（追加） | D1 | 加 `mcp[cli]>=1.0` 依赖 |
| `server/app/core/mcp_auth.py` | D1 | FastAPI dependency: 校验 `X-MCP-Token` header |
| `server/mcp/__init__.py` | D1 | 空文件 |
| `server/mcp/server.py` | D1 | FastMCP stdio server 入口 |
| `server/mcp/config.py` | D1 | 读 `GEO_MCP_TOKEN` / `GEO_API_BASE_URL` |
| `server/mcp/http_client.py` | D1 | 共享 httpx client，自动带 token header |
| `server/mcp/tools/__init__.py` | D1 | 注册三组 tool 到 FastMCP 实例 |
| `server/mcp/tools/catalog.py` | D1+D2 | 7 个只读 tool |
| `server/mcp/tools/action.py` | D2+D3+D4+D5 | 6 个写操作 tool |
| `server/mcp/tools/meta.py` | D3+D6 | 4 个评估/回流 tool |
| `server/app/modules/auto_review/__init__.py` | D3 | 空 |
| `server/app/modules/auto_review/models.py` | D3 | `AutoReviewDecision` ORM |
| `server/app/modules/auto_review/schemas.py` | D3 | Pydantic in/out |
| `server/app/modules/auto_review/service.py` | D3 | 批量评分 + decision 持久化 纯函数 |
| `server/app/modules/auto_review/router.py` | D3 | `POST /api/articles/score` + `POST /api/articles/{id}/auto-review` |
| `server/app/modules/performance/__init__.py` | D6 | 空 |
| `server/app/modules/performance/service.py` | D6 | 聚合 metrics 纯函数 |
| `server/app/modules/performance/router.py` | D6 | template/account performance + publish metrics 回写 |
| `server/alembic/versions/0047_auto_review_decisions.py` | D3 | 新表 + 给 `articles` 加 `metrics` JSON 列 |
| `claude-loops/generation-loop.md` | D4 | 生文 Loop 配方 |
| `claude-loops/distribute-loop.md` | D5 | 发文 Loop 配方 |
| `claude-loops/weekly-report-loop.md` | D6 | 评估器周报 Loop 配方 |
| `server/tests/test_auto_review.py` | D3 | router + service |
| `server/tests/test_performance.py` | D6 | router + service |
| `server/tests/test_mcp_auth.py` | D1 | 中间件 |
| `server/tests/test_mcp_http_client.py` | D1 | httpx mock |

### 修改

| File | Phase | 变更 |
|------|-------|------|
| `server/app/main.py` | D3+D4+D6 | 注册 auto_review_router / performance_router / feishu-notify endpoint |
| `server/app/core/config.py` | D1 | 加 `mcp_token: str = ""` + `mcp_api_base_url: str` |
| `server/app/shared/feishu.py` | D4 | 抽出通用 `send_text(title, message, level)` 给 system_router 调 |
| `server/app/modules/system/system_router.py` | D4 | 加 `POST /api/system/feishu-notify` |
| `server/app/modules/ai_generation/router.py` 或新建 | D2 | 加 `POST /api/generation/compose-once`（直调 article_writer.generate_article_from_prompt） |
| `server/app/modules/articles/models.py` | D3 | `Article` 加 `metrics: JSON` 列（同迁移 0047） |
| `CLAUDE.md` | D7 | 加 `server/mcp/` 章节 + `auto_review` / `performance` / `compose-once` 说明 |

---

## Phase 1 (D1) — MCP 骨架 + 鉴权 + 3 个 Catalog tool

---

### Task 1: 依赖 + 配置

**Files:**
- Modify: `requirements.txt`
- Modify: `server/app/core/config.py:148` (在 model_config 上方)
- Test: 启动 conftest 实测 settings 加载

- [ ] **Step 1: 追加依赖到 requirements.txt**

打开 `requirements.txt`，找到合适位置（建议放在 `litellm` 附近）追加一行：

```
mcp[cli]>=1.0
```

- [ ] **Step 2: 在 config.py 加 MCP 配置项**

在 `server/app/core/config.py` 找到 `model_config = SettingsConfigDict(env_prefix="GEO_", ...)` 那一行（约 149 行），在它**之前**插入：

```python
    # MCP server（Claude Code 通过 stdio spawn 调用 GEO 能力）
    mcp_token: str = ""  # GEO_MCP_TOKEN（独立 service token，与 user JWT 隔离；空=禁用 MCP）
    mcp_api_base_url: str = "http://127.0.0.1:8000"  # GEO_MCP_API_BASE_URL
```

- [ ] **Step 3: 在 .env 加示例**

如果项目有 `.env.example`（没有就跳过），追加：

```
# MCP server（POC 期生成方式：openssl rand -hex 32）
GEO_MCP_TOKEN=changeme-generate-with-openssl-rand-hex-32
GEO_MCP_API_BASE_URL=http://127.0.0.1:8000
```

- [ ] **Step 4: 装依赖**

Run: `pip install -r requirements.txt`
Expected: `mcp` 包成功安装、`pip show mcp` 能看到版本

- [ ] **Step 5: 验证 settings 加载**

Run: `python -c "from server.app.core.config import get_settings; s = get_settings(); print(s.mcp_token, s.mcp_api_base_url)"`
Expected: 输出空字符串 + `http://127.0.0.1:8000`（环境变量未设时取默认值）

- [ ] **Step 6: Commit**

```bash
git add requirements.txt server/app/core/config.py
git commit -m "feat(mcp): 加 mcp[cli] 依赖与 GEO_MCP_TOKEN/GEO_MCP_API_BASE_URL 配置"
```

---

### Task 2: GEO 后端 MCP token 校验中间件

**Files:**
- Create: `server/app/core/mcp_auth.py`
- Create: `server/tests/test_mcp_auth.py`

- [ ] **Step 1: 写失败测试**

`server/tests/test_mcp_auth.py`:

```python
import pytest
from fastapi import FastAPI, Depends, HTTPException
from fastapi.testclient import TestClient
from server.app.core.mcp_auth import require_mcp_token


def _app():
    app = FastAPI()

    @app.get("/probe", dependencies=[Depends(require_mcp_token)])
    def probe():
        return {"ok": True}

    return TestClient(app)


def test_missing_token_returns_401(monkeypatch):
    from server.app.core import config
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-abc")
    config.get_settings.cache_clear()
    r = _app().get("/probe")
    assert r.status_code == 401


def test_wrong_token_returns_401(monkeypatch):
    from server.app.core import config
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-abc")
    config.get_settings.cache_clear()
    r = _app().get("/probe", headers={"X-MCP-Token": "wrong"})
    assert r.status_code == 401


def test_correct_token_passes(monkeypatch):
    from server.app.core import config
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-abc")
    config.get_settings.cache_clear()
    r = _app().get("/probe", headers={"X-MCP-Token": "secret-abc"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_empty_configured_token_rejects_all(monkeypatch):
    from server.app.core import config
    monkeypatch.setenv("GEO_MCP_TOKEN", "")
    config.get_settings.cache_clear()
    r = _app().get("/probe", headers={"X-MCP-Token": ""})
    assert r.status_code == 401  # 空配置等于禁用 MCP，绝不能放过任何请求
```

- [ ] **Step 2: 跑测试验证失败**

Run: `pytest server/tests/test_mcp_auth.py -v`
Expected: FAIL — `ImportError: cannot import name 'require_mcp_token'`

- [ ] **Step 3: 写实现**

`server/app/core/mcp_auth.py`:

```python
"""MCP token 鉴权依赖。

独立于 user JWT 的 service token：
- 空配置 (`GEO_MCP_TOKEN=""`) 视作"MCP 已禁用"，任何带 token 的请求都返回 401。
- 配置非空时，校验请求 header `X-MCP-Token` 是否匹配。
- 使用 `hmac.compare_digest` 做常数时间比较，避免 timing attack。
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from server.app.core.config import get_settings


def require_mcp_token(
    x_mcp_token: str | None = Header(default=None, alias="X-MCP-Token"),
) -> None:
    """FastAPI Depends：校验 MCP token header。

    用法（在 router 上挂依赖）：
        app.include_router(
            auto_review_router,
            prefix="/api/articles",
            dependencies=[Depends(require_mcp_token)],
        )
    """
    configured = get_settings().mcp_token or ""
    if not configured:
        # 空配置 = MCP 禁用，所有请求一律拒绝
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MCP token not configured",
        )
    if not x_mcp_token or not hmac.compare_digest(x_mcp_token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid MCP token",
        )
```

- [ ] **Step 4: 跑测试验证通过**

Run: `pytest server/tests/test_mcp_auth.py -v`
Expected: 4 个测试全 PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/core/mcp_auth.py server/tests/test_mcp_auth.py
git commit -m "feat(mcp): 加 require_mcp_token 鉴权依赖（hmac compare_digest）"
```

---

### Task 3: MCP server 骨架（FastMCP stdio 起来）

**Files:**
- Create: `server/mcp/__init__.py`
- Create: `server/mcp/config.py`
- Create: `server/mcp/server.py`

- [ ] **Step 1: 建空 __init__**

```bash
# Windows PowerShell
New-Item -ItemType File server/mcp/__init__.py
New-Item -ItemType Directory server/mcp/tools
New-Item -ItemType File server/mcp/tools/__init__.py
```

或 bash：

```bash
mkdir -p server/mcp/tools && touch server/mcp/__init__.py server/mcp/tools/__init__.py
```

- [ ] **Step 2: 写 server/mcp/config.py**

```python
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
        self.timeout_seconds = float(os.environ.get("GEO_MCP_TIMEOUT_SECONDS", "30"))

    def assert_ready(self) -> None:
        if not self.token:
            raise RuntimeError(
                "GEO_MCP_TOKEN is empty. Set it in Claude Code mcpServers.geo.env."
            )


def get_config() -> McpConfig:
    cfg = McpConfig()
    cfg.assert_ready()
    return cfg
```

- [ ] **Step 3: 写 server/mcp/server.py（FastMCP 骨架）**

```python
"""GEO MCP Server — FastMCP stdio 入口。

启动方式（POC 期由 Claude Code 自动 spawn，开发时手测可以）：
    python -m server.mcp.server

工具按三组分文件注册：
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
from server.mcp.tools import catalog as _catalog  # noqa: F401,E402
from server.mcp.tools import action as _action  # noqa: F401,E402
from server.mcp.tools import meta as _meta  # noqa: F401,E402


def main() -> None:
    """stdio 模式入口（被 Claude Code spawn 时用）。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 写空 tools 占位**

`server/mcp/tools/catalog.py`:

```python
"""只读 Catalog 类工具。list_* / get_*，调 GEO 后端 GET 接口。"""

from __future__ import annotations

# 各 tool 会通过 @mcp.tool 注册到 server.mcp.server 的全局 mcp 实例
# 这里先空着，Task 5 开始填
```

`server/mcp/tools/action.py` 和 `server/mcp/tools/meta.py` 同样建空文件，标注用途注释。

- [ ] **Step 5: 验证 server 能跑起来**

Run:
```bash
GEO_MCP_TOKEN=test-token-abc python -m server.mcp.server
```

Windows PowerShell:
```powershell
$env:GEO_MCP_TOKEN="test-token-abc"; python -m server.mcp.server
```

Expected: 进程启动后阻塞在 stdin（等 MCP client 发协议消息）。Ctrl-C 退出。

- [ ] **Step 6: Commit**

```bash
git add server/mcp/__init__.py server/mcp/config.py server/mcp/server.py server/mcp/tools/__init__.py server/mcp/tools/catalog.py server/mcp/tools/action.py server/mcp/tools/meta.py
git commit -m "feat(mcp): MCP server 骨架（FastMCP stdio + 三组 tool 占位）"
```

---

### Task 4: MCP HTTP client（共享调 GEO 的 client）

**Files:**
- Create: `server/mcp/http_client.py`
- Create: `server/tests/test_mcp_http_client.py`

- [ ] **Step 1: 写失败测试**

```python
# server/tests/test_mcp_http_client.py
import httpx
import pytest

from server.mcp.http_client import GeoApiClient, ApiError


def test_get_attaches_token_header(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-xyz")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True, "data": [1, 2]})

    transport = httpx.MockTransport(handler)
    client = GeoApiClient(base_url="http://test", transport=transport, token="secret-xyz")
    resp = client.get("/api/articles", params={"limit": 5})

    assert resp == {"ok": True, "data": [1, 2]}
    assert captured["headers"]["x-mcp-token"] == "secret-xyz"


def test_get_returns_error_on_4xx(monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"detail": "bad request"})

    client = GeoApiClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        token="t",
    )
    with pytest.raises(ApiError) as exc:
        client.get("/api/articles")
    assert "400" in str(exc.value)
    assert "bad request" in str(exc.value)


def test_post_json_body_and_header(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = request.read().decode()
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"ok": True})

    client = GeoApiClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        token="t",
    )
    client.post("/api/articles/score", json={"article_ids": [1, 2]})

    assert '"article_ids": [1, 2]' in captured["body"].replace(" ", "").replace("[", "[ ").replace("]", " ]") or '"article_ids":[1,2]' in captured["body"]
    assert "application/json" in captured["content_type"]
```

- [ ] **Step 2: 跑测试验证失败**

Run: `pytest server/tests/test_mcp_http_client.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 写实现**

`server/mcp/http_client.py`:

```python
"""GEO API HTTP client (sync).

设计要点：
- 默认走 httpx.Client（FastMCP tool handler 是 sync 的，async client 反而麻烦）
- 自动在所有请求注入 `X-MCP-Token` header
- 4xx/5xx 一律抛 ApiError(描述包含 method/path/status/detail)，让 tool handler 转成
  {ok: false, error: ...} 顶层封装返回给 LLM
- 大对象（如 article content）按 GEO 现有 schema 返回，不做裁剪——LLM 决定要不要二次读
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """非 2xx 响应或网络错误。"""


class GeoApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"X-MCP-Token": token},
        )

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, *, json: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=json)

    def patch(self, path: str, *, json: dict[str, Any] | None = None) -> Any:
        return self._request("PATCH", path, json=json)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        try:
            resp = self._client.request(method, url, params=params, json=json)
        except httpx.RequestError as exc:
            raise ApiError(f"{method} {path}: network error: {exc}") from exc
        if resp.status_code >= 400:
            detail = _extract_detail(resp)
            raise ApiError(f"{method} {path}: {resp.status_code} {detail}")
        try:
            return resp.json()
        except ValueError:
            return resp.text


def _extract_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("message") or data)[:300]
    return str(data)[:300]
```

- [ ] **Step 4: 跑测试验证通过**

Run: `pytest server/tests/test_mcp_http_client.py -v`
Expected: 3 个测试全 PASS

- [ ] **Step 5: Commit**

```bash
git add server/mcp/http_client.py server/tests/test_mcp_http_client.py
git commit -m "feat(mcp): GeoApiClient sync http 封装（auto-inject X-MCP-Token / 4xx 转 ApiError）"
```

---

### Task 5: MCP tool: `list_articles` (建立 Catalog 模式)

**Files:**
- Modify: `server/mcp/tools/catalog.py`

> 关键设计：所有 tool 返回顶层 `{"ok": bool, "data": ..., "error": str|None}` 包装。失败一律转 ok=False + error，不抛异常给 Claude（异常会中断 Loop）。

- [ ] **Step 1: 写 list_articles tool**

替换 `server/mcp/tools/catalog.py` 内容为：

```python
"""只读 Catalog 类工具。

每个 tool 走 `@mcp.tool` 装饰，签名直接做 LLM-facing schema：
- 参数有默认值则在 LLM prompt 里可省
- 返回 dict 顶层 `{ok, data, error}` —— 失败时 data=None, error=str
"""

from __future__ import annotations

from typing import Any

from server.mcp.config import get_config
from server.mcp.http_client import ApiError, GeoApiClient
from server.mcp.server import mcp


def _client() -> GeoApiClient:
    cfg = McpConfig() if False else get_config()  # 兼容直接 import 时也能拿到 cfg
    return GeoApiClient(base_url=cfg.api_base_url, token=cfg.token, timeout=cfg.timeout_seconds)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def _fail(error: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": error}


@mcp.tool()
def list_articles(
    status: str | None = None,
    review_status: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List GEO articles with filters.

    Args:
        status: Article workflow status. Common values: "draft", "ready".
        review_status: Editorial review status. Values: "pending", "approved".
        limit: Max number of articles to return (1-100).

    Returns:
        {"ok": True, "data": {"items": [...], "total": N}, "error": None} on success.
        {"ok": False, "data": None, "error": "<message>"} on failure.
    """
    params: dict[str, Any] = {"limit": max(1, min(100, limit))}
    if status:
        params["status"] = status
    if review_status:
        params["review_status"] = review_status
    try:
        data = _client().get("/api/articles", params=params)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
```

- [ ] **Step 2: 删掉测试占位（如果有）**

之前 Task 3 的 catalog.py 是空注释。这一步用上面新内容完全替换。

- [ ] **Step 3: 验证 server 启动时 tool 注册成功**

Run:
```bash
GEO_MCP_TOKEN=test-abc python -c "from server.mcp.server import mcp; print([t.name for t in mcp._tool_manager._tools.values()])"
```

Windows PowerShell:
```powershell
$env:GEO_MCP_TOKEN="test-abc"; python -c "from server.mcp.server import mcp; print([t.name for t in mcp._tool_manager._tools.values()])"
```

Expected: 输出 `['list_articles']`

- [ ] **Step 4: Commit**

```bash
git add server/mcp/tools/catalog.py
git commit -m "feat(mcp): list_articles tool（建立 catalog 模式 + ok/data/error 包装）"
```

---

### Task 6: MCP tools: `list_question_pools` + `list_question_items` + `list_prompt_templates`

**Files:**
- Modify: `server/mcp/tools/catalog.py`

- [ ] **Step 1: 在 catalog.py 末尾追加三个 tool**

```python
@mcp.tool()
def list_question_pools() -> dict[str, Any]:
    """List all question pools (Feishu-synced topic libraries)."""
    try:
        data = _client().get("/api/generation/question-pools")
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def list_question_items(
    pool_id: int,
    limit: int = 20,
    category: str | None = None,
) -> dict[str, Any]:
    """List question items within a pool, optionally filtered by category.

    Args:
        pool_id: Question pool id (from list_question_pools).
        limit: Max items to return (1-100).
        category: Optional category filter (e.g. "未分类" / specific category name).
    """
    params: dict[str, Any] = {"limit": max(1, min(100, limit))}
    if category:
        params["category"] = category
    try:
        data = _client().get(f"/api/generation/question-pools/{pool_id}/items", params=params)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def list_prompt_templates(scope: str = "generation") -> dict[str, Any]:
    """List prompt templates filtered by scope.

    Args:
        scope: One of "generation", "ai_format", "image_search", "image_companion".
               "generation" = article writing prompts (most common for Loops).
    """
    try:
        data = _client().get("/api/prompt-templates", params={"scope": scope})
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
```

- [ ] **Step 2: 验证 4 个 tool 都注册**

Run:
```powershell
$env:GEO_MCP_TOKEN="test-abc"; python -c "from server.mcp.server import mcp; print(sorted(t.name for t in mcp._tool_manager._tools.values()))"
```

Expected: `['list_articles', 'list_prompt_templates', 'list_question_items', 'list_question_pools']`

- [ ] **Step 3: Commit**

```bash
git add server/mcp/tools/catalog.py
git commit -m "feat(mcp): list_question_pools / list_question_items / list_prompt_templates tools"
```

---

### Task 7: 本机 Claude Code 配 mcpServers + 连通验证

**Files:**
- Modify: `~/.claude.json`（user-local，不进 repo）
- 创建：`docs/mcp-setup-notes.md`（开发说明）

> **不在 Phase 1 测试范围**：这是 D1 的最后一步，目的是验证「Claude Code 真能 spawn 起 MCP server + 调用三个 catalog tool」。

- [ ] **Step 1: 生成 MCP token**

Run（bash 或 git-bash）：
```bash
openssl rand -hex 32
```

Windows PowerShell:
```powershell
-join ((48..57) + (97..102) | Get-Random -Count 64 | % {[char]$_})
```

记下输出，假设是 `abc123def456...`

- [ ] **Step 2: 在 GEO 后端 .env 加 token**

打开（或创建）`.env` 文件：

```
GEO_MCP_TOKEN=abc123def456...（上一步生成的）
```

重启 GEO 后端：
```powershell
$env:GEO_MCP_TOKEN="abc123def456..."; uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000
```

- [ ] **Step 3: 配置 Claude Code mcpServers**

打开 `~/.claude.json`（Windows 在 `%USERPROFILE%\.claude.json`），找到（或加入）`mcpServers`：

```json
{
  "mcpServers": {
    "geo": {
      "command": "python",
      "args": ["-m", "server.mcp.server"],
      "env": {
        "GEO_MCP_TOKEN": "abc123def456...",
        "GEO_API_BASE_URL": "http://127.0.0.1:8000",
        "PYTHONPATH": "C:\\Users\\admin\\Desktop\\geo-collab"
      }
    }
  }
}
```

> **重要**：`PYTHONPATH` 要指向 geo-collab 仓库根（让 Python 能找到 `server.mcp.server`）。也可以省略 PYTHONPATH，改用 `cwd` 字段（FastMCP / mcp[cli] 配置中 cwd 支持，按 CLI doc 确认）。

- [ ] **Step 4: 重启 Claude Code、验证连接**

在新的 Claude Code 会话里，输入：
```
/mcp
```

Expected: 输出包含 `geo: connected` + 列出 4 个 tool 名字（list_articles / list_prompt_templates / list_question_items / list_question_pools）

- [ ] **Step 5: 实际调一次 list_articles**

在 Claude Code 里问：「列出最近 5 篇文章」。Claude 应自动调用 `list_articles(limit=5)`，看到 GEO 返回的 JSON。

如果 GEO 后端无 article 数据，结果是 `{"ok": true, "data": {"items": [], "total": 0}}`，也算通。如果是 `{"ok": false, "error": ...}`，根据 error 排错（最常见：token 不一致 / API 端口不对 / GEO 后端没起来）。

- [ ] **Step 6: 写开发笔记**

`docs/mcp-setup-notes.md`:

```markdown
# GEO MCP Server · 本机开发配置

## 准备

1. 生成 MCP token:
   - bash: `openssl rand -hex 32`
   - PowerShell: `-join ((48..57) + (97..102) | Get-Random -Count 64 | %{[char]$_})`
2. GEO 后端 `.env` 加 `GEO_MCP_TOKEN=<token>` 并重启
3. Claude Code 配 `~/.claude.json` 的 `mcpServers.geo`，env 里同样设 `GEO_MCP_TOKEN`

## 调试

- 看 MCP server 日志：FastMCP stdio 把 server 端日志写到 stderr（Claude Code 会有面板显示）
- 手动起 MCP server：`python -m server.mcp.server`（阻塞等 stdin，确认 import 路径正确）
- 列已注册 tool：
  ```bash
  GEO_MCP_TOKEN=test-abc python -c "from server.mcp.server import mcp; print(sorted(t.name for t in mcp._tool_manager._tools.values()))"
  ```

## 常见问题

- **`401 MCP token not configured`**：GEO 后端没读到 `GEO_MCP_TOKEN`（启动时漏 export / .env 没生效）
- **`401 invalid MCP token`**：两边 token 不一致
- **`network error`**：GEO 后端没起 / 端口不对
- **Claude Code 看不到 `geo`**：`~/.claude.json` JSON 格式不对 / 重启 Claude Code
```

- [ ] **Step 7: Commit**

```bash
git add docs/mcp-setup-notes.md
git commit -m "docs(mcp): 本机开发配置笔记（token 生成 / Claude Code mcpServers 配法 / 常见问题）"
```

**Phase 1 验收 Definition of Done**：
- ✅ `pytest server/tests/test_mcp_*.py` 全 PASS
- ✅ Claude Code 里 `/mcp` 看到 `geo: connected`、4 个工具列出
- ✅ Claude 问"列文章"时能拿到 GEO 返回的真实 JSON

---

## Phase 2 (D2) — 补完 Catalog + 直调生文 Action

---

### Task 8: 补完剩余 Catalog tools (`list_pipelines` / `list_accounts` / `get_article`)

**Files:**
- Modify: `server/mcp/tools/catalog.py`

- [ ] **Step 1: 追加三个 tool 到 catalog.py 末尾**

```python
@mcp.tool()
def list_pipelines(type_filter: str | None = None) -> dict[str, Any]:
    """List all pipelines (智能体 / workflows).

    Args:
        type_filter: Optional pipeline type filter (e.g. "agent" / "workflow").
    """
    params: dict[str, Any] = {}
    if type_filter:
        params["type"] = type_filter
    try:
        data = _client().get("/api/pipelines", params=params or None)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def list_accounts(
    platform_code: str | None = None,
    distribution_enabled: bool | None = None,
) -> dict[str, Any]:
    """List publishing accounts.

    Args:
        platform_code: Filter by platform (e.g. "toutiao", "wechat_mp").
        distribution_enabled: If true, only accounts available for distribution.
    """
    params: dict[str, Any] = {}
    if platform_code:
        params["platform_code"] = platform_code
    if distribution_enabled is not None:
        params["distribution_enabled"] = str(distribution_enabled).lower()
    try:
        data = _client().get("/api/accounts", params=params or None)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def get_article(article_id: int) -> dict[str, Any]:
    """Get one article by id, including full content_json / content_html / plain_text."""
    try:
        data = _client().get(f"/api/articles/{article_id}")
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
```

- [ ] **Step 2: 验证 7 个 catalog tool 都注册**

Run:
```powershell
$env:GEO_MCP_TOKEN="test-abc"; python -c "from server.mcp.server import mcp; print(sorted(t.name for t in mcp._tool_manager._tools.values()))"
```

Expected: 包含 `list_accounts`, `list_articles`, `list_pipelines`, `list_prompt_templates`, `list_question_items`, `list_question_pools`, `get_article`

- [ ] **Step 3: Commit**

```bash
git add server/mcp/tools/catalog.py
git commit -m "feat(mcp): 补完 catalog 三个 tool（list_pipelines / list_accounts / get_article）"
```

---

### Task 9: GEO 后端 service: `compose_one` 直调生文

> spec §3.4 决策：MCP 调 `compose_article` 时不创建 pipeline_run / scheme_run，直接调底层 article_writer。
> 现有 `generate_article_from_prompt`（`server/app/modules/ai_generation/article_writer.py:126`）签名是 `(session_factory, user_id, template_content, question_text, model)`，已经完整可复用——POC 不需要新写 service 层，只需要新加一个 router 把 question_item_id + template_id 解析成 (template_content, question_text) 后转调。

**Files:**
- Create: `server/app/modules/ai_generation/compose_once.py`
- Create: `server/tests/test_compose_once.py`

- [ ] **Step 1: 写失败测试**

`server/tests/test_compose_once.py`:

```python
"""compose_one：直调 article_writer.generate_article_from_prompt，绕开 scheme/pipeline 编排。"""

import pytest

from server.app.modules.ai_generation.compose_once import ComposeOnceRequest, compose_one


def test_compose_one_calls_writer_with_template_and_question(monkeypatch):
    """compose_one 应拼好 template_content + question_text 后调 generate_article_from_prompt。"""
    captured = {}

    def fake_writer(*, session_factory, user_id, template_content, question_text, model):
        captured["template_content"] = template_content
        captured["question_text"] = question_text
        captured["user_id"] = user_id
        captured["model"] = model
        return 987  # mock article_id

    monkeypatch.setattr(
        "server.app.modules.ai_generation.compose_once.generate_article_from_prompt",
        fake_writer,
    )

    # mock get_question_item / get_prompt_template
    class _Item:
        question_text = "测试问题"
        category = "未分类"

    class _Tpl:
        content = "请写一篇关于 {{问题}} 的文章"

    def fake_get_item(db, item_id):
        return _Item() if item_id == 1 else None

    def fake_get_tpl(db, tpl_id):
        return _Tpl() if tpl_id == 2 else None

    monkeypatch.setattr(
        "server.app.modules.ai_generation.compose_once._load_question_item", fake_get_item
    )
    monkeypatch.setattr(
        "server.app.modules.ai_generation.compose_once._load_prompt_template", fake_get_tpl
    )

    article_id = compose_one(
        session_factory=lambda: None,
        user_id=42,
        req=ComposeOnceRequest(question_item_id=1, prompt_template_id=2, model=None),
    )
    assert article_id == 987
    assert captured["template_content"] == "请写一篇关于 {{问题}} 的文章"
    assert "测试问题" in captured["question_text"]
    assert captured["user_id"] == 42


def test_compose_one_raises_on_missing_question(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.ai_generation.compose_once._load_question_item",
        lambda db, item_id: None,
    )
    monkeypatch.setattr(
        "server.app.modules.ai_generation.compose_once._load_prompt_template",
        lambda db, tpl_id: object(),
    )

    with pytest.raises(ValueError, match="question_item"):
        compose_one(
            session_factory=lambda: None,
            user_id=42,
            req=ComposeOnceRequest(question_item_id=999, prompt_template_id=2),
        )
```

- [ ] **Step 2: 跑测试验证失败**

Run: `pytest server/tests/test_compose_once.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 写实现**

`server/app/modules/ai_generation/compose_once.py`:

```python
"""compose_one — 单次直调生文，不进 pipeline_run / scheme_run。

供 MCP `compose_article` tool 调用：Claude Code Loop 想要"现在就给我生一篇"，
不需要走整套编排（无并发闸、无快照、无 retry）。直接复用 article_writer.generate_article_from_prompt。

设计约束：
- 不直接 import 模块全局，路径化的延迟读 question_item / prompt_template，便于测试 monkeypatch
- 抛 ValueError 让 router 转 400（不抛裸 Exception 走全局 500）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
from server.app.modules.ai_generation.question_bank import extract_question_text


@dataclass
class ComposeOnceRequest:
    question_item_id: int
    prompt_template_id: int
    model: str | None = None


def _load_question_item(db: Session, item_id: int) -> Any:
    from server.app.modules.ai_generation.models import QuestionItem
    return db.query(QuestionItem).filter(QuestionItem.id == item_id).first()


def _load_prompt_template(db: Session, tpl_id: int) -> Any:
    from server.app.modules.prompt_templates.models import PromptTemplate
    return db.query(PromptTemplate).filter(PromptTemplate.id == tpl_id).first()


def compose_one(
    *,
    session_factory: Callable[[], Session],
    user_id: int,
    req: ComposeOnceRequest,
) -> int:
    """调底层 article_writer 生一篇并返回 article_id。

    抛 ValueError 表示参数错（router 转 400）；底层 LLM/DB 异常向上抛（router 转 500）。
    """
    # 先用短会话拿 template_content + question_text
    db = session_factory()
    try:
        item = _load_question_item(db, req.question_item_id)
        if item is None:
            raise ValueError(f"question_item not found: id={req.question_item_id}")
        tpl = _load_prompt_template(db, req.prompt_template_id)
        if tpl is None:
            raise ValueError(f"prompt_template not found: id={req.prompt_template_id}")

        question_text = extract_question_text(item)
        template_content = tpl.content
    finally:
        db.close()

    # article_writer 自带短会话池管理，session_factory 透传过去
    return generate_article_from_prompt(
        session_factory=session_factory,
        user_id=user_id,
        template_content=template_content,
        question_text=question_text,
        model=req.model,
    )
```

> **注**：`extract_question_text` 是 `question_bank.py` 已有的函数（spec §1.2 "默认取法"），不用新写。

- [ ] **Step 4: 跑测试验证通过**

Run: `pytest server/tests/test_compose_once.py -v`
Expected: 2 个测试 PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/ai_generation/compose_once.py server/tests/test_compose_once.py
git commit -m "feat(generation): compose_one 直调生文（绕开 scheme/pipeline，供 MCP 调用）"
```

---

### Task 10: GEO 后端 router: `POST /api/generation/compose-once`

**Files:**
- Modify: `server/app/modules/ai_generation/router.py` 末尾
- Modify: `server/tests/test_compose_once.py`（追加 API 测试）

- [ ] **Step 1: 看现有 router.py 结构（不改、只参考路由风格）**

Read the file to see where to add:

```bash
# 看 router.py 末尾 200 行
```

定位到现有 router 末尾，在最后一个 `@router.post(...)` 之后追加。

- [ ] **Step 2: 追加路由**

```python
# === MCP-facing: compose_once ===
# 不进 pipeline_run / scheme_run，给 Claude Code Loop 直接生一篇用。
# 鉴权：MCP token（独立服务 token，跟 user JWT 隔离）。

from server.app.core.mcp_auth import require_mcp_token
from server.app.modules.ai_generation.compose_once import ComposeOnceRequest, compose_one


class ComposeOncePayload(BaseModel):
    question_item_id: int
    prompt_template_id: int
    model: str | None = None
    user_id: int  # MCP 调用时由 Claude Code 传"代表谁生文"；POC 期可固定 admin 用户 id


class ComposeOnceResponse(BaseModel):
    article_id: int


@router.post(
    "/compose-once",
    response_model=ComposeOnceResponse,
    dependencies=[Depends(require_mcp_token)],
)
def post_compose_once(payload: ComposeOncePayload) -> ComposeOnceResponse:
    """[MCP] 直调生文，返回 article_id。绕开 pipeline / scheme 编排。"""
    try:
        article_id = compose_one(
            session_factory=SessionLocal,  # 顶部已 import；若没有则补 from server.app.db.session import SessionLocal
            user_id=payload.user_id,
            req=ComposeOnceRequest(
                question_item_id=payload.question_item_id,
                prompt_template_id=payload.prompt_template_id,
                model=payload.model,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ComposeOnceResponse(article_id=article_id)
```

> **注**：`BaseModel` 和 `HTTPException` 如果文件顶部没 import 要补上。`SessionLocal` 同理。

- [ ] **Step 3: 追加 API 测试到 `server/tests/test_compose_once.py`**

```python
# 在 test_compose_once.py 末尾追加：

def test_compose_once_api_requires_mcp_token(monkeypatch):
    from server.tests.utils import build_test_app  # 项目里已有的 helper

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config
        config.get_settings.cache_clear()

        # 不带 token → 401
        r = test_app.client.post(
            "/api/generation/compose-once",
            json={"question_item_id": 1, "prompt_template_id": 2, "user_id": test_app.admin_id},
        )
        assert r.status_code == 401

        # 带错 token → 401
        r = test_app.client.post(
            "/api/generation/compose-once",
            json={"question_item_id": 1, "prompt_template_id": 2, "user_id": test_app.admin_id},
            headers={"X-MCP-Token": "wrong"},
        )
        assert r.status_code == 401
    finally:
        test_app.cleanup()


def test_compose_once_api_returns_400_on_missing_question(monkeypatch):
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config
        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/generation/compose-once",
            json={"question_item_id": 999999, "prompt_template_id": 1, "user_id": test_app.admin_id},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 400
        assert "question_item" in r.json()["detail"]
    finally:
        test_app.cleanup()
```

- [ ] **Step 4: 跑测试**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/test_compose_once.py -v`
Expected: 4 个测试 PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/ai_generation/router.py server/tests/test_compose_once.py
git commit -m "feat(generation): POST /api/generation/compose-once（MCP token 鉴权）"
```

---

### Task 11: MCP tool: `compose_article`

**Files:**
- Modify: `server/mcp/tools/action.py`

- [ ] **Step 1: 替换 action.py 为带 compose_article 的版本**

```python
"""写操作 Action 类工具。

compose / illustrate / submit_review / set_review_status / create_distribute / notify
"""

from __future__ import annotations

from typing import Any

from server.mcp.config import get_config
from server.mcp.http_client import ApiError, GeoApiClient
from server.mcp.server import mcp


def _client() -> GeoApiClient:
    cfg = get_config()
    return GeoApiClient(base_url=cfg.api_base_url, token=cfg.token, timeout=cfg.timeout_seconds)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def _fail(error: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": error}


# POC 期：调 compose_article 用一个固定 admin user_id 代表 Loop 身份。
# 后续可在 MCP 配置里加 `GEO_MCP_OPERATOR_USER_ID`，这里读环境变量。
import os
_OPERATOR_USER_ID = int(os.environ.get("GEO_MCP_OPERATOR_USER_ID", "1"))


@mcp.tool()
def compose_article(
    question_item_id: int,
    prompt_template_id: int,
    model: str | None = None,
) -> dict[str, Any]:
    """Compose a single article from a question item and a prompt template.

    Bypasses pipeline/scheme orchestration — calls article_writer directly. The article
    is saved with `review_status="pending"` (enters review queue).

    Args:
        question_item_id: From list_question_items.
        prompt_template_id: From list_prompt_templates(scope="generation").
        model: Optional litellm model override; None = use system default writing model.

    Returns:
        {"ok": True, "data": {"article_id": N}, "error": None}
    """
    payload = {
        "question_item_id": question_item_id,
        "prompt_template_id": prompt_template_id,
        "user_id": _OPERATOR_USER_ID,
    }
    if model:
        payload["model"] = model
    try:
        data = _client().post("/api/generation/compose-once", json=payload)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
```

- [ ] **Step 2: 验证 tool 注册**

Run:
```powershell
$env:GEO_MCP_TOKEN="test-abc"; python -c "from server.mcp.server import mcp; print(sorted(t.name for t in mcp._tool_manager._tools.values()))"
```

Expected: 列表包含 `compose_article`

- [ ] **Step 3: 手动 demo（要 GEO 后端跑起来 + 有问题项 + 有提示词模板）**

在 Claude Code 里：
1. 调 `list_question_pools` 找到一个 pool_id
2. 调 `list_question_items(pool_id=...)` 拿到一个 question_item_id
3. 调 `list_prompt_templates(scope="generation")` 拿到一个 template_id
4. 调 `compose_article(question_item_id=..., prompt_template_id=...)`
5. 应返回 `{"ok": true, "data": {"article_id": N}}`，N 是新建的 article id

在 GEO 前端「未审核库」tab 应该看到刚生成的文章。

- [ ] **Step 4: Commit**

```bash
git add server/mcp/tools/action.py
git commit -m "feat(mcp): compose_article tool（调 POST /api/generation/compose-once）"
```

---

### Task 12: MCP tool: `illustrate_article`

**Files:**
- Modify: `server/mcp/tools/action.py`（追加）
- 可能 Modify: `server/app/modules/articles/router.py` 或 `image_library/router.py` 加一个供 MCP 调的 endpoint

> **现状调研**：spec §3.4 写「illustrate_article 复用 `image_library/hook.py`」。但 hook 是模块内部函数，没暴露 HTTP。需要决定：
> - 方案 A：加一个 `POST /api/articles/{id}/illustrate` endpoint 在 articles router，body 含 `category_ids`，内部调 hook
> - 方案 B：MCP server 不调 HTTP，直接 import hook 函数（违反 spec §2.2 边界）
>
> **走方案 A**。

- [ ] **Step 1: 在 articles router 加 illustrate endpoint**

打开 `server/app/modules/articles/router.py`，找到文件末尾的 `articles_router`，追加：

```python
class IllustratePayload(BaseModel):
    category_ids: list[int] | None = None  # None = use article's existing stock_category_ids
    image_positions: list[int] | None = None  # None = auto-detect from content


class IllustrateResponse(BaseModel):
    inserted_count: int


@articles_router.post(
    "/{article_id}/illustrate",
    response_model=IllustrateResponse,
    dependencies=[Depends(require_mcp_token)],  # 顶部 import 一下
)
def illustrate_article(
    article_id: int,
    payload: IllustratePayload,
    db: Session = Depends(get_db),
) -> IllustrateResponse:
    """[MCP] Insert AI-selected images into the article body.

    Uses image_library/hook.py logic. POC 期：positions 默认按 content 顶层段落数自动均分。
    """
    from server.app.modules.image_library.hook import insert_images_for_article

    article = db.query(Article).filter(Article.id == article_id).first()
    if article is None:
        raise HTTPException(status_code=404, detail="article not found")

    # 选 category：payload > article.stock_category_ids[0]
    cat_ids = payload.category_ids or list(article.stock_category_ids or [])
    if not cat_ids:
        raise HTTPException(
            status_code=400,
            detail="no category_ids: either pass them or set article.stock_category_ids first",
        )
    category_id = cat_ids[0]

    # 自动 positions：默认在 content_json 第 2、4、6 段后插
    positions = payload.image_positions or [2, 4, 6]
    before = len(article.content_json.get("content", [])) if isinstance(article.content_json, dict) else 0
    insert_images_for_article(article_id, category_id, positions, db)
    db.commit()
    db.refresh(article)
    after = len(article.content_json.get("content", [])) if isinstance(article.content_json, dict) else 0
    return IllustrateResponse(inserted_count=max(0, after - before))
```

> **注**：顶部要 `from server.app.core.mcp_auth import require_mcp_token` 和必要的 model imports。

- [ ] **Step 2: 在 action.py 追加 MCP tool**

```python
@mcp.tool()
def illustrate_article(
    article_id: int,
    category_ids: list[int] | None = None,
    image_positions: list[int] | None = None,
) -> dict[str, Any]:
    """Insert AI-selected stock images into article body.

    Args:
        article_id: Target article (must exist).
        category_ids: Image library categories to draw from. None = use article's existing tags.
        image_positions: Insertion indices in content array. None = auto [2, 4, 6].
    """
    body: dict[str, Any] = {}
    if category_ids:
        body["category_ids"] = category_ids
    if image_positions:
        body["image_positions"] = image_positions
    try:
        data = _client().post(f"/api/articles/{article_id}/illustrate", json=body)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
```

- [ ] **Step 3: 验证 tool 注册 + 手动测一次**

Run:
```powershell
$env:GEO_MCP_TOKEN="test-abc"; python -c "from server.mcp.server import mcp; print(sorted(t.name for t in mcp._tool_manager._tools.values()))"
```

Expected: 列表含 `compose_article`, `illustrate_article`

手动：在 Claude Code 里调 `compose_article(...)` 拿到 article_id，再调 `illustrate_article(article_id, category_ids=[1])` 看是否在前端能看到正文插入了图。

- [ ] **Step 4: Commit**

```bash
git add server/app/modules/articles/router.py server/mcp/tools/action.py
git commit -m "feat(mcp): illustrate_article tool（POST /api/articles/{id}/illustrate）"
```

---

### Task 13: Phase 2 端到端 manual demo

- [ ] **Step 1: Loop 模拟跑一遍**

在 Claude Code 里：
```
请用 MCP geo server 完成：
1. list_question_pools()
2. 选第一个 pool，list_question_items(pool_id=..., limit=3)
3. list_prompt_templates(scope="generation")
4. 对 3 个问题中的第 1 个，用第一个模板调 compose_article(...)
5. 给生成的 article 调 illustrate_article(article_id, category_ids=[1])
6. 最后 get_article(article_id) 检查 content 里是否插了图
```

Expected: Claude 自主完成上述 6 步，最后给出"已生成 article_id=N，插入 X 张图"。

- [ ] **Step 2: 在 GEO 前端「未审核库」tab 验证文章存在 + 内容含图**

- [ ] **Step 3: Commit Phase 2 验收记录（如果有变更）**

如果上一步发现 bug，修复并提交。如果一切顺利，跳过这一步。

**Phase 2 验收 Definition of Done**：
- ✅ 7 个 catalog tool 都在 `/mcp` 列表里
- ✅ `compose_article` + `illustrate_article` 跑通：能在 GEO UI 里看到 Claude Code 生成的文章 + 自动插图

---

## Phase 3 (D3) — Alembic 0047 + 评分 + 自动审核

---

### Task 14: Alembic 迁移 0047 — `auto_review_decisions` 表 + `articles.metrics` 列

**Files:**
- Create: `server/alembic/versions/0047_auto_review_decisions.py`
- Modify: `server/app/modules/articles/models.py`（加 `metrics: JSON` 列）

- [ ] **Step 1: 写迁移**

```python
"""auto_review_decisions：Loop 自动评分记录；articles 加 metrics JSON 列（回流用）。

修订 ID: 0047
上一修订: 0046
创建日期: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)

    if "auto_review_decisions" not in inspector.get_table_names():
        op.create_table(
            "auto_review_decisions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("article_id", sa.Integer(), nullable=False),
            sa.Column("decision", sa.String(20), nullable=False),
            sa.Column("score_total", sa.Integer(), nullable=True),
            sa.Column("score_breakdown", sa.JSON(), nullable=True),
            sa.Column("reasoning", sa.Text(), nullable=True),
            sa.Column("decided_by", sa.String(50), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_auto_review_decisions_article_created",
            "auto_review_decisions",
            ["article_id", sa.text("created_at DESC")],
        )
        op.create_foreign_key(
            "fk_auto_review_decisions_article",
            "auto_review_decisions",
            "articles",
            ["article_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # 给 articles 加 metrics 列（如果还没有）
    article_cols = {c["name"] for c in inspector.get_columns("articles")}
    if "metrics" not in article_cols:
        op.add_column("articles", sa.Column("metrics", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "auto_review_decisions" in inspector.get_table_names():
        op.drop_constraint(
            "fk_auto_review_decisions_article",
            "auto_review_decisions",
            type_="foreignkey",
        )
        op.drop_index("ix_auto_review_decisions_article_created", table_name="auto_review_decisions")
        op.drop_table("auto_review_decisions")
    article_cols = {c["name"] for c in inspector.get_columns("articles")}
    if "metrics" in article_cols:
        op.drop_column("articles", "metrics")
```

- [ ] **Step 2: 给 Article ORM 加 metrics 字段**

在 `server/app/modules/articles/models.py` 里找到 `Article` 类（约 30-50 行），在最后一个字段后追加：

```python
    # MCP 回流写入：发布后的阅读 / 互动指标
    # JSON 结构示例: {"views": 1234, "likes": 56, "comments": 7, "shares": 3, "recorded_at": "2026-06-18T..."}
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

> 顶部要 `from sqlalchemy import JSON`（如果还没 import）。

- [ ] **Step 3: 跑迁移**

```bash
alembic upgrade head
```

Expected: `Running upgrade 0046 -> 0047, auto_review_decisions...`

验证：

```bash
python -c "from server.app.db.session import SessionLocal; from sqlalchemy import inspect; db = SessionLocal(); print(inspect(db.bind).get_columns('auto_review_decisions'))"
```

应输出 8 列定义。

- [ ] **Step 4: Commit**

```bash
git add server/alembic/versions/0047_auto_review_decisions.py server/app/modules/articles/models.py
git commit -m "feat(db): 0047 加 auto_review_decisions 表 + articles.metrics JSON 列"
```

---

### Task 15: `auto_review` 模块骨架（model + schema + service）

**Files:**
- Create: `server/app/modules/auto_review/__init__.py`（空）
- Create: `server/app/modules/auto_review/models.py`
- Create: `server/app/modules/auto_review/schemas.py`
- Create: `server/app/modules/auto_review/service.py`
- Create: `server/tests/test_auto_review.py`

- [ ] **Step 1: 写 model**

`server/app/modules/auto_review/models.py`:

```python
"""AutoReviewDecision — Loop 自评分记录。

跟 articles 是多对一：一个 article 可有多次自动审核记录（每次 Loop 跑都写一条）。
不直接修改 articles.review_status；最终人工审核仍是 truth。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.app.core.time import utcnow
from server.app.db.session import Base


class AutoReviewDecision(Base):
    __tablename__ = "auto_review_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    # values: "approved" | "needs_rewrite" | "rejected"

    score_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    decided_by: Mapped[str] = mapped_column(String(50), nullable=False)
    # 示例: "claude-code-loop" / "auto-reviewer-v1" / "claude-code-manual"

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
```

- [ ] **Step 2: 写 schemas**

`server/app/modules/auto_review/schemas.py`:

```python
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Decision = Literal["approved", "needs_rewrite", "rejected"]


class ScoreRequest(BaseModel):
    article_ids: list[int] = Field(..., min_length=1, max_length=20)
    dimensions: list[str] | None = None
    # None = use defaults: ["factuality", "readability", "style", "policy_safety"]


class ScoreBreakdown(BaseModel):
    article_id: int
    score_total: int
    score_breakdown: dict[str, int]
    suggested_decision: Decision
    reasoning: str


class ScoreResponse(BaseModel):
    results: list[ScoreBreakdown]


class AutoReviewSubmitRequest(BaseModel):
    decision: Decision
    score_total: int | None = None
    score_breakdown: dict[str, int] | None = None
    reasoning: str | None = None
    decided_by: str = "claude-code-loop"


class AutoReviewDecisionRead(BaseModel):
    id: int
    article_id: int
    decision: Decision
    score_total: int | None
    score_breakdown: dict[str, int] | None
    reasoning: str | None
    decided_by: str
    created_at: datetime

    class Config:
        from_attributes = True
```

- [ ] **Step 3: 写 service 骨架（评分 + 持久化两个纯函数；细节 Task 16 填）**

`server/app/modules/auto_review/service.py`:

```python
"""auto_review service：LLM 批量评分 + decision 持久化。

评分用 ai_format_model（deepseek-v4-flash 经济档），由 ai_models.service 解析。
失败容错：单条评分失败 → score_total=None + reasoning="[评分失败] ..." 仍入结果列表。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from server.app.modules.articles.models import Article
from server.app.modules.auto_review.models import AutoReviewDecision
from server.app.modules.auto_review.schemas import (
    AutoReviewSubmitRequest,
    ScoreBreakdown,
    ScoreRequest,
)

DEFAULT_DIMENSIONS = ["factuality", "readability", "style", "policy_safety"]


def score_articles(db: Session, req: ScoreRequest) -> list[ScoreBreakdown]:
    """批量评分。每条独立调 LLM，单条失败不影响其它。返回结果与 input 顺序一致。"""
    # Task 16 实现
    raise NotImplementedError("Task 16")


def submit_decision(
    db: Session,
    article_id: int,
    req: AutoReviewSubmitRequest,
) -> AutoReviewDecision:
    """写一条 AutoReviewDecision。注意：不动 article.review_status，最终人审兜底。"""
    article = db.query(Article).filter(Article.id == article_id).first()
    if article is None:
        raise ValueError(f"article not found: {article_id}")
    decision = AutoReviewDecision(
        article_id=article_id,
        decision=req.decision,
        score_total=req.score_total,
        score_breakdown=req.score_breakdown,
        reasoning=req.reasoning,
        decided_by=req.decided_by,
    )
    db.add(decision)
    db.flush()
    return decision
```

- [ ] **Step 4: 写单元测试（submit_decision）**

`server/tests/test_auto_review.py`:

```python
from server.app.modules.auto_review.schemas import AutoReviewSubmitRequest
from server.app.modules.auto_review.service import submit_decision
from server.tests.utils import build_test_app


def test_submit_decision_persists(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        # 先建一篇 article
        from server.app.modules.articles.models import Article
        from server.app.modules.articles.parser import dumps_content_json
        db = test_app.session_factory()
        try:
            a = Article(
                user_id=test_app.admin_id,
                title="test",
                content_json={"type": "doc", "content": []},
                content_html="",
                plain_text="",
                word_count=0,
                status="draft",
                review_status="pending",
            )
            db.add(a)
            db.commit()
            article_id = a.id
        finally:
            db.close()

        db = test_app.session_factory()
        try:
            decision = submit_decision(
                db,
                article_id,
                AutoReviewSubmitRequest(
                    decision="approved",
                    score_total=85,
                    score_breakdown={"factuality": 90, "readability": 80},
                    reasoning="reads well",
                    decided_by="claude-code-loop",
                ),
            )
            db.commit()
            assert decision.id is not None
            assert decision.decision == "approved"
            assert decision.score_breakdown == {"factuality": 90, "readability": 80}
        finally:
            db.close()
    finally:
        test_app.cleanup()
```

- [ ] **Step 5: 跑测试**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_auto_review.py::test_submit_decision_persists -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/auto_review/ server/tests/test_auto_review.py
git commit -m "feat(auto_review): model + schemas + service.submit_decision（评分留 Task 16）"
```

---

### Task 16: `auto_review.service.score_articles` LLM 评分实现

**Files:**
- Modify: `server/app/modules/auto_review/service.py:score_articles`
- Modify: `server/tests/test_auto_review.py`

- [ ] **Step 1: 写失败测试**

追加到 `test_auto_review.py`:

```python
def test_score_articles_returns_one_per_input(monkeypatch):
    """评分接受一组 article_id，对每个返回一条结果（含失败兜底）。"""

    # mock litellm.completion
    def fake_completion(*args, **kwargs):
        class _Choice:
            message = type("m", (), {"content": (
                '{"score_breakdown": {"factuality": 85, "readability": 80, "style": 75, "policy_safety": 90},'
                ' "score_total": 82, "suggested_decision": "approved", "reasoning": "looks ok"}'
            )})()
        class _Resp:
            choices = [_Choice()]
        return _Resp()

    monkeypatch.setattr("litellm.completion", fake_completion)

    # mock resolve_ai_format_model
    monkeypatch.setattr(
        "server.app.modules.ai_models.service.resolve_ai_format_model",
        lambda db, selected=None: ("deepseek/deepseek-v4-flash", "fake-key", None, 60),
    )

    test_app = build_test_app(monkeypatch)
    try:
        # 建 2 篇 article
        from server.app.modules.articles.models import Article
        db = test_app.session_factory()
        try:
            articles = []
            for i in range(2):
                a = Article(
                    user_id=test_app.admin_id,
                    title=f"t{i}",
                    content_json={"type": "doc", "content": []},
                    content_html="",
                    plain_text=f"body {i} " * 50,
                    word_count=100,
                    status="draft",
                    review_status="pending",
                )
                db.add(a)
                articles.append(a)
            db.commit()
            ids = [a.id for a in articles]
        finally:
            db.close()

        from server.app.modules.auto_review.schemas import ScoreRequest
        from server.app.modules.auto_review.service import score_articles

        db = test_app.session_factory()
        try:
            results = score_articles(db, ScoreRequest(article_ids=ids))
            assert len(results) == 2
            assert all(r.score_total == 82 for r in results)
            assert all(r.suggested_decision == "approved" for r in results)
        finally:
            db.close()
    finally:
        test_app.cleanup()


def test_score_articles_returns_failure_for_invalid_json(monkeypatch):
    """LLM 输出非 JSON → 该条返回 score_total=None + reasoning 含 [评分失败]。"""

    def fake_completion(*args, **kwargs):
        class _Choice:
            message = type("m", (), {"content": "this is not json"})()
        class _Resp:
            choices = [_Choice()]
        return _Resp()

    monkeypatch.setattr("litellm.completion", fake_completion)
    monkeypatch.setattr(
        "server.app.modules.ai_models.service.resolve_ai_format_model",
        lambda db, selected=None: ("any-model", "k", None, 60),
    )

    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.models import Article
        db = test_app.session_factory()
        try:
            a = Article(
                user_id=test_app.admin_id, title="x",
                content_json={"type": "doc", "content": []},
                content_html="", plain_text="text",
                word_count=4, status="draft", review_status="pending",
            )
            db.add(a); db.commit()
            aid = a.id
        finally:
            db.close()

        from server.app.modules.auto_review.schemas import ScoreRequest
        from server.app.modules.auto_review.service import score_articles
        db = test_app.session_factory()
        try:
            results = score_articles(db, ScoreRequest(article_ids=[aid]))
            assert len(results) == 1
            assert results[0].score_total is None or results[0].score_total < 0
            assert "[评分失败]" in results[0].reasoning
        finally:
            db.close()
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试验证失败**

Run: `pytest server/tests/test_auto_review.py -v`
Expected: `score_articles` 相关测试 FAIL（NotImplementedError）

- [ ] **Step 3: 实现 score_articles**

替换 `service.py` 的 `score_articles` 占位为：

```python
import json
import logging

import litellm

from server.app.modules.ai_models.service import resolve_ai_format_model

_logger = logging.getLogger(__name__)


_SCORE_PROMPT_TEMPLATE = """你是「餐厅养成记」官方矩阵的内容评估官。

请评估下面这篇文章在以下维度的表现，每项 0-100 整数：
{dimensions_block}

文章正文（已截断到 4000 字）：
---
{plain_text}
---

只输出严格 JSON，不要 markdown / 代码块，键名严格匹配维度 key：
{{
  "score_breakdown": {{ "<key>": <int>, ... }},
  "score_total": <int 0-100>,
  "suggested_decision": "approved" | "needs_rewrite" | "rejected",
  "reasoning": "<1-2 句话>"
}}

判定规则建议：score_total >= 70 → approved；40-69 → needs_rewrite；< 40 → rejected。
"""

_DIM_LABELS = {
    "factuality": ("事实性", "事实陈述是否准确、有无明显错误或夸大"),
    "readability": ("可读性", "句子流畅、结构清晰、逻辑通顺"),
    "style": ("风格匹配", "符合餐厅养成记目标受众（休闲玩家、女性偏多、治愈调性）"),
    "policy_safety": ("政策安全", "无敏感话题、无违规、无诱导"),
}


def _format_dimensions(dimensions: list[str]) -> str:
    lines = []
    for i, key in enumerate(dimensions, 1):
        label, hint = _DIM_LABELS.get(key, (key, ""))
        lines.append(f"  {i}. {key}（{label}）：{hint}")
    return "\n".join(lines)


def score_articles(db: Session, req: ScoreRequest) -> list[ScoreBreakdown]:
    """批量评分。每条独立调 LLM，单条失败不影响其它。"""
    dimensions = req.dimensions or DEFAULT_DIMENSIONS
    model, api_key, base_url, timeout = resolve_ai_format_model(db, selected=None)

    results: list[ScoreBreakdown] = []
    articles = (
        db.query(Article)
        .filter(Article.id.in_(req.article_ids))
        .all()
    )
    by_id = {a.id: a for a in articles}

    for aid in req.article_ids:
        a = by_id.get(aid)
        if a is None:
            results.append(ScoreBreakdown(
                article_id=aid, score_total=-1,
                score_breakdown={k: 0 for k in dimensions},
                suggested_decision="rejected",
                reasoning="[评分失败] article not found",
            ))
            continue

        plain = (a.plain_text or "")[:4000]
        prompt = _SCORE_PROMPT_TEMPLATE.format(
            dimensions_block=_format_dimensions(dimensions),
            plain_text=plain,
        )

        try:
            resp = litellm.completion(
                model=model,
                api_key=api_key or None,
                api_base=base_url or None,
                timeout=timeout,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or "{}"
            parsed = json.loads(content)
            results.append(ScoreBreakdown(
                article_id=aid,
                score_total=int(parsed.get("score_total", 0)),
                score_breakdown={k: int(parsed.get("score_breakdown", {}).get(k, 0)) for k in dimensions},
                suggested_decision=parsed.get("suggested_decision", "needs_rewrite"),
                reasoning=parsed.get("reasoning", ""),
            ))
        except Exception as exc:  # noqa: BLE001 单条失败不影响其它
            _logger.warning("score article %s failed: %s", aid, exc)
            results.append(ScoreBreakdown(
                article_id=aid, score_total=-1,
                score_breakdown={k: 0 for k in dimensions},
                suggested_decision="rejected",
                reasoning=f"[评分失败] {exc}",
            ))

    return results
```

- [ ] **Step 4: 跑测试验证通过**

Run: `pytest server/tests/test_auto_review.py -v`
Expected: 3 个测试全 PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/auto_review/service.py server/tests/test_auto_review.py
git commit -m "feat(auto_review): score_articles LLM 评分（ai_format_model + 单条容错）"
```

---

### Task 17: `auto_review` router 与 main.py 挂载

**Files:**
- Create: `server/app/modules/auto_review/router.py`
- Modify: `server/app/main.py`
- Modify: `server/tests/test_auto_review.py`

- [ ] **Step 1: 写 router**

```python
"""auto_review router：`POST /api/articles/score` + `POST /api/articles/{id}/auto-review`。

两条都用 MCP token 鉴权（独立于 user JWT）。
注意 prefix 挂在 main.py 是 `/api/articles`，因此本 router path 自己不再带 `/articles`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from server.app.core.mcp_auth import require_mcp_token
from server.app.db.session import get_db
from server.app.modules.auto_review.schemas import (
    AutoReviewDecisionRead,
    AutoReviewSubmitRequest,
    ScoreRequest,
    ScoreResponse,
)
from server.app.modules.auto_review.service import score_articles, submit_decision

router = APIRouter()


@router.post(
    "/score",
    response_model=ScoreResponse,
    dependencies=[Depends(require_mcp_token)],
)
def post_score(req: ScoreRequest, db: Session = Depends(get_db)) -> ScoreResponse:
    """[MCP] LLM 批量评分。最多 20 篇一次（schema 校验）。"""
    results = score_articles(db, req)
    return ScoreResponse(results=results)


@router.post(
    "/{article_id}/auto-review",
    response_model=AutoReviewDecisionRead,
    dependencies=[Depends(require_mcp_token)],
)
def post_auto_review(
    article_id: int,
    req: AutoReviewSubmitRequest,
    db: Session = Depends(get_db),
) -> AutoReviewDecisionRead:
    """[MCP] 写一条自动审核 decision。"""
    try:
        decision = submit_decision(db, article_id, req)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()
    db.refresh(decision)
    return AutoReviewDecisionRead.model_validate(decision)
```

- [ ] **Step 2: 在 main.py 顶部 import + create_app() 里挂载**

在 `server/app/main.py` 顶部 import 区，找到 `from server.app.modules.articles.router import (...)` 附近，添加：

```python
from server.app.modules.auto_review.router import router as auto_review_router
```

在 `create_app()` 内，找到 `app.include_router(articles_router, prefix="/api/articles", ...)` 那段，**之后**追加：

```python
    # auto_review 走 /api/articles 前缀（与现有 article 路由同前缀，由 MCP token 单独鉴权）
    app.include_router(
        auto_review_router,
        prefix="/api/articles",
        tags=["auto-review"],
    )
```

> **注**：这个 router 路由内部已用 `Depends(require_mcp_token)`，不再加 `Depends(get_current_user)`（MCP 服务身份独立）。

- [ ] **Step 3: 追加 API 集成测试**

```python
# 在 test_auto_review.py 末尾追加：

def test_post_score_api_requires_mcp_token(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config
        config.get_settings.cache_clear()

        r = test_app.client.post("/api/articles/score", json={"article_ids": [1]})
        assert r.status_code == 401
    finally:
        test_app.cleanup()


def test_post_auto_review_api_404_on_missing_article(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config
        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/articles/999999/auto-review",
            json={"decision": "approved", "decided_by": "claude-code-loop"},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 404
    finally:
        test_app.cleanup()
```

- [ ] **Step 4: 跑测试**

Run: `pytest server/tests/test_auto_review.py -v`
Expected: 5 个测试 PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/auto_review/router.py server/app/main.py server/tests/test_auto_review.py
git commit -m "feat(auto_review): router + main 挂载（POST /api/articles/score + /api/articles/{id}/auto-review）"
```

---

### Task 18: MCP tools: `score_recent_articles` + `submit_review_decision`

**Files:**
- Modify: `server/mcp/tools/meta.py`
- Modify: `server/mcp/tools/action.py`

- [ ] **Step 1: 在 meta.py 加 score_recent_articles**

```python
"""评估 / 反馈回流类工具。"""

from __future__ import annotations

from typing import Any

from server.mcp.config import get_config
from server.mcp.http_client import ApiError, GeoApiClient
from server.mcp.server import mcp


def _client() -> GeoApiClient:
    cfg = get_config()
    return GeoApiClient(base_url=cfg.api_base_url, token=cfg.token, timeout=cfg.timeout_seconds)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def _fail(error: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": error}


@mcp.tool()
def score_recent_articles(
    article_ids: list[int],
    dimensions: list[str] | None = None,
) -> dict[str, Any]:
    """LLM-score one or more articles using GEO's ai_format model.

    Args:
        article_ids: Up to 20 article ids per call.
        dimensions: Score dimensions. None = ["factuality", "readability", "style", "policy_safety"].

    Returns:
        results: list of {article_id, score_total, score_breakdown, suggested_decision, reasoning}
    """
    body: dict[str, Any] = {"article_ids": article_ids}
    if dimensions:
        body["dimensions"] = dimensions
    try:
        data = _client().post("/api/articles/score", json=body)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
```

- [ ] **Step 2: 在 action.py 加 submit_review_decision**

```python
@mcp.tool()
def submit_review_decision(
    article_id: int,
    decision: str,
    score_total: int | None = None,
    score_breakdown: dict[str, int] | None = None,
    reasoning: str | None = None,
    decided_by: str = "claude-code-loop",
) -> dict[str, Any]:
    """Record an auto-review decision for an article.

    Note: this does NOT change `article.review_status` — final human review is still authoritative.
    The decision is persisted for audit / training data.

    Args:
        article_id: Target article.
        decision: One of "approved" / "needs_rewrite" / "rejected".
        score_total: 0-100 weighted score, optional.
        score_breakdown: dict[dimension_key, score_0_100], optional.
        reasoning: 1-2 sentence explanation, optional.
        decided_by: Identifier for the deciding agent (default "claude-code-loop").
    """
    if decision not in ("approved", "needs_rewrite", "rejected"):
        return _fail(f"invalid decision: {decision}")
    body: dict[str, Any] = {"decision": decision, "decided_by": decided_by}
    if score_total is not None:
        body["score_total"] = score_total
    if score_breakdown is not None:
        body["score_breakdown"] = score_breakdown
    if reasoning:
        body["reasoning"] = reasoning
    try:
        data = _client().post(f"/api/articles/{article_id}/auto-review", json=body)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
```

- [ ] **Step 3: 验证 tool 注册**

```powershell
$env:GEO_MCP_TOKEN="test-abc"; python -c "from server.mcp.server import mcp; print(sorted(t.name for t in mcp._tool_manager._tools.values()))"
```

Expected: 包含 `score_recent_articles`, `submit_review_decision`

- [ ] **Step 4: Commit**

```bash
git add server/mcp/tools/meta.py server/mcp/tools/action.py
git commit -m "feat(mcp): score_recent_articles + submit_review_decision tools"
```

**Phase 3 验收 Definition of Done**：
- ✅ `alembic upgrade head` 成功，DB 里有 `auto_review_decisions` 表 + `articles.metrics` 列
- ✅ `pytest server/tests/test_auto_review.py` 全 PASS
- ✅ Claude Code 调 `score_recent_articles([article_id])` 能拿到真实 LLM 评分
- ✅ `submit_review_decision(article_id, "approved", ...)` 在 DB 能看到 decision 记录

---

## Phase 4 (D4) — 飞书通知 + 第一个 Loop

---

### Task 19: 飞书通用 notify + `POST /api/system/feishu-notify`

**Files:**
- Modify: `server/app/shared/feishu.py`
- Modify: `server/app/modules/system/system_router.py`
- Create: `server/tests/test_feishu_notify_api.py`

- [ ] **Step 1: 在 feishu.py 抽出通用 send_text**

打开 `server/app/shared/feishu.py`，在 `_send` 函数之后追加：

```python
def send_text(title: str, message: str, level: str = "info") -> bool:
    """通用飞书通知（同步发送，立即返回是否成功）。

    level ∈ info / warning / error / done — 用于决定 emoji 前缀，不影响 webhook 路由。
    return True 表示已发出（不保证对方收到），False 表示 webhook 未配置或网络失败。
    """
    url = get_settings().feishu_webhook_url
    if not url:
        return False
    emoji = {"info": "💬", "warning": "⚠️", "error": "❌", "done": "✅"}.get(level, "💬")
    text = f"【geo】{emoji} {title}\n{message}"
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception:
        _logger.warning("Failed to send Feishu notification", exc_info=True)
        return False
```

- [ ] **Step 2: 在 system_router.py 加 endpoint**

```python
from pydantic import BaseModel

from server.app.core.mcp_auth import require_mcp_token
from server.app.shared.feishu import send_text


class FeishuNotifyPayload(BaseModel):
    title: str
    message: str
    level: str = "info"  # info / warning / error / done


class FeishuNotifyResponse(BaseModel):
    sent: bool


@router.post(
    "/feishu-notify",
    response_model=FeishuNotifyResponse,
    dependencies=[Depends(require_mcp_token)],
)
def post_feishu_notify(payload: FeishuNotifyPayload) -> FeishuNotifyResponse:
    """[MCP] Send a Feishu webhook notification with title/message/level."""
    sent = send_text(payload.title, payload.message, payload.level)
    return FeishuNotifyResponse(sent=sent)
```

> **注意**：system_router 在 main.py 已经挂在 `/api/system` prefix 下，所以最终 URL 是 `POST /api/system/feishu-notify`。原 router 已有 `Depends(get_current_user)` 挂在整个 router 上吗？如果是，需要在这条 endpoint 上额外指定（或者新建一个 sub-router 专挂 MCP token）。先看 main.py 的注册行：

```python
app.include_router(
    system_router,
    prefix="/api/system",
    tags=["system"],
    dependencies=[Depends(get_current_user)],
)
```

> ⚠️ router 级别已经挂了 `get_current_user`，新增的 endpoint 会**两个都校验**（既要 user JWT 又要 MCP token）。这不是我们想要的——MCP 应该只走 token。
>
> **修正方案**：把 `feishu-notify` 放到一个独立的 sub-router，单独挂在 `/api/system`，只用 MCP token 校验。

修改 `server/app/modules/system/system_router.py`，在文件末尾追加：

```python
# === MCP-facing endpoints（不走 user JWT，走 MCP token）===
mcp_system_router = APIRouter()


@mcp_system_router.post(
    "/feishu-notify",
    response_model=FeishuNotifyResponse,
    dependencies=[Depends(require_mcp_token)],
)
def post_feishu_notify(payload: FeishuNotifyPayload) -> FeishuNotifyResponse:
    """[MCP] Send a Feishu webhook notification with title/message/level."""
    sent = send_text(payload.title, payload.message, payload.level)
    return FeishuNotifyResponse(sent=sent)
```

然后在 `main.py` 里挂载（在原 system_router 注册之后）：

```python
from server.app.modules.system.system_router import mcp_system_router

# ...在 create_app() 内...
app.include_router(
    mcp_system_router,
    prefix="/api/system",
    tags=["system-mcp"],
    # 不挂 get_current_user 依赖，MCP token 在 endpoint 内单独校验
)
```

- [ ] **Step 3: 写 API 测试**

`server/tests/test_feishu_notify_api.py`:

```python
from server.tests.utils import build_test_app


def test_feishu_notify_requires_mcp_token(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config
        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/system/feishu-notify",
            json={"title": "test", "message": "hello"},
        )
        assert r.status_code == 401
    finally:
        test_app.cleanup()


def test_feishu_notify_returns_sent_false_when_webhook_unset(monkeypatch):
    monkeypatch.setenv("GEO_FEISHU_WEBHOOK_URL", "")
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config
        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/system/feishu-notify",
            json={"title": "t", "message": "m", "level": "info"},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200
        assert r.json() == {"sent": False}  # webhook 未配置，返回 False
    finally:
        test_app.cleanup()
```

- [ ] **Step 4: 跑测试**

Run: `pytest server/tests/test_feishu_notify_api.py -v`
Expected: 2 个测试 PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/shared/feishu.py server/app/modules/system/system_router.py server/app/main.py server/tests/test_feishu_notify_api.py
git commit -m "feat(feishu): send_text 通用接口 + POST /api/system/feishu-notify（MCP token）"
```

---

### Task 20: MCP tools: `notify_feishu` + `set_review_status`

**Files:**
- Modify: `server/mcp/tools/action.py`

- [ ] **Step 1: 在 action.py 末尾追加两个 tool**

```python
@mcp.tool()
def notify_feishu(
    title: str,
    message: str,
    level: str = "info",
) -> dict[str, Any]:
    """Send a Feishu webhook notification.

    Args:
        title: Short header line (e.g. "Loop 完成").
        message: Body text (multi-line OK).
        level: "info" | "warning" | "error" | "done" — controls emoji prefix.
    """
    if level not in ("info", "warning", "error", "done"):
        return _fail(f"invalid level: {level}")
    try:
        data = _client().post(
            "/api/system/feishu-notify",
            json={"title": title, "message": message, "level": level},
        )
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def set_review_status(article_id: int, review_status: str) -> dict[str, Any]:
    """Update an article's review_status.

    Args:
        article_id: Target article id.
        review_status: "pending" (enter review queue) or "approved" (move to approved library).

    Note: MCP token does not pass user JWT. This calls GEO's PATCH /api/articles/{id}
    which normally requires user auth. POC 期暂用 user JWT 替代——v2 改成 MCP token 直通。
    """
    if review_status not in ("pending", "approved"):
        return _fail(f"invalid review_status: {review_status}")
    # POC 限制：set_review_status 暂走另一条专用 endpoint（避免改动 article PATCH 的鉴权）
    try:
        data = _client().post(
            f"/api/articles/{article_id}/set-review-status",
            json={"review_status": review_status},
        )
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
```

- [ ] **Step 2: 在 GEO 后端加 `POST /api/articles/{id}/set-review-status`**

打开 `server/app/modules/articles/router.py`，追加：

```python
class SetReviewStatusPayload(BaseModel):
    review_status: str  # "pending" | "approved"


class SetReviewStatusResponse(BaseModel):
    article_id: int
    review_status: str


@articles_router.post(
    "/{article_id}/set-review-status",
    response_model=SetReviewStatusResponse,
    dependencies=[Depends(require_mcp_token)],
)
def set_review_status_mcp(
    article_id: int,
    payload: SetReviewStatusPayload,
    db: Session = Depends(get_db),
) -> SetReviewStatusResponse:
    """[MCP] Switch article.review_status between pending / approved."""
    if payload.review_status not in ("pending", "approved"):
        raise HTTPException(status_code=400, detail="invalid review_status")
    article = db.query(Article).filter(Article.id == article_id).first()
    if article is None:
        raise HTTPException(status_code=404, detail="article not found")
    article.review_status = payload.review_status
    db.commit()
    db.refresh(article)
    return SetReviewStatusResponse(article_id=article_id, review_status=article.review_status)
```

- [ ] **Step 3: 验证 tool 注册 + 手动调一次飞书通知**

```powershell
$env:GEO_MCP_TOKEN="test-abc"; python -c "from server.mcp.server import mcp; print(sorted(t.name for t in mcp._tool_manager._tools.values()))"
```

Expected: 列表含 `notify_feishu`, `set_review_status`

如果项目配了 `GEO_FEISHU_WEBHOOK_URL`，在 Claude Code 里调一次：
```
notify_feishu(title="MCP 联调测试", message="如果你看到这条说明 MCP → GEO → 飞书链路通了", level="info")
```
飞书群里应该看到一条「💬 MCP 联调测试」。

- [ ] **Step 4: Commit**

```bash
git add server/mcp/tools/action.py server/app/modules/articles/router.py
git commit -m "feat(mcp): notify_feishu + set_review_status tools + POST /api/articles/{id}/set-review-status"
```

---

### Task 21: 第一个 Loop 配方：`claude-loops/generation-loop.md`

**Files:**
- Create: `claude-loops/generation-loop.md`

- [ ] **Step 1: 写 Loop 配方**

```bash
mkdir -p claude-loops
```

`claude-loops/generation-loop.md`:

```markdown
# 生文 Loop 配方

> **运行方式**：在 Claude Code 里 `/loop claude-loops/generation-loop.md` 启动。
>
> **目标**：今天产出 5 篇过自动评分的文章入未审核库，飞书群播报进度。

## 你是谁

你是 GEO 平台「餐厅养成记」官方矩阵的生文 Loop runner。你不直连数据库、不直接调 LLM API——所有操作通过 `geo` MCP server 提供的工具。

## 可用工具

来自 `mcp__geo__*`（按调用顺序大致排列）：

- `list_question_pools()` / `list_question_items(pool_id, limit, category?)` — 拿候选选题
- `list_prompt_templates(scope="generation")` — 拿可用模板
- `get_template_performance(template_id, window_days?)`（D6 可用，POC 早期可不调）
- `compose_article(question_item_id, prompt_template_id, model?)` — 直调生文，返回 article_id
- `illustrate_article(article_id, category_ids?, image_positions?)` — 配图
- `score_recent_articles(article_ids, dimensions?)` — LLM 批量评分
- `submit_review_decision(article_id, decision, score_total?, score_breakdown?, reasoning?)` — 写 decision 记录
- `set_review_status(article_id, "pending" | "approved")` — 切审核状态（POC 默认 pending 入未审核库）
- `get_article(article_id)` — 取详情（debug 用）
- `notify_feishu(title, message, level)` — 飞书通知

## 流程（伪码）

```
notify_feishu(title="生文 Loop 开始", message="目标 5 篇过自评 / 餐厅养成记", level="info")

pools = list_question_pools()
pool_id = pools.data[0].id  # 默认取第一个

candidates = list_question_items(pool_id=pool_id, limit=10).data.items
templates = list_prompt_templates(scope="generation").data
success_count = 0
attempts = 0

while success_count < 5 and attempts < 15:
    attempts += 1
    qid = candidates[attempts - 1].id  # 用过的不复用
    tpl_id = templates[attempts % len(templates)].id  # 简单轮换

    # 生文
    r = compose_article(question_item_id=qid, prompt_template_id=tpl_id)
    if not r.ok:
        notify_feishu("生文失败", f"qid={qid} reason={r.error}", "warning")
        continue
    aid = r.data.article_id

    # 配图（可失败，不影响）
    illustrate_article(article_id=aid, category_ids=[1])  # category_ids 后续可从 question.category 推

    # 评分
    s = score_recent_articles(article_ids=[aid])
    if not s.ok or not s.data.results:
        submit_review_decision(article_id=aid, decision="needs_rewrite", reasoning="[评分失败] 无结果")
        continue
    score = s.data.results[0]

    if score.score_total >= 70:
        submit_review_decision(
            article_id=aid, decision="approved",
            score_total=score.score_total, score_breakdown=score.score_breakdown,
            reasoning=score.reasoning,
        )
        # set_review_status 默认就是 pending（compose 时已设），不必再调
        success_count += 1
    elif score.score_total >= 40:
        submit_review_decision(
            article_id=aid, decision="needs_rewrite",
            score_total=score.score_total, score_breakdown=score.score_breakdown,
            reasoning=score.reasoning,
        )
        # POC 期不做自动重试，留待下一轮 / 人工
    else:
        submit_review_decision(
            article_id=aid, decision="rejected",
            score_total=score.score_total, score_breakdown=score.score_breakdown,
            reasoning=score.reasoning,
        )

notify_feishu(
    title="生文 Loop 完成",
    message=f"产出 {success_count}/5 篇过自评候选 · 共尝试 {attempts} 轮",
    level="done",
)
```

## 停止条件

- 成功达成 5 篇 → 退出 + 飞书 done
- 累计 15 轮仍未达成 → 退出 + 飞书 warning（"产能不足，请检查 prompt/选题"）
- 任意工具连续失败 3 次 → 退出 + 飞书 error

## 注意事项

- **不要直接读 article 的 plain_text 全文**：评分由 `score_recent_articles` 在 GEO 内部做，避免 Opus token 烧光
- **始终通过 MCP 工具**：不要尝试直接读文件 / 调外部 API
- **每一步失败要 fallback**：单次失败 → 跳过这个 qid 而不是停整个 Loop
- **飞书消息要节制**：开始、结束、严重失败发；中间进度可省
```

- [ ] **Step 2: Commit**

```bash
git add claude-loops/generation-loop.md
git commit -m "docs(loops): generation-loop 配方（D4 第一个 Loop）"
```

---

### Task 22: Phase 4 端到端 demo

> 这一步在 Claude Code 实测，没有自动测试。需要 GEO 后端跑着 + question_pool 有数据 + prompt_templates 至少一条 generation scope 的模板 + 飞书 webhook 配好。

- [ ] **Step 1: 跑配方**

在 Claude Code 里：
```
/loop claude-loops/generation-loop.md
```

Expected:
- 飞书收到 "生文 Loop 开始" 消息
- Claude Code 输出 5-10 个 tool_use 调用序列
- 每篇 article 在 GEO 「未审核库」tab 看得到
- `auto_review_decisions` 表 SELECT 能看到对应记录
- 飞书收到 "生文 Loop 完成" 消息

- [ ] **Step 2: 排错调参**

如果 LLM 评分大量返回 < 70（rejected/needs_rewrite 太多），调 prompt（`server/app/modules/auto_review/service.py` 的 `_SCORE_PROMPT_TEMPLATE`）：

- 把"严格"改成"按行业平均水平"
- 加示例："分数 80 大约相当于...""分数 60 大约相当于..."

调完跑第二轮，看是否合理。

- [ ] **Step 3: 截图存档 + commit 调参（如果有）**

```bash
git add server/app/modules/auto_review/service.py  # 如果改了 prompt
git commit -m "feat(auto_review): 调评分 prompt 让分布更合理（D4 demo 反馈）"
```

**Phase 4 验收 Definition of Done**：
- ✅ Claude Code 跑 generation-loop.md 能产出 ≥ 3 篇通过自评的文章
- ✅ 飞书群收到完整三段消息（开始 / 进度 / 完成）
- ✅ `SELECT * FROM auto_review_decisions ORDER BY created_at DESC LIMIT 5` 能看到刚写的 decision

---

## Phase 5 (D5) — 发文 Loop

---

### Task 23: MCP tool: `create_distribute_task`

**Files:**
- Modify: `server/mcp/tools/action.py`

> 现有 GEO API `POST /api/tasks` 已经支持 `task_type="article_round_robin"`（CLAUDE.md §Domain Modules `tasks/` 提到）。
> POC 期 MCP 直接调它，不新写 endpoint。
>
> ⚠️ 但 POST /api/tasks 现有用 `Depends(get_current_user)` —— MCP 走不通。需要：
> - 方案 A：新增 `POST /api/tasks/mcp` 端点，复用 service.create_task，用 MCP token 鉴权
> - 方案 B：让 MCP server 持有一个固定 user JWT（POC 期粗暴但快）
>
> **走方案 A**——新建专用 endpoint。

**Files (full):**
- Create: `POST /api/tasks/mcp` 端点（在 `server/app/modules/tasks/router.py` 加）
- Modify: `server/mcp/tools/action.py`

- [ ] **Step 1: 在 tasks router 加 MCP 专用端点**

`server/app/modules/tasks/router.py` 末尾追加：

```python
class TaskMcpCreatePayload(BaseModel):
    name: str
    article_ids: list[int]
    account_ids: list[int]
    platform_code: str = "toutiao"
    user_id: int  # MCP 调时传 operator user id（与 compose-once 一致）
    stop_before_publish: bool = False


class TaskMcpCreateResponse(BaseModel):
    task_id: int


@tasks_router.post(
    "/mcp",
    response_model=TaskMcpCreateResponse,
    dependencies=[Depends(require_mcp_token)],
)
def create_task_mcp(
    payload: TaskMcpCreatePayload,
    db: Session = Depends(get_db),
) -> TaskMcpCreateResponse:
    """[MCP] Create an article_round_robin task. Reuses task service.create_task."""
    from server.app.modules.tasks.service import TaskCreate, create_task
    import uuid

    task = create_task(
        db,
        payload.user_id,
        TaskCreate(
            name=payload.name,
            client_request_id=str(uuid.uuid4()),
            task_type="article_round_robin",
            article_ids=payload.article_ids,
            accounts=[
                {"account_id": aid, "sort_order": i} for i, aid in enumerate(payload.account_ids)
            ],
            platform_code=payload.platform_code,
            stop_before_publish=payload.stop_before_publish,
        ),
    )
    db.commit()
    return TaskMcpCreateResponse(task_id=task.id)
```

> **注**：上面的 service 调用要看现有 service.create_task 的 signature，可能要微调。

- [ ] **Step 2: 在 action.py 加 MCP tool**

```python
@mcp.tool()
def create_distribute_task(
    name: str,
    article_ids: list[int],
    account_ids: list[int],
    platform_code: str = "toutiao",
    stop_before_publish: bool = False,
) -> dict[str, Any]:
    """Create an article_round_robin distribute task.

    Args:
        name: Human-readable task name (e.g. "Daily distribute 2026-06-18").
        article_ids: Articles to distribute (must be review_status="approved" already).
        account_ids: Target accounts. Round-robin maps article→account by sort_order.
        platform_code: "toutiao" / "wechat_mp" etc. Default "toutiao".
        stop_before_publish: If True, task pauses before actual publish (manual confirm needed).
    """
    body = {
        "name": name,
        "article_ids": article_ids,
        "account_ids": account_ids,
        "platform_code": platform_code,
        "user_id": int(__import__("os").environ.get("GEO_MCP_OPERATOR_USER_ID", "1")),
        "stop_before_publish": stop_before_publish,
    }
    try:
        data = _client().post("/api/tasks/mcp", json=body)
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
```

- [ ] **Step 3: 验证 + commit**

```powershell
$env:GEO_MCP_TOKEN="test-abc"; python -c "from server.mcp.server import mcp; print(sorted(t.name for t in mcp._tool_manager._tools.values()))"
```

Expected: 列表含 `create_distribute_task`

```bash
git add server/app/modules/tasks/router.py server/mcp/tools/action.py
git commit -m "feat(mcp): create_distribute_task tool + POST /api/tasks/mcp（article_round_robin）"
```

---

### Task 24: 发文 Loop 配方：`claude-loops/distribute-loop.md`

**Files:**
- Create: `claude-loops/distribute-loop.md`

- [ ] **Step 1: 写配方**

```markdown
# 发文 Loop 配方

> **运行方式**：在 Claude Code 里 `/loop claude-loops/distribute-loop.md`。
> **目标**：把已审核库待发布文章分发到合适账号 + 回流上一轮 metrics。

## 你是谁

GEO「餐厅养成记」官方矩阵的发文 Loop runner。

## 可用工具

- `list_articles(status?, review_status?, limit)` — 拿候选
- `list_accounts(platform_code?, distribution_enabled?)` — 拿可用账号
- `get_account_performance(account_id, window_days?)`（D6 后可用；先无）
- `create_distribute_task(name, article_ids, account_ids, platform_code?, stop_before_publish?)`
- `record_publish_metrics(record_id, metrics)`（D6 后可用）
- `notify_feishu(title, message, level)`

## 流程

```
notify_feishu(title="发文 Loop 开始", message="拉取已审核 + 分发", level="info")

# 1. 分发阶段
articles_resp = list_articles(status="ready", review_status="approved", limit=20)
articles = articles_resp.data.items if articles_resp.ok else []
if not articles:
    notify_feishu("发文 Loop 跳过", "已审核库无待发布文章", "info")
    return

accounts_resp = list_accounts(platform_code="toutiao", distribution_enabled=True)
accounts = accounts_resp.data if accounts_resp.ok else []
if not accounts:
    notify_feishu("发文 Loop 失败", "无可用 toutiao 账号", "error")
    return

# POC：直接全量分发；v2 用 get_account_performance 选 top-N
article_ids = [a.id for a in articles[:5]]  # 一次最多 5 篇
account_ids = [a.id for a in accounts[:3]]   # 限 3 个账号

r = create_distribute_task(
    name=f"Daily distribute {today}",
    article_ids=article_ids,
    account_ids=account_ids,
    platform_code="toutiao",
    stop_before_publish=True,  # POC 期手动确认，避免误发
)

if not r.ok:
    notify_feishu("发文任务创建失败", r.error, "error")
    return

# 2. 回流阶段（D6 之后启用）
# metrics = ... (placeholder)

notify_feishu(
    title="发文 Loop 完成",
    message=f"已创建任务 #{r.data.task_id}，分发 {len(article_ids)} 篇到 {len(account_ids)} 账号（stop_before_publish=True，请人工确认）",
    level="done",
)
```

## 停止条件

- 创建成功 → 退出，飞书 done
- 任意必要步骤失败 → 退出，飞书 error
- 已审核库无待发布 → 退出，飞书 info

## 注意

- POC 强制 `stop_before_publish=True` —— 防误发。人工去 GEO 前端「分发引擎」tab 确认后再继续。
- 回流阶段在 D6 评估器 API 完成后再启用。
```

- [ ] **Step 2: Commit**

```bash
git add claude-loops/distribute-loop.md
git commit -m "docs(loops): distribute-loop 配方（D5 第二个 Loop）"
```

---

### Task 25: Phase 5 端到端 demo

- [ ] **Step 1: 准备**

前提：D4 跑完后，已审核库至少有 1 篇 `review_status="approved"` 的文章（可以从未审核库手动审过去）。

- [ ] **Step 2: 跑 distribute Loop**

在 Claude Code 里：`/loop claude-loops/distribute-loop.md`

Expected:
- 飞书收到 "发文 Loop 开始" + "发文 Loop 完成 / 已创建任务 #N"
- GEO 「分发引擎」tab 看到新任务、stop_before_publish=True（在 publish 前停下来等手动确认）

- [ ] **Step 3: 调试 + commit（如有）**

```bash
git commit -am "fix(...): D5 端到端调整" # 仅当有改动
```

**Phase 5 验收 Definition of Done**：
- ✅ Claude Code 跑 distribute-loop 能创建任务、飞书有通知
- ✅ 在 GEO 「分发引擎」tab 能看到该任务、状态正确

---

## Phase 6 (D6) — 评估器 + 回流

---

### Task 26: `performance` 模块（service + router）

**Files:**
- Create: `server/app/modules/performance/__init__.py`（空）
- Create: `server/app/modules/performance/service.py`
- Create: `server/app/modules/performance/router.py`
- Create: `server/tests/test_performance.py`
- Modify: `server/app/main.py`

- [ ] **Step 1: 写 service**

`server/app/modules/performance/service.py`:

```python
"""performance service：聚合模板 / 账号的表现指标。

读 articles.metrics（D3 加的 JSON 列）+ publish_records 表，按维度聚合。
POC 期：用 SQL aggregate 或纯 Python 算（数据量小）；v2 加缓存层。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.modules.articles.models import Article


def get_template_performance(
    db: Session,
    template_id: int,
    window_days: int = 7,
) -> dict[str, Any]:
    """聚合某个 prompt template 在窗口期内产出文章的指标。

    返回:
        {
          "template_id": int,
          "window_days": int,
          "article_count": int,
          "avg_views": float | None,
          "avg_likes": float | None,
          "approval_rate": float | None,  # 经自动审核 approved 的占比
        }
    """
    since = utcnow() - timedelta(days=window_days)
    # POC: articles 没有直接 template_id 字段（生成后不存 source template id）—— 
    # 暂时返回空结构，D6 决定是否补 article.source_template_id 字段
    # 或者：用 audit_logs 查找"哪些 article 由这个 template compose 出来"
    # 简化：先返回 stub，让 MCP tool 链路通
    return {
        "template_id": template_id,
        "window_days": window_days,
        "article_count": 0,
        "avg_views": None,
        "avg_likes": None,
        "approval_rate": None,
        "note": "POC stub — 评估聚合实现待补 (compose_once 加 source_template_id 后填)",
    }


def get_account_performance(
    db: Session,
    account_id: int,
    window_days: int = 7,
) -> dict[str, Any]:
    """聚合某账号窗口期内已发布文章的指标。"""
    since = utcnow() - timedelta(days=window_days)
    # 通过 publish_records 找该账号的发布、再 join articles.metrics
    from server.app.modules.tasks.models import PublishRecord
    records = (
        db.query(PublishRecord)
        .filter(
            PublishRecord.account_id == account_id,
            PublishRecord.status == "succeeded",
            PublishRecord.finished_at >= since,
        )
        .all()
    )
    article_ids = [r.article_id for r in records if r.article_id]
    articles = db.query(Article).filter(Article.id.in_(article_ids)).all() if article_ids else []
    views = []
    likes = []
    for a in articles:
        if a.metrics:
            if (v := a.metrics.get("views")) is not None:
                views.append(v)
            if (lk := a.metrics.get("likes")) is not None:
                likes.append(lk)
    return {
        "account_id": account_id,
        "window_days": window_days,
        "publish_count": len(records),
        "with_metrics_count": len([a for a in articles if a.metrics]),
        "avg_views": (sum(views) / len(views)) if views else None,
        "avg_likes": (sum(likes) / len(likes)) if likes else None,
    }


def record_publish_metrics(
    db: Session,
    record_id: int,
    metrics: dict[str, Any],
) -> None:
    """写回某条 publish_record 对应 article 的 metrics（合并到 article.metrics JSON）。"""
    from server.app.modules.tasks.models import PublishRecord

    record = db.query(PublishRecord).filter(PublishRecord.id == record_id).first()
    if record is None:
        raise ValueError(f"publish_record not found: {record_id}")
    article = db.query(Article).filter(Article.id == record.article_id).first()
    if article is None:
        raise ValueError(f"article not found for record {record_id}: {record.article_id}")
    existing = dict(article.metrics or {})
    existing.update(metrics)
    existing.setdefault("recorded_at", utcnow().isoformat() + "Z")
    article.metrics = existing
```

- [ ] **Step 2: 写 router**

`server/app/modules/performance/router.py`:

```python
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.app.core.mcp_auth import require_mcp_token
from server.app.db.session import get_db
from server.app.modules.performance.service import (
    get_account_performance,
    get_template_performance,
    record_publish_metrics,
)

router = APIRouter()


@router.get(
    "/prompt-templates/{template_id}/performance",
    dependencies=[Depends(require_mcp_token)],
)
def get_template_performance_endpoint(
    template_id: int,
    window_days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return get_template_performance(db, template_id, window_days)


@router.get(
    "/accounts/{account_id}/performance",
    dependencies=[Depends(require_mcp_token)],
)
def get_account_performance_endpoint(
    account_id: int,
    window_days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return get_account_performance(db, account_id, window_days)


class PublishMetricsPayload(BaseModel):
    metrics: dict[str, Any]


@router.post(
    "/publish-records/{record_id}/metrics",
    dependencies=[Depends(require_mcp_token)],
)
def post_publish_metrics(
    record_id: int,
    payload: PublishMetricsPayload,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        record_publish_metrics(db, record_id, payload.metrics)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()
    return {"ok": True}
```

- [ ] **Step 3: 挂载到 main.py**

```python
from server.app.modules.performance.router import router as performance_router

# 在 create_app() 内合适位置：
app.include_router(
    performance_router,
    prefix="/api",  # 路径自带 /prompt-templates/... 等子路径
    tags=["performance"],
)
```

- [ ] **Step 4: 写测试**

`server/tests/test_performance.py`:

```python
from server.app.modules.performance.service import record_publish_metrics
from server.tests.utils import build_test_app


def test_record_publish_metrics_merges_into_article(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.models import Article
        from server.app.modules.tasks.models import PublishRecord

        db = test_app.session_factory()
        try:
            a = Article(
                user_id=test_app.admin_id, title="t",
                content_json={"type": "doc", "content": []},
                content_html="", plain_text="", word_count=0,
                status="ready", review_status="approved",
                metrics={"views": 100},  # 已有的会被合并
            )
            db.add(a); db.commit()
            r = PublishRecord(
                task_id=1, article_id=a.id, platform_id=1, account_id=1,
                status="succeeded",
            )
            db.add(r); db.commit()
            aid, rid = a.id, r.id
        finally:
            db.close()

        db = test_app.session_factory()
        try:
            record_publish_metrics(db, rid, {"likes": 50, "comments": 5})
            db.commit()
            a = db.query(Article).filter(Article.id == aid).first()
            assert a.metrics["views"] == 100  # 保留
            assert a.metrics["likes"] == 50  # 新增
            assert a.metrics["comments"] == 5
        finally:
            db.close()
    finally:
        test_app.cleanup()
```

- [ ] **Step 5: 跑测试**

Run: `pytest server/tests/test_performance.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/performance/ server/app/main.py server/tests/test_performance.py
git commit -m "feat(performance): 模板/账号表现聚合 + publish-records metrics 回写 + 挂载"
```

---

### Task 27: MCP tools: `get_template_performance` + `get_account_performance` + `record_publish_metrics`

**Files:**
- Modify: `server/mcp/tools/meta.py`

- [ ] **Step 1: 在 meta.py 末尾追加三个 tool**

```python
@mcp.tool()
def get_template_performance(
    template_id: int,
    window_days: int = 7,
) -> dict[str, Any]:
    """Aggregate performance for a prompt template's output articles.

    Returns: {template_id, window_days, article_count, avg_views, avg_likes, approval_rate}
    """
    try:
        data = _client().get(
            f"/api/prompt-templates/{template_id}/performance",
            params={"window_days": window_days},
        )
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def get_account_performance(
    account_id: int,
    window_days: int = 7,
) -> dict[str, Any]:
    """Aggregate performance for an account's published articles.

    Returns: {account_id, window_days, publish_count, with_metrics_count, avg_views, avg_likes}
    """
    try:
        data = _client().get(
            f"/api/accounts/{account_id}/performance",
            params={"window_days": window_days},
        )
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))


@mcp.tool()
def record_publish_metrics(
    record_id: int,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """Record post-publish metrics (views/likes/comments/shares) for a publish record.

    Args:
        record_id: PublishRecord id (from list_articles → check publish history; or platform API).
        metrics: Dict, typically {"views": int, "likes": int, "comments": int, "shares": int}.
                 Merges into the article's metrics JSON column.
    """
    try:
        data = _client().post(
            f"/api/publish-records/{record_id}/metrics",
            json={"metrics": metrics},
        )
        return _ok(data)
    except ApiError as exc:
        return _fail(str(exc))
```

- [ ] **Step 2: 验证 tool 注册**

```powershell
$env:GEO_MCP_TOKEN="test-abc"; python -c "from server.mcp.server import mcp; print(sorted(t.name for t in mcp._tool_manager._tools.values()))"
```

Expected: 列表含 `get_template_performance`, `get_account_performance`, `record_publish_metrics`

- [ ] **Step 3: Commit**

```bash
git add server/mcp/tools/meta.py
git commit -m "feat(mcp): get_template_performance + get_account_performance + record_publish_metrics tools"
```

---

### Task 28: `claude-loops/weekly-report-loop.md`

**Files:**
- Create: `claude-loops/weekly-report-loop.md`

- [ ] **Step 1: 写配方**

```markdown
# 周报 Loop 配方

> **运行方式**：`/loop claude-loops/weekly-report-loop.md`
> **目标**：每周一跑，飞书发一份模板 / 账号表现周报

## 工具

- `list_prompt_templates(scope="generation")`
- `get_template_performance(template_id, window_days=7)`
- `list_accounts(distribution_enabled=true)`
- `get_account_performance(account_id, window_days=7)`
- `notify_feishu(title, message, level)`

## 流程

```
notify_feishu(title="周报生成中", message="拉取过去 7 天数据...", level="info")

templates = list_prompt_templates(scope="generation").data
template_perf = []
for t in templates:
    p = get_template_performance(template_id=t.id, window_days=7)
    if p.ok:
        template_perf.append((t.name, p.data))

accounts = list_accounts(distribution_enabled=True).data
account_perf = []
for a in accounts:
    p = get_account_performance(account_id=a.id, window_days=7)
    if p.ok:
        account_perf.append((a.display_name, p.data))

# 整理成 markdown
lines = ["# 周报（过去 7 天）", "", "## 模板表现"]
for name, p in sorted(template_perf, key=lambda x: -(x[1].get("avg_views") or 0)):
    lines.append(f"- {name}: 产文 {p['article_count']} 篇 / 均阅 {p.get('avg_views')} / 通过率 {p.get('approval_rate')}")
lines.append("\n## 账号表现")
for name, p in sorted(account_perf, key=lambda x: -(x[1].get("avg_views") or 0)):
    lines.append(f"- {name}: 发布 {p['publish_count']} 次 / 均阅 {p.get('avg_views')} / 均赞 {p.get('avg_likes')}")

notify_feishu(
    title="本周周报",
    message="\n".join(lines),
    level="done",
)
```

## 注意

- POC 期数据多半是 stub / 无 metrics —— 报告内容可能很空，正常
- v2 接入真实平台数据后才会有内容
```

- [ ] **Step 2: Commit**

```bash
git add claude-loops/weekly-report-loop.md
git commit -m "docs(loops): weekly-report-loop 配方（D6 评估器 Loop）"
```

**Phase 6 验收 Definition of Done**：
- ✅ `get_template_performance` / `get_account_performance` / `record_publish_metrics` 三个 tool 都能调通（返回 stub 数据或真实数据都算）
- ✅ `weekly-report-loop.md` 在 Claude Code 跑能产出飞书消息（内容可空）

---

## Phase 7 (D7) — 收尾 + 老板演示

---

### Task 29: 更新 CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 在 `Architecture` 章 `Domain Modules` 列表里追加 auto_review / performance 描述**

找到 CLAUDE.md 里 `## Architecture` → `### Domain Modules (server/app/modules/)` 的 bullet 列表，在合适位置加：

```markdown
- `auto_review/` — Loop 自动审核：`AutoReviewDecision` 表 + `POST /api/articles/score`（LLM 批量评分，用 ai_format_model）+ `POST /api/articles/{id}/auto-review`（写 decision）。**不直接动 `article.review_status`，最终人审兜底**。MCP token 鉴权（独立 service token、与 user JWT 隔离）。
- `performance/` — 模板 / 账号产出 metrics 聚合：`GET /api/prompt-templates/{id}/performance` + `GET /api/accounts/{id}/performance` + `POST /api/publish-records/{id}/metrics`（回流写入，合并到 `Article.metrics` JSON 列）。POC 期 template 聚合是 stub（缺 article→template 反向引用），v2 改造。
```

- [ ] **Step 2: 新增 MCP server 章节（放在 `Architecture` 之后、`Asset Upload` 之前）**

```markdown
## MCP Server（Claude Code Loop 调用入口）

POC 期：`server/mcp/` 跑独立 Python 进程（FastMCP stdio），把 GEO 现有 + 新增 API 包装成 ~15 个 atomic tools 给 Claude Code 调用。Loop 配方在 `claude-loops/*.md`。

### 启动方式

由 Claude Code 通过 `~/.claude.json` 的 `mcpServers.geo` 自动 spawn：

```json
{
  "mcpServers": {
    "geo": {
      "command": "python",
      "args": ["-m", "server.mcp.server"],
      "env": {
        "GEO_MCP_TOKEN": "<openssl rand -hex 32>",
        "GEO_API_BASE_URL": "http://127.0.0.1:8000",
        "GEO_MCP_OPERATOR_USER_ID": "1",
        "PYTHONPATH": "/path/to/geo-collab"
      }
    }
  }
}
```

详细配置见 `docs/mcp-setup-notes.md`。

### 鉴权边界

- MCP server 用独立 `GEO_MCP_TOKEN`（service token），跟 user JWT cookie 完全隔离
- GEO 后端校验在 `server/app/core/mcp_auth.py:require_mcp_token` —— hmac compare_digest
- 空 token 配置 = MCP 全禁用（所有带 token 请求 401）
- POC 期所有 MCP-facing endpoint 单独标 `dependencies=[Depends(require_mcp_token)]`，**不要复用 user JWT 路径**

### Tool 三组

- **catalog**（只读）：`list_articles` / `list_question_pools` / `list_question_items` / `list_prompt_templates` / `list_pipelines` / `list_accounts` / `get_article`
- **action**（写）：`compose_article` / `illustrate_article` / `submit_review_decision` / `set_review_status` / `create_distribute_task` / `notify_feishu`
- **meta**（评估 / 回流）：`score_recent_articles` / `get_template_performance` / `get_account_performance` / `record_publish_metrics`

### 加新 tool 的步骤

1. 在 GEO 后端加对应 API（带 `Depends(require_mcp_token)`）
2. 在 `server/mcp/tools/<group>.py` 用 `@mcp.tool()` 装饰新函数，签名直接做 LLM-facing schema
3. 在 server.py 已经 `from server.mcp.tools import <group>` 触发注册——新 tool 自动出现
4. 重启 Claude Code → `/mcp` 验证

### 加新 Loop 配方

放 `claude-loops/<name>-loop.md`，结构：你是谁 / 可用工具 / 流程伪码 / 停止条件 / 注意事项。
Claude Code 里 `/loop claude-loops/<name>-loop.md` 跑。
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude.md): 加 MCP server / auto_review / performance 模块说明"
```

---

### Task 30: 最终演示准备

**Files:**
- Create: `docs/mcp-demo-walkthrough.md`

- [ ] **Step 1: 准备演示脚本**

```markdown
# GEO MCP Loop · 老板演示 walkthrough

## 准备

1. 起 GEO 后端：`uvicorn server.app.main:app --reload`
2. 起 GEO 前端：`pnpm --filter @geo/web dev`
3. 飞书群打开 + 确认 webhook 配置正确
4. 起一个新的 Claude Code 会话

## Demo 1: 概念回顾（5 分钟）

打开 `docs/superpowers/specs/2026-06-17-loop-engineering-geo-integration.html`：
- 翻第 1 节"什么是 Loop Engineering · 五件套"
- 翻第 5 节"方案 C · Agent Town"
- 一句话总结：今天演示的是方案 C 用 Claude Code + GEO MCP 的最小落地

## Demo 2: MCP 连通（30 秒）

在 Claude Code 里：
```
/mcp
```
让老板看到 `geo: connected` + 15 个 tool 名字

## Demo 3: 生文 Loop 现场跑（5-10 分钟）

```
/loop claude-loops/generation-loop.md
```

老板看到：
- 飞书群弹出 "生文 Loop 开始" 消息
- Claude Code 实时显示 tool_use 序列（list_question_items → compose_article → illustrate_article → score_recent_articles → submit_review_decision）
- 切到 GEO 前端「未审核库」tab 看到新文章出现
- 切到 GEO 前端「文章详情」看到 AI 配图插在正文里
- 飞书群最后收到 "生文 Loop 完成 · 产出 N/5 篇"

## Demo 4: 现场点通过/否决（2 分钟）

在 GEO 前端「未审核库」点 1-2 篇文章，标 approved。
讲：这是人工兜底——Loop 评 70+ 进来的，运营再终审。

## Demo 5: 发文 Loop（3 分钟）

```
/loop claude-loops/distribute-loop.md
```

老板看到：
- 飞书消息 "发文 Loop 开始"
- GEO 前端「分发引擎」tab 出现新任务（stop_before_publish=True 状态）
- 飞书消息 "发文 Loop 完成 · 已创建任务 #N"

## Demo 6: 决定下一步（5 分钟）

讲：
- POC 跑通 = "Claude Code 当 Loop 大脑 + GEO 当能力底座" 这条路是可行的
- v2 候选：长跑服务器 / 真 metrics 接入 / 飞书内 OpenClaw 风格交互 / Skill 包装 / 选题 Loop（拉热榜借势）
- 老板拍：v2 投入哪几条
```

- [ ] **Step 2: 跑一次完整 walkthrough（自己 dry run，提前发现问题）**

按上面 Demo 1-5 跑一遍，每个 step 截图存档（备用，老板看不清现场就发截图）。

- [ ] **Step 3: Commit + 推分支**

```bash
git add docs/mcp-demo-walkthrough.md
git commit -m "docs(demo): 老板演示 walkthrough（D7 收尾）"

git push -u origin feat/geo-mcp-loop  # 第一次 push，等 PR review
```

**Phase 7 / Day 7 验收 Definition of Done**：
- ✅ CLAUDE.md 更新到位
- ✅ docs/mcp-demo-walkthrough.md 写完
- ✅ 分支 push 到 remote、PR 准备就绪
- ✅ Dry run demo 跑通

---

## Self-Review Notes

After writing this plan, I checked:

### Spec coverage
- ✅ spec §3 MCP server 设计 → Tasks 3 + 4 + 5
- ✅ spec §3.4 工具清单 15 个 → Tasks 5 / 6 / 8 / 11 / 12 / 18 / 20 / 23 / 27（catalog 7 / action 6 / meta 4，共 17 个工具，比 spec 多了 set_review_status 和 illustrate_article 的 manual placement — OK，是细化）
- ✅ spec §4 新表 + 新 API → Tasks 14 / 15 / 16 / 17 / 19 / 23 / 26
- ✅ spec §5 Loop 配方 → Tasks 21 / 24 / 28
- ✅ spec §6 飞书通知 → Task 19 + Loop 配方
- ✅ spec §7 7-day 节奏 → 7 phase 一一对应
- ✅ spec §8 改动 checklist → File Structure 章节列全
- ✅ spec §9 风险 → 主要在各 Task 的 note 里覆盖
- ✅ spec §11 跟讨论稿映射 → 不需要重复（spec 已写）
- ⚠️ spec §10 v2 路标 → 不在 plan 范围（这是 v2，POC 不实现）

### Placeholder scan
- 无 TODO / TBD / "implement later"
- 唯一一处 stub：Task 26 `get_template_performance` 返回 stub data —— 已在 service 注释和 docstring 明确说明"POC stub"，理由：需要 `articles.source_template_id` 反向引用，POC 范围外。这是设计取舍，不是 plan 失败。

### Type consistency
- `MCP token` 在所有 task 引用一致：环境变量名 `GEO_MCP_TOKEN`、header 名 `X-MCP-Token`、settings 字段名 `mcp_token`、dependency 名 `require_mcp_token`
- `Article.metrics` 字段在 Task 14 加，Task 26 读、Task 27 写——都用 `metrics` 字段名
- `AutoReviewDecision.decided_by` 字段在 Task 15 定义 String(50)、Task 18 默认值 "claude-code-loop" —— 一致
- `_OPERATOR_USER_ID` 在 Task 11 + 23 都从 `GEO_MCP_OPERATOR_USER_ID` 环境变量读 —— 一致

---

**Plan complete and saved to `docs/superpowers/plans/2026-06-18-claude-code-loop-with-geo-mcp.md`.**
