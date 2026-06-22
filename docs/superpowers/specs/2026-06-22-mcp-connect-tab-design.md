# MCP 接入 tab + 外网多机接入设计稿（2026-06-22）

> 范围：`feat/geo-mcp-loop` 分支 PR 收尾前的最后一刀。把 MCP loop POC 从「需要本机 dev 才能用」拉到「外网部署后多台机器抄一份配置就能接」的状态。
>
> 配套：`docs/superpowers/specs/2026-06-18-claude-code-loop-with-geo-mcp-design.md`（POC 主干设计）、`docs/mcp-setup-notes.md`（本机开发配置）、`docs/mcp-demo-walkthrough.md`（老板演示稿）。本稿只补「公网部署」+「前端接入指引 tab」两块。

## 1. 背景与目标

POC 已经能跑通：本机的 Claude Code 通过 `python -m server.mcp`（FastMCP stdio）spawn 子进程，子进程 HTTP 调本机 GEO 后端，跑生文 / 分发 / 评估周报三条 Loop。

但要把它推向「外网部署 + 团队其他成员接入」时，门槛集中在两处：

1. **每台外部机器都要克隆仓库 + 装 Python 依赖**——MCP server 走 stdio，必须在本机能起 Python 进程
2. **配置散在文档里**——`~/.claude.json` 的 JSON 块、`GEO_MCP_TOKEN`、`GEO_API_BASE_URL`、`PYTHONPATH`，每个值都要正确，错一处就 401/no tools

目标：
- 在 GEO 前端加一个**全员可见**的「MCP 接入」tab，集成「服务端状态指示 / 客户端配置 JSON 模板 / 一键复制 / 测试连接 / 故障排查」五段
- 落地一份**外网部署 + 多机接入**的可执行文档（同 PR 内）
- 同时把 PR 创建前的两个阻塞项处理掉：alembic 0047 同号双 head + 未提交 WIP

**非目标**（明确不在本稿范围）：
- HTTP/SSE transport（FastMCP `streamable-http`）—— 留作后续独立 PR
- per-user MCP token（目前 POC 共享一个）—— 留作后续独立 spec
- MCP wheel 打包 —— 留作后续独立 PR

## 2. 整体架构（部署形态）

```
┌────────────────────────────────┐        ┌────────────────────────────────────┐
│ 外部机器（同事 A 的电脑）       │        │ 公网服务器（GEO 部署机）            │
│                                │        │                                    │
│  Claude Code                   │        │  Nginx / Caddy（HTTPS 收口）        │
│    ├ ~/.claude.json            │        │    ├─→ FastAPI :8000               │
│    │   mcpServers.geo          │        │    │     /api/mcp/status (user JWT)│
│    │     command: python       │        │    │     /api/mcp/health (MCP tok) │
│    │     args: -m server.mcp   │        │    │     /api/articles/* 等        │
│    │     env:                  │        │    │                                │
│    │       GEO_API_BASE_URL    │   HTTPS│    │     .env: GEO_MCP_TOKEN=xxx   │
│    │       GEO_MCP_TOKEN       │←──────→│    │                                │
│    │       PYTHONPATH          │        │    └─→ MySQL / MinIO / Worker      │
│    │                           │        │                                    │
│    └ spawn 子进程              │        │                                    │
│       python -m server.mcp     │        │                                    │
│       （stdio MCP server）     │        │                                    │
│       └→ HTTP X-MCP-Token →─── │ ──────→│                                    │
│          访问 /api/* 工具集    │        │                                    │
│                                │        │                                    │
│  本地 clone 的 geo-collab      │        │                                    │
│    用作 PYTHONPATH 跑 MCP      │        │                                    │
└────────────────────────────────┘        └────────────────────────────────────┘
```

关键不变量：
- MCP server 仍是**本机 stdio 子进程**，不暴露端口
- 外部机器与公网服务器之间**只**有 HTTPS + `X-MCP-Token` 这一条线
- 服务端 token 通过 `.env` 注入，**永远不**经任何 API 下发原文（前端 tab 也不显示）

## 3. 前端：「MCP 接入」tab

### 3.1 入口

