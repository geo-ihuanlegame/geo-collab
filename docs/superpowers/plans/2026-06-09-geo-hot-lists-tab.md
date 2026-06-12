# Geo 热榜 tab（集成 DailyHotApi）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Geo 前端新增一个「热榜」tab，展示 DailyHotApi 聚合的 56+ 热榜，点条目开原文。

**Architecture:** DailyHotApi 原样作为独立 Node 服务（vendored 进 `services/dailyhot-api/`，端口 6688）；Geo 后端新增一个无 DB 的轻量代理模块 `hot_lists`，把 `/api/hot-lists/*` 转发过去并复用 JWT 鉴权；前端加一个 tab 调 `/api/hot-lists/*`。只看不联动，无迁移、无 Redis。

**Tech Stack:** 后端 FastAPI + httpx（已是依赖）；前端 React 19 + TypeScript；vendored 服务为 Node.js/Hono。

设计文档：[docs/superpowers/specs/2026-06-09-geo-hot-lists-tab-design.md](../specs/2026-06-09-geo-hot-lists-tab-design.md)

---

## ⚠️ 用户约束（必须遵守）

> 本次只允许**新增**。对现有文件的**修改/删除**默认不做；只有"明确必要"时，先逐条列出找用户确认，**用户同意后**才执行。绝不碰 WIP 文件（ai_format.py / ai_illustrate.py / baidu.py / image_library/service.py 及相关测试 / requirements.txt / config.py）。

### 纯新增（无需审批，直接做）
- `services/dailyhot-api/`（vendored 整个 Node 项目）
- `server/app/modules/hot_lists/__init__.py`、`service.py`、`router.py`
- `server/tests/test_hot_lists_service.py`、`server/tests/test_hot_lists_api.py`
- `web/src/api/hot-lists.ts`
- `web/src/features/hot-lists/HotListsWorkspace.tsx`

### 必须修改现有文件（**逐条需用户审批**，对应 Task 标了 🔒）
1. 🔒 `server/app/main.py` — 加 1 行 import + 1 个 `include_router` 块（不改则路由挂不上，无替代方案）。
2. 🔒 `web/src/types.ts` — `NavKey` 联合类型加 `"hot-lists"`，`navItems` 数组加一项（不改则 tab 无法注册）。
3. 🔒 `web/src/App.tsx` — 加 1 行 import + 1 个渲染块（不改则 tab 不渲染）。
4. 🔒 `docker-compose.yml` — 加 `dailyhot-api` 服务 + 给 app 加 `GEO_HOTLIST_API_URL` env + `depends_on`（仅生产部署需要；本地开发不需要）。

> 这些都是**插入式新增行**，不删除、不重写任何现有逻辑。但因落在现有文件，仍按约束逐条等用户点头。

---

## Task 0: Vendor DailyHotApi 进仓库（纯新增）

**Files:**
- Create: `services/dailyhot-api/`（从 `E:\agent_study\DailyHotApi` 拷贝源码，剔除 `.git/`、`node_modules/`、`dist/`、`logs/`）

- [ ] **Step 1: 拷贝源码，排除无关目录**

PowerShell：
```powershell
$src = "E:\agent_study\DailyHotApi"
$dst = "E:\geo\services\dailyhot-api"
New-Item -ItemType Directory -Force $dst | Out-Null
robocopy $src $dst /E /XD .git node_modules dist logs /XF *.log
```
（`robocopy` 退出码 0–7 均为成功，不要把它当失败。）

- [ ] **Step 2: 确认拷贝结果**

Run: `Get-ChildItem E:\geo\services\dailyhot-api`
Expected: 有 `src/`、`package.json`、`pnpm-lock.yaml`、`Dockerfile`、`.env.example`；**没有** `.git/`、`node_modules/`。

- [ ] **Step 3: 本地装依赖并冒烟启动（验证 vendored 服务可跑）**

