# 设计方案：Geo「热榜」tab（集成 DailyHotApi）

- 日期：2026-06-09
- 状态：设计已确认，待写实现计划
- 作者：lufeng + Claude

## 背景与目标

把开源项目 **DailyHotApi**（`imsyy/DailyHotApi`，本地在 `E:\agent_study\DailyHotApi`）的热榜能力融入 Geo，在 Geo 前端新增一个「热榜」tab 做展示。

DailyHotApi 现状：

- 独立 **Node.js / Hono** 服务，默认端口 `6688`。
- 56+ 热榜路由（微博、知乎、B 站、抖音、头条、掘金、GitHub Trending……），每个路由返回统一 JSON：
  ```json
  { "code": 200, "name": "weibo", "title": "微博", "type": "热搜榜",
    "link": "...", "updateTime": "...", "fromCache": true, "total": 50,
    "data": [{ "id": "...", "title": "...", "cover": "...", "hot": 123,
               "timestamp": 0, "url": "...", "mobileUrl": "..." }] }
  ```
- `GET /all` 返回全部可用榜单 `{ code, count, routes: [{ name, path }] }`。
- 自带缓存：**Redis 可选**（lazy-connect，连不上自动降级），否则用内存 `NodeCache`（默认 TTL 3600s）。**不需要部署 Redis。**
- 查询参数：`?cache=false`（强刷）、`?limit=N`（限制条数）、`?rss=true`（RSS 输出，本项目不用）。

Geo 现状：

- 后端 FastAPI + SQLAlchemy（MySQL only），所有路由挂 `/api/` 下、默认走 `Depends(get_current_user)` JWT cookie 鉴权。
- 前端 React 19 + Vite，tab 在 `web/src/types.ts` 的 `navItems` 注册、`web/src/App.tsx` 按 `visitedTabs` 懒加载渲染；前端只认 `/api`，Vite dev 代理 `/api` → `127.0.0.1:8000`，无跨源。
- `httpx==0.28.1` 已是依赖（`server/app/shared/baidu.py` 已在用 `httpx.get/post`）。

## 已确认的决策（来自 brainstorming 问答）

1. **数据来源方式**：Node 服务 + FastAPI 代理。DailyHotApi 原样作为独立 Node 服务跑，**不改它的代码**；Geo 后端新增轻量代理转发，复用 Geo 鉴权 + 同源。
2. **榜单范围**：全部 56 个，前端调 `/all` 动态列出，上游新增榜单自动出现，无需改代码。
3. **联动**：**只看，不联动**。点条目在新标签打开原文 `url`，不与 Geo 内容/AI 生文打通（后续可另开单做「送去 AI 生文」）。
4. **代码管理**：把 DailyHotApi 源码 **vendor 进 Geo 仓库** `services/dailyhot-api/`（去掉它自己的 `.git` 和 `node_modules`），随仓库版本化，docker-compose 可复现构建。

## 总体架构

```
浏览器 ──/api/hot-lists/*──> FastAPI (Geo, :8000) ──HTTP──> DailyHotApi (Node, :6688) ──爬取/缓存──> 各平台
        (同源, 带 JWT)         轻量代理, 鉴权             services/dailyhot-api, 自带 NodeCache
```

三块改动：DailyHotApi 原样跑（vendored）、Geo 后端加代理模块、Geo 前端加 tab。缓存完全交给上游，代理不另做缓存。

## 组件设计

### 1. 后端代理模块 `server/app/modules/hot_lists/`

纯代理、**无 DB 模型 / 无迁移**。两个文件：

- `service.py`
  - 用 `httpx` 向上游转发（`httpx` 已是依赖）。**用 `httpx.AsyncClient` + async 路由**（代理是 I/O 密集，async 不阻塞事件循环；优于 `baidu.py` 的同步惯例）。
  - 上游地址读新配置项 `GEO_HOTLIST_API_URL`（在 `server/app/core/config.py` 的 `Settings` 加，前缀 `GEO_`，默认 `http://127.0.0.1:6688`）。
  - 超时用一个合理默认（如 8s，略大于上游 `REQUEST_TIMEOUT=6s`）。
  - 函数：`fetch_all_sources()` → 转发 `/all`；`fetch_source(source, *, limit, no_cache)` → 转发 `/{source}` 并透传 `limit` / `cache` 查询参数。
  - 错误处理：上游超时 / 连接失败 → 抛出映射到 HTTP 502 的异常（见下）；上游返回 4xx/5xx → 透传其状态码与 body。

- `router.py`
  - `GET /api/hot-lists` → 转发上游 `/all`，返回 `{ count, routes: [{ name, path }] }`。
  - `GET /api/hot-lists/{source}` → 转发上游 `/{source}`，接受查询参数 `limit`（int，可选）、`cache`（bool，默认 true；`false` 透传给上游强刷），原样回 JSON。
  - `source` 用路径参数；只允许 `[a-z0-9-]+`（白名单字符，避免被当作路径穿越或注入到上游 URL）。
  - 在 `server/app/main.py:create_app()` 用 `app.include_router(hot_lists_router, prefix="/api/hot-lists", tags=["hot-lists"], dependencies=[Depends(get_current_user)])` 注册（与 pipelines / image-library 同款鉴权写法）。

错误处理细节：上游不可用时返回 `502`，body `{ "detail": "热榜服务不可用" }`。实现方式可在 router 内 `try/except httpx.RequestError` 后 `raise HTTPException(status_code=502, detail=...)`（FastAPI 标准），不依赖 Geo 的 ConflictError/ClientError 全局映射（那些映射到 409/400，语义不符）。