- `web/src/types.ts` `NavKey` 加 `"mcp"`
- `navItems` 数组末尾追加：`{ key: "mcp", label: "MCP 接入", icon: <某 Lucide 图标>, children: [] }`
- `web/src/App.tsx` 顶部 lazy import `McpConnectWorkspace`、`visitedTabs` 集合管理、`ScrollPanel` + `ErrorBoundary` 包装，照搬 `HotListsWorkspace` 模板
- **全员可见**（不区分 admin），与 admin-only 的「用户管理 / 审计日志 / AI 模型」并列但**位于 admin 三项之前**——保持 admin 入口集中在最底部

### 3.2 内容（五段）

文件：`web/src/features/mcp/McpConnectWorkspace.tsx`

#### 段 ①「概览」

- 一句话说明：「Claude Code 通过 MCP 协议调用 GEO 平台工具，自动跑生文 / 分发 / 评估 Loop。」
- 工具数：`{tools_count}` 个 atomic tools（数字从 `GET /api/mcp/status` 拿）
- Loop 配方链接：`claude-loops/generation-loop.md` / `distribute-loop.md` / `weekly-report-loop.md`（仅文字，不做内嵌渲染）

#### 段 ②「服务端状态」

- 调 `GET /api/mcp/status` 显示状态卡片：
  - `configured: true` → 绿色 badge「服务端 token 已配置 ✓」
  - `configured: false` → 红色 badge「服务端 token 未配置 ⚠️ 请联系 admin」
- 显示「建议的 `GEO_API_BASE_URL`」=`suggested_base_url`（后端拼，见 4.1）
- 显示注册工具数

#### 段 ③「客户端配置」

- 完整 `~/.claude.json` 片段 code block：
  ```json
  {
    "mcpServers": {
      "geo": {
        "command": "python",
        "args": ["-m", "server.mcp"],
        "env": {
          "GEO_MCP_TOKEN": "<PASTE_YOUR_TOKEN_HERE>",
          "GEO_API_BASE_URL": "{suggested_base_url}",
          "PYTHONPATH": "<PATH_TO_YOUR_LOCAL_geo-collab_CLONE>"
        }
      }
    }
  }
  ```
- 「复制 JSON」按钮（`navigator.clipboard.writeText`）
- 下方说明 3 条 bullet：
  - `GEO_MCP_TOKEN`：找 admin 获取
  - `GEO_API_BASE_URL`：默认填的是当前你浏览器看到的域名；如果你的 Claude Code 跑在容器里访问宿主，改成 `http://host.docker.internal:8000`
  - `PYTHONPATH`：在自己机器上 `git clone https://github.com/geo-ihuanlegame/geo-collab.git` 后，填克隆出来的绝对路径；Windows 注意双反斜杠

#### 段 ④「测试连接」

- 文本输入框（type=password，避免肩窥）+ 「测试」按钮
- 点击 → 前端 fetch `GET /api/mcp/health` with header `X-MCP-Token: <input>`
- 三种结果：
  - 200 → 绿色「✓ token 正确，网络可达」
  - 401 → 红色「✗ token 错或服务端未配置」
  - 其他/网络错 → 红色「✗ 网络错误：<message>」
- **说明文案**：本次测试只验证「token + 网络可达」，不验证「Claude Code 能不能起 MCP 子进程」——后者要回到 Claude Code 里 `/mcp` 自查

#### 段 ⑤「故障排查」

- 默认折叠（`<details>`），4 条问答：
  - `401 MCP token not configured` → 后端 `.env` 没读到 `GEO_MCP_TOKEN`，让 admin 检查 + 重启 uvicorn
  - `401 invalid MCP token` → 两边 token 不一致，对照本页段 ④ 测试
  - `geo · connected · no tools` → Claude Code 配的命令是 `python -m server.mcp.server`，改成 `python -m server.mcp`
  - 看不到 `geo` server → `~/.claude.json` JSON 格式坏 / Claude Code 没重启 / `PYTHONPATH` 不对

### 3.3 API client

`web/src/api/mcp.ts`：

```ts
export type McpStatus = {
  configured: boolean;
  suggested_base_url: string;
  tools_count: number;
};

export async function getMcpStatus(): Promise<McpStatus> { ... }

export async function pingMcpHealth(token: string): Promise<{ ok: true } | { ok: false; status: number; message: string }> { ... }
```

`getMcpStatus` 走默认的 `fetchJson`（带 user JWT cookie）；`pingMcpHealth` 手写 fetch，**手动塞 `X-MCP-Token` header**，**不**带 cookie（带也无害，后端不看），错误码 200/401/其它分别返回结果对象。

### 3.4 移动端