Run:
```powershell
cd E:\geo\services\dailyhot-api; pnpm install
```
然后另开一个后台进程跑 `pnpm run dev`，等日志出现 `🔥 DailyHot API successfully runs on port 6688`，再：
```powershell
Invoke-WebRequest http://127.0.0.1:6688/all -UseBasicParsing | Select-Object -ExpandProperty Content
```
Expected: 返回含 `"code":200` 和 `routes` 数组的 JSON。验证完可停掉该进程（后续后端联调时再开）。

- [ ] **Step 4: Commit（纯新增）**

```bash
git add services/dailyhot-api
git commit -m "chore(hot-lists): vendor DailyHotApi as services/dailyhot-api"
```

> 注意：`git add services/dailyhot-api` 只暂存该目录，**不要** `git add -A`，避免误提交 WIP 改动。

---

## Task 1: 后端代理服务 `service.py`（纯新增，TDD）

**Files:**
- Create: `server/app/modules/hot_lists/__init__.py`（空文件，使其成为 package）
- Create: `server/app/modules/hot_lists/service.py`
- Test: `server/tests/test_hot_lists_service.py`

服务用 `httpx.AsyncClient` 转发；上游地址直接读环境变量 `GEO_HOTLIST_API_URL`（默认 `http://127.0.0.1:6688`），**不动 config.py**。函数接受可选 `client` 便于用 `httpx.MockTransport` 测试。

- [ ] **Step 1: 写失败测试**

`server/tests/test_hot_lists_service.py`（纯异步单测，**不需要 MySQL**，用 `asyncio.run` 跑协程，避免引入 pytest-asyncio）：
```python
import asyncio

import httpx

from server.app.modules.hot_lists import service


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_fetch_all_sources_forwards_to_all():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"code": 200, "count": 2, "routes": []})

    result = asyncio.run(service.fetch_all_sources(client=_client(handler)))
    assert captured["url"].endswith("/all")
    assert result["count"] == 2


def test_fetch_source_passes_limit_and_cache_and_returns_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/weibo"
        assert request.url.params.get("limit") == "10"
        assert request.url.params.get("cache") == "false"
        return httpx.Response(200, json={"code": 200, "name": "weibo", "data": []})

    status, payload = asyncio.run(
        service.fetch_source("weibo", limit=10, no_cache=True, client=_client(handler))
    )
    assert status == 200
    assert payload["name"] == "weibo"


def test_fetch_source_maps_request_error_to_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    try:
        asyncio.run(service.fetch_source("weibo", limit=None, no_cache=False, client=_client(handler)))
    except service.HotListUpstreamError:
        return
    raise AssertionError("expected HotListUpstreamError")
```

- [ ] **Step 2: 跑测试确认失败**

Run（Windows，按 CLAUDE.md/记忆用 env python 全路径；DB 无关测试不需 GEO_TEST_DATABASE_URL）：
```powershell
python -m pytest server/tests/test_hot_lists_service.py -q
```
Expected: FAIL（`ModuleNotFoundError: server.app.modules.hot_lists` 或 `AttributeError`）。

- [ ] **Step 3: 写实现**

`server/app/modules/hot_lists/__init__.py`：留空。