### 2. 前端「热榜」tab

- `web/src/types.ts`
  - `NavKey` 增加 `"hot-lists"`。
  - `navItems` 增加 `{ key: "hot-lists", label: "热榜", icon: Flame }`（lucide `Flame` 图标）。
  - 增加类型 `HotListSource = { name: string; path: string }` 与 `HotListItem`（对应上游 `data[]`：`id, title, cover?, author?, desc?, hot?, timestamp?, url, mobileUrl`）与 `HotListResponse`（`name, title, type, link?, total, updateTime, fromCache, data: HotListItem[]`）。
- `web/src/api/hot-lists.ts`
  - `listHotSources(): Promise<HotListSource[]>` → `GET /api/hot-lists`。
  - `getHotList(source, opts?: { limit?: number; noCache?: boolean }): Promise<HotListResponse>` → `GET /api/hot-lists/{source}`。
  - 走现有 fetch 封装（与其它 `web/src/api/*.ts` 一致，带 cookie / 错误处理）。
- `web/src/features/hot-lists/HotListsWorkspace.tsx`
  - 左侧：源列表（来自 `listHotSources`），点选切换当前源。
  - 右侧：当前源的条目列表（序号、标题、热度 `hot`、可选 `cover` 缩略图）；点条目用 `window.open(item.url, "_blank", "noopener")` 打开原文。
  - 顶部：标题（`title` + `type`）、`updateTime`、刷新按钮（带 `noCache=true` 强刷）。
  - 加载 / 空 / 错误三态；样式复用现有 `styles.css` 既有类，不追求精致（用户明确「不计较前端」）。
- `web/src/App.tsx`
  - 按现有 `visitedTabs` 懒加载模式，加 `{visitedTabs.has("hot-lists") && (<div style={{display: activeNav === "hot-lists" ? undefined : "none"}}><ErrorBoundary fallback={...}><HotListsWorkspace /></ErrorBoundary></div>)}`。

### 3. Vendoring：`services/dailyhot-api/`

- 把 `E:\agent_study\DailyHotApi` 的源码复制到 `E:\geo\services\dailyhot-api\`，**剔除** `.git/`、`node_modules/`、`dist/`、`logs/`（依赖 `.dockerignore` / 不拷）。
- 保留其 `Dockerfile`、`package.json`、`pnpm-lock.yaml`、`src/`、`.env.example` 等。
- 不改其源码。

## 部署 / 运行

### 本地开发（Windows）

1. 另开终端：`cd E:\geo\services\dailyhot-api && pnpm install && pnpm run dev`（监听 6688）。
2. Geo 后端代理默认 `GEO_HOTLIST_API_URL=http://127.0.0.1:6688`，无需额外配置。
3. 前端 `pnpm --filter @geo/web dev`（5173），`/api` 代理到 8000，热榜 tab 即可用。

### 生产 docker-compose

- 新增服务 `dailyhot-api`：`build: ./services/dailyhot-api`，`restart: unless-stopped`，仅内部网络（不映射宿主端口），环境可设 `NODE_ENV=docker`（其入口在 docker/development 时才自启 server）。
- 给 `app` 服务加环境变量 `GEO_HOTLIST_API_URL: http://dailyhot-api:6688`，并在 `depends_on` 加 `dailyhot-api`。
- `worker` 服务**不需要**该变量（不提供热榜接口）。
- **不需要 Redis**；如将来要加可另配，上游会自动用上。
- nginx 是公共入口，`/api/hot-lists/*` 经 app 转发到内部 `dailyhot-api`，前端无跨源。

## 错误与边界

- 上游服务未起 / 超时：代理返回 502 `{detail:"热榜服务不可用"}`；前端展示「热榜服务暂不可用，请稍后重试」。
- 未知 source：上游回 404，代理透传 404。
- 未登录：`dependencies=[Depends(get_current_user)]` → 401。
- `source` 参数白名单 `[a-z0-9-]+`，拒绝非法字符（防穿越 / SSRF 拼接）。
- 大 body：上游单榜最多几十条，体量小，不设特殊处理。

## 测试

- 后端（`build_test_app` + monkeypatch 掉 `service` 里的 httpx 调用，避免真打外网）：
  1. `GET /api/hot-lists` 转发 `/all` 并返回 routes。
  2. `GET /api/hot-lists/weibo` 转发并原样回 JSON；`limit` / `cache` 参数透传。
  3. 上游抛 `httpx.RequestError` → 502。
  4. 非法 `source`（含非白名单字符）→ 400 / 404。
  5. 未带 JWT → 401。
- 前端：`pnpm --filter @geo/web typecheck` + `build`（CI 硬门禁）。
- DailyHotApi 自身不写新测试（vendored，不改源码）。

## 不做（YAGNI / 后续另开单）

- 热榜条目 → AI 生文 / 问题池的「送去」联动按钮。
- 代理层二级缓存（依赖上游自带缓存）。
- 收藏 / 历史 / 多源聚合首页。
- RSS 输出透传。
- Redis 部署。

## 影响面

- 新增：`server/app/modules/hot_lists/{service.py,router.py}`、`web/src/api/hot-lists.ts`、`web/src/features/hot-lists/HotListsWorkspace.tsx`、`services/dailyhot-api/`（vendored）、docker-compose 一个服务 + 一个 env。
- 改动：`server/app/core/config.py`（加 `GEO_HOTLIST_API_URL`）、`server/app/main.py`（注册 router）、`web/src/types.ts`（NavKey + navItems + 类型）、`web/src/App.tsx`（渲染块）、`docker-compose.yml`。
- 无数据库迁移、无 DB 模型。
```