`MobileMorePage` 的 nav 列表加一项即可，icon 复用同一个。tab 内容自适应（沿用既有 workspace mobile 样式，不做额外定制）。

## 4. 后端：两个新端点

### 4.1 `GET /api/mcp/status`

- 鉴权：**user JWT**（`Depends(get_current_user)`）—— 全员可见
- 返回：
  ```json
  {
    "configured": true,
    "suggested_base_url": "https://geo.example.com",
    "tools_count": 17
  }
  ```
- `configured` = `bool(get_settings().mcp_token)`
- `suggested_base_url`：用 `str(request.base_url).rstrip("/")` 拿，不读环境变量也不存配置——这样前端永远看到「你浏览器现在看到的域名」。**反代部署须知**：Nginx/Caddy 必须把 `X-Forwarded-Proto` / `X-Forwarded-Host` 传下来，且 FastAPI 启动要带 `--proxy-headers --forwarded-allow-ips="*"`（或在代码里 `Uvicorn.run(..., proxy_headers=True)`），否则 `request.base_url` 会返回 `http://127.0.0.1:8000` 而不是公网域名。**docs/mcp-setup-notes.md 公网部署节里要写这条**。**本机 localhost 兜底**：如果检测到 host 是 `127.0.0.1` / `localhost`，前端段 ③ 在 JSON 模板下方加一句红字提示「你看到的是本机地址，外部机器复制时把 `GEO_API_BASE_URL` 改成公网域名」
- `tools_count`：硬编码常量 17（与 `claude-loops/*.md` 文档一致），值定义在 `server/app/modules/mcp_catalog/connect.py` 顶部 `_MCP_TOOLS_COUNT = 17`。后续如果新增 MCP tool，同时改这个常量；不通过 import `server.mcp.server` 来读，避免把 FastMCP 依赖拖进 web 进程

### 4.2 `GET /api/mcp/health`

- 鉴权：**MCP token**（`Depends(require_mcp_token)`）—— 与现有 MCP-facing endpoint 一致
- 返回：`{ "ok": true }`
- 仅作 token + 网络可达性探测；不查 DB、不调外部，毫秒级返回

### 4.3 文件归属

两个 endpoint 放在新文件 `server/app/modules/mcp_catalog/connect_router.py`，复用 `mcp_catalog` 模块（该模块已存在于 WIP 中）。`server/app/main.py` 注册：

```python
from server.app.modules.mcp_catalog.connect_router import (
    mcp_connect_user_router,  # user JWT
    mcp_connect_token_router,  # MCP token
)
app.include_router(mcp_connect_user_router, prefix="/api/mcp", dependencies=[Depends(get_current_user)])
app.include_router(mcp_connect_token_router, prefix="/api/mcp", dependencies=[Depends(require_mcp_token)])
```

**注意**：`/api/mcp/status` 与 `/api/mcp/health` 必须挂在不同的子 router——不能在同一个 router 上混挂两种鉴权 dependency。

### 4.4 测试

新增 `server/tests/test_mcp_connect.py`：
- `test_status_admin_sees_configured_true`：admin 登录，`mcp_token` 设值，断言 `configured=true`、`tools_count=17`
- `test_status_user_sees_configured_false_when_unset`：非空 user 登录、`mcp_token=""`，断言 `configured=false`
- `test_status_requires_auth`：不带 cookie 调，断言 401
- `test_status_suggested_base_url_from_request`：mock TestClient base_url，断言 `suggested_base_url` 与 host 一致（含 trailing slash 去除）
- `test_status_suggested_base_url_respects_forwarded_proto`：模拟带 `X-Forwarded-Proto: https` / `X-Forwarded-Host: geo.example.com` 的请求（需 ProxyHeadersMiddleware 配置正确），断言 `suggested_base_url == "https://geo.example.com"`
- `test_health_ok_with_token`：正确 `X-MCP-Token` 调，断言 `{"ok": true}`
- `test_health_wrong_token`：错误 token 401
- `test_health_no_token_when_disabled`：服务端未配 token，任意 token 401（沿用 `require_mcp_token` 行为）

## 5. 外网部署文档（`docs/mcp-setup-notes.md` 新增节）

在现有文档底部追加 `## 公网部署 + 多机接入`，覆盖：