`server/app/modules/hot_lists/service.py`：
```python
"""DailyHotApi 代理：把热榜请求转发给独立 Node 服务（默认 127.0.0.1:6688）。

纯转发、无缓存（缓存交给上游自带的 NodeCache）、无 DB。上游地址读环境变量
GEO_HOTLIST_API_URL，不进 Settings（避免与在途 WIP 改动 config.py 冲突）。
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, AsyncIterator

import httpx

_DEFAULT_BASE_URL = "http://127.0.0.1:6688"
_TIMEOUT_SECONDS = 8.0


class HotListUpstreamError(Exception):
    """上游热榜服务不可用（连接失败 / 超时）。"""


def _base_url() -> str:
    return (os.environ.get("GEO_HOTLIST_API_URL") or _DEFAULT_BASE_URL).rstrip("/")


@contextlib.asynccontextmanager
async def _client_ctx(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as owned:
            yield owned


async def fetch_all_sources(client: httpx.AsyncClient | None = None) -> Any:
    url = f"{_base_url()}/all"
    try:
        async with _client_ctx(client) as c:
            resp = await c.get(url)
    except httpx.RequestError as exc:
        raise HotListUpstreamError(str(exc)) from exc
    return resp.json()


async def fetch_source(
    source: str,
    *,
    limit: int | None,
    no_cache: bool,
    client: httpx.AsyncClient | None = None,
) -> tuple[int, Any]:
    params: dict[str, str] = {}
    if limit is not None:
        params["limit"] = str(limit)
    if no_cache:
        params["cache"] = "false"
    url = f"{_base_url()}/{source}"
    try:
        async with _client_ctx(client) as c:
            resp = await c.get(url, params=params)
    except httpx.RequestError as exc:
        raise HotListUpstreamError(str(exc)) from exc
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"code": resp.status_code, "message": resp.text}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_hot_lists_service.py -q`
Expected: PASS（3 passed）。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/hot_lists/__init__.py server/app/modules/hot_lists/service.py server/tests/test_hot_lists_service.py
git commit -m "feat(hot-lists): add DailyHotApi proxy service"
```

---

## Task 2: 后端路由 `router.py`（纯新增，TDD）

**Files:**
- Create: `server/app/modules/hot_lists/router.py`
- Test: `server/tests/test_hot_lists_api.py`（用 `build_test_app`，**需要 MySQL**）

路由：`GET /api/hot-lists`（转发 /all）、`GET /api/hot-lists/{source}`（透传 limit/cache + 上游状态码）。`source` 白名单 `[a-z0-9-]+`。上游错误 → 502。鉴权在 main.py 注册时统一加（Task 3），故路由本身不重复加依赖。

> 注意：Task 2 的 API 测试依赖 Task 3 已把 router 挂到 app 上。若按顺序 TDD，可先写 router + 单元级测试，待 Task 3 注册后再让 `test_hot_lists_api.py` 全绿；或将 Task 3 的 main.py 改动与本 Task 合并执行（但 main.py 改动需用户审批，见 Task 3）。

- [ ] **Step 1: 写失败测试**

`server/tests/test_hot_lists_api.py`（`build_test_app` 在 `server/tests/utils.py`，已核对：`TestApp.client` 是带 admin 登录 cookie 的 `TestClient`，无 `client_no_auth`——未登录请求用 `TestClient(test_app.client.app)` 现造一个不带 cookie 的）：
```python
import pytest
from fastapi.testclient import TestClient