1. **服务端**（一次性）
   - 公网服务器跑 docker compose 起 GEO 后端
   - Nginx/Caddy 反代 `https://geo.example.com` → `127.0.0.1:8000`，必须**透传** `X-Forwarded-Proto` / `X-Forwarded-Host`（Caddy 默认透传；Nginx 在 location 块写 `proxy_set_header X-Forwarded-Proto $scheme;` / `proxy_set_header X-Forwarded-Host $host;`）
   - uvicorn 启动加 `--proxy-headers --forwarded-allow-ips="*"`（docker-compose 里 `command` / Dockerfile CMD 改一下）
   - `.env` 加 `GEO_MCP_TOKEN=<openssl rand -hex 32>` 并 `docker compose restart app`
   - 验证：浏览器开 `https://geo.example.com` → 登录 → 进「MCP 接入」tab，段 ② 应显示「✓」

2. **每台客户端机器**
   - `git clone https://github.com/geo-ihuanlegame/geo-collab.git`
   - `cd geo-collab && python -m venv .venv && .venv/Scripts/activate`（PowerShell）/ `source .venv/bin/activate`（bash）
   - `pip install -r requirements-mcp.txt`（**本 PR 同时新增**：从 `requirements.txt` 抽出 MCP server 真正需要的子集——`mcp`、`httpx`、`pydantic`、`pydantic-settings`、`python-dotenv` 几个，避免装上 sqlalchemy/playwright 等无关重依赖）
   - 编辑 `~/.claude.json`：从 GEO 前端「MCP 接入」tab 段 ③ 复制 JSON，替换 token + PYTHONPATH
   - 重启 Claude Code，输入 `/mcp` 验证 `geo: connected` + 工具数 17

3. **安全须知**
   - token 与用户密码同等敏感，不要在 wiki / 群聊明文传，建议 1Password / Bitwarden 或私聊
   - 公网部署**必须 HTTPS**——明文 HTTP 等于 token 明传
   - POC 期所有客户端共享同一个 token，**任一台机器泄露即影响全员**；下一步引入 per-user token 后再分发

4. **可选：一键脚本**（本 PR 是否落由实现阶段决定，spec 这里只占位）
   - `scripts/setup-mcp-client.ps1` / `setup-mcp-client.sh`：clone + venv + pip install + 提示用户编辑 `~/.claude.json`
   - 优先级低于核心 tab 与端点，时间允许就做、不够就跳过

## 6. PR 准备工作（feat/geo-mcp-loop 收尾）

### 6.1 alembic 同号双 head 解决

main 已有 `server/alembic/versions/0047_account_dedup_sharing.py`（`revision="0047"`, `down_revision="0046"`）。

本 PR 在合并前修：
- `git mv server/alembic/versions/0047_auto_review_decisions.py server/alembic/versions/0048_auto_review_decisions.py`
- 文件内修改：
  - 模块 docstring 头部 `修订 ID: 0047` → `修订 ID: 0048`
  - `revision: str = "0047"` → `revision: str = "0048"`
  - `down_revision: str | None = "0046"` → `down_revision: str | None = "0047"`
- 验证：本机 MySQL 上 `alembic upgrade head` 线性升到 0048 且 `alembic history` 看到 `0046 → 0047 (account_dedup_sharing) → 0048 (auto_review_decisions)`

### 6.2 未提交 WIP 整理（24 个文件）

按语义切 commit，不要一锅出：

- **Commit C1（`feat(mcp): save_article 工具替代 compose_article`）**：
  - `D` `server/app/modules/ai_generation/compose_once.py`、`D` `server/tests/test_compose_once.py`
  - `M` `server/app/modules/articles/router.py`（新增 `/save-from-mcp` 端点，复用 `markdown_to_tiptap`）
  - `M` `server/mcp/tools/action.py`（删 `compose_article`、加 `save_article`）
  - `M` `server/app/modules/ai_generation/router.py`（移除 compose-once 路由挂载）
  - `M` `server/app/core/config.py`（如果有相关 setting 删除）
  - `?? server/tests/test_save_article_mcp.py`

- **Commit C2（`feat(mcp): mcp_catalog 模块 + mcp_errors helper`）**：
  - `?? server/app/modules/mcp_catalog/`（含 `__init__.py` 与 catalog router）
  - `?? server/app/core/mcp_errors.py`、`?? server/tests/test_mcp_errors.py`、`?? server/tests/test_mcp_catalog.py`
  - `M` `server/mcp/tools/catalog.py`、`M` `server/mcp/server.py`、`?? server/mcp/__main__.py`、`?? server/tests/test_mcp_entry.py`
  - `M` `server/app/main.py`（注册 mcp_catalog router）
  - `M` `server/app/modules/auto_review/router.py`（异常包装走 mcp_errors）