from server.app.modules.hot_lists import service
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def test_get_source_passthrough(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        async def fake_fetch_source(source, *, limit, no_cache, client=None):
            return 200, {"code": 200, "name": source, "data": [{"id": "1", "title": "x", "url": "u"}]}

        monkeypatch.setattr(
            "server.app.modules.hot_lists.service.fetch_source", fake_fetch_source
        )
        resp = test_app.client.get("/api/hot-lists/weibo")
        assert resp.status_code == 200
        assert resp.json()["name"] == "weibo"
    finally:
        test_app.cleanup()


def test_upstream_down_returns_502(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        async def boom(source, *, limit, no_cache, client=None):
            raise service.HotListUpstreamError("down")

        monkeypatch.setattr("server.app.modules.hot_lists.service.fetch_source", boom)
        resp = test_app.client.get("/api/hot-lists/weibo")
        assert resp.status_code == 502
    finally:
        test_app.cleanup()


def test_invalid_source_rejected(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        # 大写不匹配 ^[a-z0-9-]+$ → 400（不打上游）
        resp = test_app.client.get("/api/hot-lists/WEIBO")
        assert resp.status_code == 400
    finally:
        test_app.cleanup()


def test_requires_auth(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        noauth = TestClient(test_app.client.app)  # 不带 access_token cookie
        resp = noauth.get("/api/hot-lists/weibo")
        assert resp.status_code == 401
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$env:GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"; python -m pytest server/tests/test_hot_lists_api.py -q`
Expected: FAIL（路由不存在 → 404，或 import 失败）。

- [ ] **Step 3: 写实现**

`server/app/modules/hot_lists/router.py`：
```python
"""热榜代理路由：/api/hot-lists（列出全部源）、/api/hot-lists/{source}（取某源）。

鉴权在 main.py 注册时统一加（dependencies=[Depends(get_current_user)]）。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from . import service

router = APIRouter()

_SOURCE_RE = re.compile(r"^[a-z0-9-]+$")


@router.get("")
async def list_sources():
    try:
        return await service.fetch_all_sources()
    except service.HotListUpstreamError as exc:
        raise HTTPException(status_code=502, detail="热榜服务不可用") from exc


@router.get("/{source}")
async def get_source(
    source: str,
    limit: int | None = Query(default=None, ge=1, le=500),
    cache: bool = Query(default=True),
):
    if not _SOURCE_RE.match(source):
        raise HTTPException(status_code=400, detail="非法的榜单名")
    try:
        status_code, payload = await service.fetch_source(
            source, limit=limit, no_cache=not cache
        )
    except service.HotListUpstreamError as exc:
        raise HTTPException(status_code=502, detail="热榜服务不可用") from exc
    return JSONResponse(status_code=status_code, content=payload)
```

- [ ] **Step 4: 跑测试**

先确保 Task 3（main.py 注册）已执行（需用户审批）。然后：
Run: `$env:GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"; python -m pytest server/tests/test_hot_lists_api.py -q`
Expected: PASS（4 passed）。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/hot_lists/router.py server/tests/test_hot_lists_api.py
git commit -m "feat(hot-lists): add proxy router with auth + 502 mapping"
```

---

## Task 3: 🔒 注册路由到 main.py（**需用户审批**）

**Files:**
- Modify: `server/app/main.py`（加 import + include_router；不删任何内容）

- [ ] **Step 0: 向用户出示这两处插入，获得同意后再改**

插入点 1 —— import 区（约 `server/app/main.py:54` 附近，audit_router import 之后）新增一行：
```python
from server.app.modules.hot_lists.router import router as hot_lists_router
```

插入点 2 —— include_router 区（约 `server/app/main.py:260` 附近，audit_router 的 `include_router` 块之后）新增一块：
```python
    app.include_router(
        hot_lists_router,
        prefix="/api/hot-lists",
        tags=["hot-lists"],
        dependencies=[Depends(get_current_user)],
    )
```
（已核对：`Depends` 与 `get_current_user` 在 main.py 已 import，pipelines/image-library/audit 三个 `include_router` 块都在用同款 `dependencies=[Depends(get_current_user)]`。）

- [ ] **Step 1: 用户同意后，做上述两处插入**

- [ ] **Step 2: 验证 app 能启动且路由已挂**

Run: `python -c "from server.app.main import create_app; app=create_app(); print([r.path for r in app.routes if 'hot-lists' in getattr(r,'path','')])"`
（需先设置 `GEO_JWT_SECRET` / `GEO_DATA_DIR` / `GEO_DATABASE_URL` 三个必填环境变量，见 CLAUDE.md。）
Expected: 打印出包含 `/api/hot-lists` 和 `/api/hot-lists/{source}` 的列表。

- [ ] **Step 3: 跑 Task 2 的 API 测试，应全绿**

Run: `$env:GEO_TEST_DATABASE_URL="...";  python -m pytest server/tests/test_hot_lists_api.py -q`
Expected: PASS。

- [ ] **Step 4: Commit**

```bash
git add server/app/main.py
git commit -m "feat(hot-lists): mount /api/hot-lists router with auth"
```

---

## Task 4: 前端 API 客户端 + 类型（hot-lists.ts 纯新增）

**Files:**
- Create: `web/src/api/hot-lists.ts`（含类型，**不放进 types.ts**，减少对现有文件的改动）

- [ ] **Step 1: 写实现**

`web/src/api/hot-lists.ts`：
```typescript
import { api } from "./core";

export type HotListSource = { name: string; path: string };

export type HotListItem = {
  id: number | string;
  title: string;
  cover?: string;
  author?: string;
  desc?: string;
  hot?: number;
  timestamp?: number;
  url: string;
  mobileUrl?: string;
};

export type HotListResponse = {
  code: number;
  name: string;
  title: string;
  type: string;
  link?: string;
  total: number;
  updateTime?: string | number;
  fromCache?: boolean;
  data: HotListItem[];
};

type AllResponse = { code: number; count: number; routes: HotListSource[] };

export async function listHotSources(): Promise<HotListSource[]> {
  const res = await api<AllResponse>("/api/hot-lists");
  return res.routes.filter((r) => Boolean(r.path));
}

export function getHotList(
  source: string,
  opts?: { limit?: number; noCache?: boolean },
): Promise<HotListResponse> {
  const params = new URLSearchParams();
  if (opts?.limit) params.set("limit", String(opts.limit));
  if (opts?.noCache) params.set("cache", "false");
  const query = params.toString();
  return api<HotListResponse>(`/api/hot-lists/${source}${query ? `?${query}` : ""}`);
}
```

- [ ] **Step 2: typecheck**

Run: `pnpm --filter @geo/web typecheck`
Expected: 通过（无新错误）。

- [ ] **Step 3: Commit**

```bash
git add web/src/api/hot-lists.ts
git commit -m "feat(hot-lists): add web api client for hot lists"
```

---

## Task 5: 前端「热榜」工作区组件（HotListsWorkspace.tsx 纯新增）

**Files:**
- Create: `web/src/features/hot-lists/HotListsWorkspace.tsx`

自包含组件：左侧源列表 + 右侧条目列表，点条目新标签开原文。样式从简（用户「不计较前端」），用少量内联样式，不依赖未知 CSS class。

- [ ] **Step 1: 写实现**

`web/src/features/hot-lists/HotListsWorkspace.tsx`：
```tsx
import { useEffect, useState } from "react";
import {
  listHotSources,
  getHotList,
  type HotListSource,
  type HotListResponse,
} from "../../api/hot-lists";

export function HotListsWorkspace() {
  const [sources, setSources] = useState<HotListSource[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [data, setData] = useState<HotListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listHotSources()
      .then((list) => {
        setSources(list);
        if (list.length > 0) setCurrent(list[0].name);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "加载榜单列表失败"));
  }, []);

  function load(source: string, noCache = false) {
    setLoading(true);
    setError(null);
    getHotList(source, { noCache })
      .then((res) => setData(res))
      .catch((e) => {
        setData(null);
        setError(e instanceof Error ? e.message : "加载热榜失败");
      })
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (current) load(current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  return (
    <div style={{ display: "flex", gap: 16, height: "100%", padding: 16 }}>
      <aside style={{ width: 180, overflowY: "auto", borderRight: "1px solid #e5e7eb" }}>
        {sources.map((s) => (
          <button
            key={s.name}
            type="button"
            onClick={() => setCurrent(s.name)}
            style={{
              display: "block",
              width: "100%",
              textAlign: "left",
              padding: "6px 10px",
              border: "none",
              background: current === s.name ? "#eef2ff" : "transparent",
              cursor: "pointer",
              fontWeight: current === s.name ? 600 : 400,
            }}
          >
            {s.name}
          </button>
        ))}
      </aside>
      <section style={{ flex: 1, overflowY: "auto" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>
            {data ? `${data.title} · ${data.type}` : current ?? "热榜"}
          </h2>
          {data?.updateTime && (
            <span style={{ color: "#6b7280", fontSize: 12 }}>更新于 {String(data.updateTime)}</span>
          )}
          {current && (
            <button type="button" onClick={() => load(current, true)} disabled={loading}>
              {loading ? "刷新中…" : "刷新"}
            </button>
          )}
        </div>
        {error && <p role="alert" style={{ color: "#dc2626" }}>{error}</p>}
        {loading && !data && <p>加载中…</p>}
        {data && data.data.length === 0 && !loading && <p>暂无数据</p>}
        <ol style={{ paddingLeft: 0, listStyle: "none", margin: 0 }}>
          {data?.data.map((item, idx) => (
            <li key={String(item.id)} style={{ padding: "8px 0", borderBottom: "1px solid #f3f4f6" }}>
              <a
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
                style={{ display: "flex", gap: 10, alignItems: "baseline", textDecoration: "none", color: "inherit" }}
              >
                <span style={{ color: "#9ca3af", width: 24, textAlign: "right" }}>{idx + 1}</span>
                <span style={{ flex: 1 }}>{item.title}</span>
                {typeof item.hot === "number" && (
                  <span style={{ color: "#f97316", fontSize: 12 }}>{item.hot.toLocaleString()}</span>
                )}
              </a>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
```

- [ ] **Step 2: typecheck**

Run: `pnpm --filter @geo/web typecheck`
Expected: 通过。

- [ ] **Step 3: Commit**

```bash
git add web/src/features/hot-lists/HotListsWorkspace.tsx
git commit -m "feat(hot-lists): add HotListsWorkspace component"
```

---

## Task 6: 🔒 在 types.ts 注册 tab（**需用户审批**）

**Files:**
- Modify: `web/src/types.ts`（`NavKey` 联合类型加成员 + `navItems` 加一项；不删任何内容）

- [ ] **Step 0: 向用户出示这两处修改，获得同意后再改**

修改 1 —— `web/src/types.ts:4` 的 `NavKey`，在联合里加 `"hot-lists"`：
```typescript
export type NavKey = "agents" | "ai" | "content" | "prompts" | "image-library" | "media" | "tasks" | "system" | "hot-lists" | "admin" | "audit-logs";
```

修改 2 —— `web/src/types.ts:1` 的 lucide import 加 `Flame`，并在 `navItems`（约 `:452`）数组末尾（system 之后）加一项：
```typescript
// import 行加入 Flame：
import { Bot, FileText, Flame, Images, MessagesSquare, MonitorCog, RadioTower, Send, Sparkles } from "lucide-react";

// navItems 末尾追加：
  { key: "hot-lists", label: "热榜", icon: Flame },
```

- [ ] **Step 1: 用户同意后，做上述修改**

- [ ] **Step 2: typecheck**

Run: `pnpm --filter @geo/web typecheck`
Expected: 通过。

- [ ] **Step 3: Commit**

```bash
git add web/src/types.ts
git commit -m "feat(hot-lists): register 热榜 nav tab"
```

---

## Task 7: 🔒 在 App.tsx 渲染 tab（**需用户审批**）

**Files:**
- Modify: `web/src/App.tsx`（加 import + 渲染块；不删任何内容）

- [ ] **Step 0: 向用户出示这两处插入，获得同意后再改**

插入 1 —— import 区（约 `web/src/App.tsx:13`，SystemWorkspace import 附近）：
```typescript
import { HotListsWorkspace } from "./features/hot-lists/HotListsWorkspace";
```

插入 2 —— workspaceInner 内（约 `web/src/App.tsx:160`，system 渲染块之后）追加：
```tsx
            {visitedTabs.has("hot-lists") && (
              <div style={{ display: activeNav === "hot-lists" ? undefined : "none" }}>
                <ErrorBoundary fallback={<p role="alert">热榜出错，请刷新重试</p>}>
                  <HotListsWorkspace />
                </ErrorBoundary>
              </div>
            )}
```

- [ ] **Step 1: 用户同意后，做上述插入**

- [ ] **Step 2: typecheck + build**

Run: `pnpm --filter @geo/web typecheck; pnpm --filter @geo/web build`
Expected: 都通过。

- [ ] **Step 3: 端到端手测（本地）**

启动 vendored 服务 + 后端（`uvicorn server.app.main:app --port 8000`）+ 前端（`pnpm --filter @geo/web dev`），登录后点「热榜」tab。

> **vendored 服务启动注意（已实测）**：先 `pnpm install --ignore-workspace`（否则 pnpm 会上溯到 geo 根 workspace、不装本目录依赖）。`pnpm run dev`（tsx watch）在非交互/后台 shell 里**起不来**；本地交互终端用 `pnpm run dev` 正常，脚本/后台用非 watch：`node node_modules/tsx/dist/cli.mjs src/index.ts`（实测能在 6688 起服务）。生产走其自带 Dockerfile，不受影响。
Expected: 左侧出现源列表，点微博/知乎等能看到条目，点条目新标签开原文，刷新按钮可强刷。

- [ ] **Step 4: Commit**

```bash
git add web/src/App.tsx
git commit -m "feat(hot-lists): render 热榜 tab in app shell"
```

---

## Task 8: 🔒 生产 docker-compose 接入（**需用户审批**；可最后做 / 可选）

**Files:**
- Modify: `docker-compose.yml`（加 `dailyhot-api` 服务 + 给 app 加 env + depends_on；不删任何内容）

- [ ] **Step 0: 向用户出示改动，获得同意后再改**

新增服务（与 minio/mysql 同级）：
```yaml
  dailyhot-api:
    build: ./services/dailyhot-api
    restart: unless-stopped
    environment:
      NODE_ENV: docker
      PORT: 6688
    # 仅内部网络，不映射宿主端口
```
给 `app` 服务的 `environment` 加一行：
```yaml
      GEO_HOTLIST_API_URL: http://dailyhot-api:6688
```
给 `app` 服务的 `depends_on` 加：
```yaml
      dailyhot-api:
        condition: service_started
```

> 执行时确认 vendored 的 `services/dailyhot-api/Dockerfile` 在 `NODE_ENV=docker` 下会自启服务（其 `src/index.ts` 在 `NODE_ENV` 为 development/docker 时调用 `serveHotApi`）。若 Dockerfile 默认 `NODE_ENV` 不是这两者，需在此处显式设 `NODE_ENV=docker`（上面已设）。

- [ ] **Step 1: 用户同意后改 docker-compose.yml**

- [ ] **Step 2: 校验 compose 语法**

Run: `docker compose config`
Expected: 输出合并后的配置，无报错，能看到 `dailyhot-api` 服务和 app 的 `GEO_HOTLIST_API_URL`。

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(hot-lists): wire dailyhot-api service into docker-compose"
```

---

## 收尾验证

- [ ] 后端：`python -m pytest server/tests/test_hot_lists_service.py server/tests/test_hot_lists_api.py -q`（API 测试需 `GEO_TEST_DATABASE_URL`）全绿。
- [ ] 前端：`pnpm --filter @geo/web typecheck` + `build` 全绿（CI 硬门禁）。
- [ ] 手测：本地三件套起来后「热榜」tab 正常展示与跳转。
- [ ] 确认未误改 WIP 文件：`git status` 中 ai_format.py / ai_illustrate.py / baidu.py / image_library/service.py / config.py / requirements.txt 仍是原 WIP 状态、未被本次提交带走。

## 风险 / 注意

- **绝不 `git add -A`**：工作区有不相关 WIP，所有提交按文件精确 `git add`。
- 测试环境：Windows 下 conda activate 在工具 shell 不生效，用 `python -m pytest`；MySQL 测试需 `GEO_TEST_DATABASE_URL`（库名含 "test"）。
- `build_test_app` 已核对在 `server/tests/utils.py`；`TestApp.client` 是带 admin cookie 的 `TestClient`，未登录测试用 `TestClient(test_app.client.app)`。
- vendored 服务首启需 `pnpm install`；生产靠其自带 Dockerfile 构建。