- **Commit C3（`chore(alembic): 0047 → 0048 同号冲突规避`）**：
  - rename + revision/down_revision 字段更新

- **Commit C4（`docs(mcp): generation-loop 配方零配置版 + 演示稿/setup notes 更新`）**：
  - `M` `claude-loops/generation-loop.md`、`M` `docs/mcp-demo-walkthrough.md`、`M` `docs/mcp-setup-notes.md`
  - `M` `.env.example`、`M` `CLAUDE.md`
  - `?? docs/superpowers/specs/2026-06-22-claude-code-loop-architecture-visual*`

- **Commit C5（`feat(mcp): /api/mcp/status + /api/mcp/health 端点`）**：
  - 新增 `server/app/modules/mcp_catalog/connect_router.py`
  - 新增 `server/tests/test_mcp_connect.py`
  - `M` `server/app/main.py`

- **Commit C6（`feat(web): MCP 接入 tab`）**：
  - 新增 `web/src/features/mcp/McpConnectWorkspace.tsx`
  - 新增 `web/src/api/mcp.ts`
  - `M` `web/src/App.tsx`、`M` `web/src/types.ts`、`M` `web/src/components/MobileMorePage.tsx`（如适用）

- **Commit C7（`docs(mcp): 公网部署 + 多机接入指引`）**：
  - `M` `docs/mcp-setup-notes.md`（追加「公网部署」节）
  - 新增 `requirements-mcp.txt`（MCP server 子集依赖）
  - 可选：`?? scripts/setup-mcp-client.{ps1,sh}`

### 6.3 PR 描述模板

标题：`feat(mcp): MCP loop POC + 接入指引 tab`

正文：
```markdown
## Summary
- MCP server (FastMCP stdio) + 17 atomic tools 三组（catalog/action/meta）
- 三条 Loop 配方（生文 / 分发 / 评估周报）零配置版
- 前端「MCP 接入」tab：状态指示 / 配置模板 / 一键复制 / 测试连接
- 公网部署 + 多机接入指引（docs/mcp-setup-notes.md）
- alembic 0047 → 0048 同号冲突规避

## Test plan
- [ ] `alembic upgrade head` 线性升到 0048
- [ ] `ruff check / ruff format --check / mypy server/app` 全绿
- [ ] `pytest server/tests/test_mcp_*.py server/tests/test_save_article_mcp.py server/tests/test_mcp_connect.py` 全绿
- [ ] `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build` 全绿
- [ ] 本机 GEO 后端起 → 进「MCP 接入」tab → 服务端 token 配置后状态显示 ✓ → 正确 token 测试连接成功、错误 token 失败
- [ ] Claude Code 按 tab 给的 JSON 配 `~/.claude.json` → `/mcp` 看到 `geo: connected` + 17 个工具
- [ ] generation-loop 在 Claude Code 里能拉问题 → save_article 落库 → submit_review_decision 评分

## Risks
- alembic 0048 仅在 rebase 后生效；merge 前若有人再在 main 上加迁移会再次冲突
- POC 共享 token：单机泄露影响全员，下一步引入 per-user token 单独 PR
- MCP server 当前 stdio-only，每台外部机器仍需克隆仓库；HTTP transport 待后续 PR
```

## 7. 验收清单（实施阶段开始前对齐）

- [ ] 0048 改号、`alembic history` 线性
- [ ] `/api/mcp/status` user JWT 鉴权、`/api/mcp/health` MCP token 鉴权，两路 endpoint 各自的单测覆盖
- [ ] 前端「MCP 接入」tab 五段齐全、复制按钮可用、测试连接三态显示
- [ ] `docs/mcp-setup-notes.md` 公网部署节落地、`requirements-mcp.txt` 子集依赖
- [ ] PR 描述按模板填写、Test plan 全勾、CI 全绿
- [ ] `git rebase origin/main` 干净通过

## 8. 留作后续 spec / PR

- MCP server `streamable-http` transport（消除"每台机器装 Python"门槛）
- per-user MCP token（每用户独立 token，落审计 + 撤销能力）
- MCP wheel 打包发布到 PyPI / 内部 index
- 一键脚本 `setup-mcp-client.{ps1,sh}` 若本 PR 没塞下
