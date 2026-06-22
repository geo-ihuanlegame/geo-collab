# MCP 接入 tab + feat/geo-mcp-loop PR 收尾 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `feat/geo-mcp-loop` 分支从「未提交 WIP + 同号 alembic 冲突」拉到「带 MCP 接入 tab 的可合并 PR」。

**Architecture:** 新增前端单页 tab (`McpConnectWorkspace`) 五段（概览/状态/配置模板/测试连接/故障排查）+ 后端两个端点 (`/api/mcp/status` user JWT、`/api/mcp/health` MCP token) + alembic 0047→0048 重命名 + 公网部署文档 + WIP 按语义拆 commit。MCP 仍走 stdio transport，HTTP transport 留作后续 PR。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic / React 19 + Vite + TypeScript + Tiptap / FastMCP (stdio) / pytest / pnpm。

**Spec：** `docs/superpowers/specs/2026-06-22-mcp-connect-tab-design.md`

**环境约束**（重要，来自 user 记忆）：
- 宿主无 Python / conda——所有 `alembic` / `pytest` / `ruff` / `mypy` 命令通过 docker 容器跑：`docker compose -f docker-compose.dev.yml exec -T app <cmd>`
- `pnpm` 走 npm 全局可用
- PowerShell 含中文的 `.ps1` 文件须 UTF-8 BOM
- `parent reference projects` (content-library-public / pc-admin) 仅供参照，**不可改**

**Branch state at plan start：**
- `feat/geo-mcp-loop` 已 commit `eaff22b`（spec 文档）
- 工作区 24 个未提交 WIP（15M + 9??），spec § 6.2 列了拆 commit 方案
- `origin/main` 在 `8ffe4eb`，feat 落后 32、领先 9（包括刚 commit 的 spec）

---

## File Structure

**新建（本 plan 创建）：**
- `server/app/modules/mcp_catalog/connect_router.py` — `/api/mcp/status` 与 `/api/mcp/health` 两个端点的 sub-router
- `server/tests/test_mcp_connect.py` — 上述端点的单测
- `web/src/features/mcp/McpConnectWorkspace.tsx` — 前端 MCP 接入 tab 主组件
- `web/src/api/mcp.ts` — `getMcpStatus` / `pingMcpHealth` 两个 API client
- `requirements-mcp.txt` — MCP server 子集依赖（外部机器 pip install 用）
- `docs/superpowers/plans/2026-06-22-mcp-connect-tab-and-pr-ready.md` — 即本文件

**修改：**
- `server/app/main.py` — 注册新的 connect_router
- `web/src/App.tsx` — lazy import + ScrollPanel
- `web/src/types.ts` — NavKey 加 `"mcp"` + navItems 末尾追加
- `web/src/components/MobileMorePage.tsx` — 移动端「更多」页加项
- `docs/mcp-setup-notes.md` — 追加「公网部署 + 多机接入」节
- `server/alembic/versions/0047_auto_review_decisions.py` — 重命名 + revision/down_revision 改号

**已存在但需拆分提交（WIP 收尾，不再产生新内容）：**
- spec § 6.2 列的 C1-C4、C7 各组文件

---

## Task 0: 环境核对 + 工作树净度确认

**Files:** 无新文件，仅运行检查命令

- [ ] **Step 0.1：确认当前分支 + 工作树状态**

Run:
```bash
git branch --show-current
git status --short
git log --oneline -3
```

Expected:
- 分支输出 `feat/geo-mcp-loop`
- `git status` 仍能看到 spec § 6.2 列的 24 个 WIP 文件（15M + 9??）
- 最新 commit 是 `eaff22b docs(specs): MCP 接入 tab ...`

如果分支不对：`git checkout feat/geo-mcp-loop`。
如果工作树意外干净：说明 WIP 已被人 commit/stash，跳到 Task 4（alembic 0047 改号）。

- [ ] **Step 0.2：确认 dev 容器在线（用于跑 python 命令）**

Run:
```bash
docker compose -f docker-compose.dev.yml ps app
```

Expected: `app` 服务 status `running` 或 `Up`。

如果未起：`docker compose -f docker-compose.dev.yml up -d app`，等到 `Up` 再继续。

- [ ] **Step 0.3：拉一次 main 备用**

Run:
```bash
git fetch origin --no-tags
git rev-list --left-right --count origin/main...HEAD
```

Expected: 输出 `32 9`（落后 32 / 领先 9，含 spec commit）。如果不是 32，说明 origin/main 又有新动，更新 spec 中 alembic 检查的预期。

---

## Task 1: 提交 WIP 组 C1 (save_article 工具替代 compose_article)

**Files:**
- Delete: `server/app/modules/ai_generation/compose_once.py`
- Delete: `server/tests/test_compose_once.py`
- Modify: `server/app/modules/articles/router.py` (新增 `/save-from-mcp` 端点)
- Modify: `server/mcp/tools/action.py` (删 `compose_article` / 加 `save_article`)
- Modify: `server/app/modules/ai_generation/router.py` (移除 compose-once 挂载)
- Modify: `server/app/core/config.py`
- New: `server/tests/test_save_article_mcp.py`

- [ ] **Step 1.1：核查每个文件 diff 内容确实是 save_article 工作**

Run:
```bash
git diff -- server/app/modules/articles/router.py | head -80
git diff -- server/mcp/tools/action.py | head -80
git diff -- server/app/modules/ai_generation/router.py | head -40
cat server/tests/test_save_article_mcp.py | head -40
```

Expected: `articles/router.py` 多了一个 `save_from_mcp` 函数 + `POST /save-from-mcp` 路由；`action.py` 删除了 `compose_article` tool 装饰器、新增了 `save_article` tool；`ai_generation/router.py` 删了 compose_once import / `app.include_router(compose_once_router, ...)` 行；`test_save_article_mcp.py` 是新增的测试。

如果 diff 看起来跟 spec 描述对不上：**停下，告诉 user，不要乱动**。

- [ ] **Step 1.2：先单独跑 save_article 的测试确保通过**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app pytest server/tests/test_save_article_mcp.py -q
```

Expected: 通过（绿）。若失败：修复代码再继续；不要带着红测试 commit。

- [ ] **Step 1.3：stage 这组文件**

Run:
```bash
git add server/app/modules/articles/router.py \
        server/mcp/tools/action.py \
        server/app/modules/ai_generation/router.py \
        server/app/core/config.py \
        server/app/modules/ai_generation/compose_once.py \
        server/tests/test_compose_once.py \
        server/tests/test_save_article_mcp.py
git status --short
```

Expected: 这 7 个文件标 staged（A / M / D），其它 WIP 仍 unstaged。

- [ ] **Step 1.4：commit**

Run:
```bash
git commit -m "$(cat <<'EOF'
feat(mcp): save_article 工具替代 compose_article（零配置 Loop）

- compose_once.py / test_compose_once.py 删除（旧 LiteLLM 生文路径下线）
- articles/router.py 加 POST /save-from-mcp（Claude 主对话写 markdown → 落库）
- mcp/tools/action.py 用 save_article tool 替代 compose_article tool
- ai_generation/router.py 移除 compose-once 路由挂载
- 新增 test_save_article_mcp.py 覆盖 markdown→tiptap 转换 + review_status=pending

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: 1 commit created，`git log -1 --stat` 看到 7 个 file changed。

---

## Task 2: 提交 WIP 组 C2 (mcp_catalog 模块 + mcp_errors helper)

**Files:**
- New: `server/app/modules/mcp_catalog/__init__.py`
- New: `server/app/modules/mcp_catalog/router.py`
- New: `server/app/core/mcp_errors.py`
- New: `server/tests/test_mcp_errors.py`
- New: `server/tests/test_mcp_catalog.py`
- New: `server/mcp/__main__.py`
- New: `server/tests/test_mcp_entry.py`
- Modify: `server/mcp/tools/catalog.py`
- Modify: `server/mcp/server.py`
- Modify: `server/app/main.py` (注册 mcp_catalog_router)
- Modify: `server/app/modules/auto_review/router.py` (异常包装走 mcp_errors)

- [ ] **Step 2.1：核查 mcp_catalog 路由 + mcp_errors 是否在 main.py 已注册**

Run:
```bash
grep -n "mcp_catalog\|mcp_errors" server/app/main.py
cat server/app/core/mcp_errors.py | head -30
```

Expected: `main.py` 已 import + include `mcp_catalog_router`（参见 spec § 4.3）；`mcp_errors.py` 提供 `mcp_exception_response` helper（参见 CLAUDE.md 「Backend / MCP 端点的未捕获异常用 `core/mcp_errors.mcp_exception_response`」节）。

- [ ] **Step 2.2：跑这组涉及的测试**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app pytest \
  server/tests/test_mcp_catalog.py \
  server/tests/test_mcp_errors.py \
  server/tests/test_mcp_entry.py -q
```

Expected: 全绿。

- [ ] **Step 2.3：stage**

Run:
```bash
git add server/app/modules/mcp_catalog/ \
        server/app/core/mcp_errors.py \
        server/tests/test_mcp_errors.py \
        server/tests/test_mcp_catalog.py \
        server/mcp/__main__.py \
        server/tests/test_mcp_entry.py \
        server/mcp/tools/catalog.py \
        server/mcp/server.py \
        server/app/main.py \
        server/app/modules/auto_review/router.py
git status --short
```

Expected: 全部 staged，剩余 unstaged 应为：`.env.example`、`CLAUDE.md`、`claude-loops/generation-loop.md`、`docs/mcp-demo-walkthrough.md`、`docs/mcp-setup-notes.md`、`docs/superpowers/specs/2026-06-22-claude-code-loop-architecture-visual*`。

- [ ] **Step 2.4：commit**

Run:
```bash
git commit -m "$(cat <<'EOF'
feat(mcp): mcp_catalog 模块 + mcp_errors helper + __main__ 入口

- server/app/modules/mcp_catalog/：跨模块只读 list/get 集中到 /api/mcp/*
- server/app/core/mcp_errors.py：MCP 端点异常包装（litellm/httpx → 502，其它 → 500）
- server/mcp/__main__.py：固化 python -m server.mcp 入口（规避 server.py dual-import）
- server/mcp/server.py：tools 注册数兜底断言（≥1 个，否则 RuntimeError）
- server/mcp/tools/catalog.py：catalog tools 移植到统一 helper
- auto_review/router.py：异常包装切到 mcp_errors

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: 1 commit created。

---

## Task 3: 提交 WIP 组 C4 (文档 + .env.example + CLAUDE.md)

**Files:**
- Modify: `.env.example`
- Modify: `CLAUDE.md`
- Modify: `claude-loops/generation-loop.md`
- Modify: `docs/mcp-demo-walkthrough.md`
- Modify: `docs/mcp-setup-notes.md`
- New: `docs/superpowers/specs/2026-06-22-claude-code-loop-architecture-visual-design.md`
- New: `docs/superpowers/specs/2026-06-22-claude-code-loop-architecture-visual.html`

- [ ] **Step 3.1：核查 docs 改动符合 spec § 6.2 C4 范围**

Run:
```bash
git diff -- .env.example CLAUDE.md docs/mcp-setup-notes.md | head -120
```

Expected: `.env.example` 新增 `GEO_MCP_TOKEN` 等 env vars；`CLAUDE.md` 模块说明同步；`mcp-setup-notes.md` 含 Claude Code config 示例；`generation-loop.md` 零配置版（参考之前讨论：Claude 主对话直接写 markdown）。

- [ ] **Step 3.2：stage + commit**

Run:
```bash
git add .env.example \
        CLAUDE.md \
        claude-loops/generation-loop.md \
        docs/mcp-demo-walkthrough.md \
        docs/mcp-setup-notes.md \
        docs/superpowers/specs/2026-06-22-claude-code-loop-architecture-visual-design.md \
        docs/superpowers/specs/2026-06-22-claude-code-loop-architecture-visual.html

git commit -m "$(cat <<'EOF'
docs(mcp): generation-loop 零配置版 + 演示稿/setup notes 同步

- claude-loops/generation-loop.md：写作环节移到 Claude 主对话，去掉 GEO_AI_API_KEY 依赖
- docs/mcp-setup-notes.md：~/.claude.json 配置示例 + 常见问题
- docs/mcp-demo-walkthrough.md：D7 演示稿收尾
- .env.example：GEO_MCP_TOKEN / GEO_API_BASE_URL 等新 env
- CLAUDE.md：MCP server / auto_review / performance 模块文档同步
- docs/superpowers/specs/2026-06-22-claude-code-loop-architecture-visual*：架构可视化稿

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: 工作树 `git status --short` 输出为空。

- [ ] **Step 3.3：sanity check 工作树净度**

Run:
```bash
git status
git log --oneline -4
```

Expected: `working tree clean`；4 个最新 commit 标题分别是 docs(mcp), feat(mcp): mcp_catalog..., feat(mcp): save_article..., docs(specs): MCP 接入...

---

## Task 4: alembic 0047 → 0048 重命名 + revision 改号

**Files:**
- Rename: `server/alembic/versions/0047_auto_review_decisions.py` → `server/alembic/versions/0048_auto_review_decisions.py`
- Modify (rename target): docstring 头部 + `revision` + `down_revision`

- [ ] **Step 4.1：git mv 重命名**

Run:
```bash
git mv server/alembic/versions/0047_auto_review_decisions.py \
       server/alembic/versions/0048_auto_review_decisions.py
git status --short
```

Expected: 看到 `R  server/alembic/versions/0047_auto_review_decisions.py -> server/alembic/versions/0048_auto_review_decisions.py`。

- [ ] **Step 4.2：改文件内的 revision / down_revision / 头部注释**

用 Edit 工具修改 `server/alembic/versions/0048_auto_review_decisions.py`：

旧 docstring 头部第三行：
```
修订 ID: 0047
上一修订: 0046
```
改为：
```
修订 ID: 0048
上一修订: 0047
```

旧 `revision: str = "0047"` 改为 `revision: str = "0048"`
旧 `down_revision: str | None = "0046"` 改为 `down_revision: str | None = "0047"`

- [ ] **Step 4.3：验证文件内容**

Run:
```bash
grep -nE 'revision|修订' server/alembic/versions/0048_auto_review_decisions.py | head -10
```

Expected:
```
"修订 ID: 0048
上一修订: 0047
revision: str = "0048"
down_revision: str | None = "0047"
```

- [ ] **Step 4.4：合并 main 改动到 feat 分支（rebase 或 merge）**

由于 alembic 改号要基于 main 的 0047 才有意义，**先把 origin/main rebase 进来**：

Run:
```bash
git stash list  # 应为空
git fetch origin --no-tags
git rebase origin/main
```

Expected: rebase 干净通过（spec § 1 已用 `merge-tree` 验证文本无冲突）。如果意外停在冲突：
- 看冲突文件，多半是 main 把 main.py 改了
- 解决：选择两边都保留的合并（main.py register 段保留 main 上的新增 + feat 上的 mcp_catalog 注册）
- `git add` + `git rebase --continue`
- 若彻底乱：`git rebase --abort` 退回，告诉 user

- [ ] **Step 4.5：rebase 后再次确认 alembic head 线性**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app alembic history | head -10
docker compose -f docker-compose.dev.yml exec -T app alembic heads
```

Expected: `alembic heads` 只输出一行（指向 0048）；`alembic history` 上方依次是 `0048 → 0047 (account_dedup_sharing) → 0046`。如果 heads 输出多行：alembic 还是看到了多个 head，多半是改号没生效，回查 Step 4.2 文件内容。

- [ ] **Step 4.6：实际跑一次 upgrade head 验证**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app alembic upgrade head
```

Expected: 输出包含 `Running upgrade 0047 -> 0048, auto_review_decisions`。如果数据库已经升过 0047（不同名 0047 的 stale state），手动 `alembic downgrade 0046` 再 upgrade。

- [ ] **Step 4.7：commit**

Run:
```bash
git add server/alembic/versions/
git commit -m "$(cat <<'EOF'
chore(alembic): 0047 → 0048 同号冲突规避

main 已 ship 0047_account_dedup_sharing，本分支原 0047_auto_review_decisions
重命名为 0048 并 down_revision 指向 0047。git mv 保留历史，文件内 revision 字段同步改号。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: 1 commit created，`alembic history` 单 head。

---

## Task 5: 后端 `/api/mcp/status` 端点（TDD）

**Files:**
- Create: `server/app/modules/mcp_catalog/connect_router.py`
- Create: `server/tests/test_mcp_connect.py`

- [ ] **Step 5.1：写 status 端点的失败测试**

Create `server/tests/test_mcp_connect.py`：

```python
"""MCP 接入端点测试：/api/mcp/status (user JWT) + /api/mcp/health (MCP token)。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def test_status_returns_configured_true_when_token_set(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "test-token-abc")
    test_app = build_test_app(monkeypatch)
    try:
        client = TestClient(test_app.app)
        # build_test_app 已经登录了 admin 并把 cookie 装在 client 上
        resp = client.get("/api/mcp/status", cookies=test_app.cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert body["tools_count"] == 17
        assert body["suggested_base_url"].startswith("http")
        assert not body["suggested_base_url"].endswith("/")
    finally:
        test_app.cleanup()


def test_status_returns_configured_false_when_token_empty(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "")
    test_app = build_test_app(monkeypatch)
    try:
        client = TestClient(test_app.app)
        resp = client.get("/api/mcp/status", cookies=test_app.cookies)
        assert resp.status_code == 200
        assert resp.json()["configured"] is False
    finally:
        test_app.cleanup()


def test_status_requires_auth(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "test-token-abc")
    test_app = build_test_app(monkeypatch)
    try:
        client = TestClient(test_app.app)
        resp = client.get("/api/mcp/status")  # no cookies
        assert resp.status_code == 401
    finally:
        test_app.cleanup()
```

- [ ] **Step 5.2：跑测试看红**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app pytest server/tests/test_mcp_connect.py -q
```

Expected: 测试失败，错误是 404（端点不存在）或 import 错。

- [ ] **Step 5.3：创建 connect_router.py 并实现 /status**

Create `server/app/modules/mcp_catalog/connect_router.py`：

```python
"""MCP 接入指引相关端点。

两组路由分开挂：
- mcp_connect_user_router：/api/mcp/status，user JWT 鉴权（在 main.py 通过 prefix 注册）
- mcp_connect_health_router：/api/mcp/health，MCP token 鉴权（router 自带依赖）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from server.app.core.mcp_auth import require_mcp_token
from server.app.core.config import get_settings

# 与 server/mcp/tools/ 下三个文件 (action.py / catalog.py / meta.py) 注册的 @mcp.tool 数量同步。
# 增减 MCP tool 时改这里，前端「MCP 接入」tab 段 ① 用此数字。
MCP_TOOLS_COUNT = 17


class McpStatusResponse(BaseModel):
    configured: bool
    suggested_base_url: str
    tools_count: int


class McpHealthResponse(BaseModel):
    ok: bool


# user JWT 鉴权：dependency 在 main.py include_router 时通过 prefix 链路自然继承
mcp_connect_user_router = APIRouter()


@mcp_connect_user_router.get("/status", response_model=McpStatusResponse)
def get_mcp_status(request: Request) -> McpStatusResponse:
    settings = get_settings()
    return McpStatusResponse(
        configured=bool(settings.mcp_token),
        suggested_base_url=str(request.base_url).rstrip("/"),
        tools_count=MCP_TOOLS_COUNT,
    )


# MCP token 鉴权（router-level dependency）
mcp_connect_health_router = APIRouter(dependencies=[Depends(require_mcp_token)])


@mcp_connect_health_router.get("/health", response_model=McpHealthResponse)
def get_mcp_health() -> McpHealthResponse:
    return McpHealthResponse(ok=True)
```

- [ ] **Step 5.4：在 main.py 注册 user 路由**

Modify `server/app/main.py`：

在 `mcp_catalog_router` 已有 import 段附近加：
```python
from server.app.modules.mcp_catalog.connect_router import (
    mcp_connect_health_router,
    mcp_connect_user_router,
)
```

在 `app.include_router(mcp_catalog_router, prefix="/api/mcp", ...)` 之后加：
```python
    # MCP 接入指引（前端「MCP 接入」tab 用）
    # user JWT 鉴权（与 system_router 走同一组依赖）
    app.include_router(
        mcp_connect_user_router,
        prefix="/api/mcp",
        tags=["mcp-connect"],
        dependencies=[Depends(get_current_user)],
    )
    # MCP token 鉴权（router 自带 dependency）
    app.include_router(
        mcp_connect_health_router,
        prefix="/api/mcp",
        tags=["mcp-connect"],
    )
```

确保 `Depends` 与 `get_current_user` 在文件顶部已 import（main.py 其它地方应已有；如果没有，加上 `from server.app.modules.auth.dependencies import get_current_user`——具体路径用 `grep -n "from .*import get_current_user" server/app/main.py` 找现有 import 行抄）。

- [ ] **Step 5.5：再跑测试看绿**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app pytest server/tests/test_mcp_connect.py -q
```

Expected: 三个 test 全过。如果 `suggested_base_url` 断言失败：TestClient 的默认 base_url 是 `http://testserver`，应该不带 trailing slash；调试 print 一下值。

- [ ] **Step 5.6：测一次手动 curl 确认行为**

Run（admin 登录后才能用，简化起见这步可跳过——但 health endpoint 在 Task 6 一起测）：
```bash
# 跳过，由 Task 7 集成验证覆盖
```

不 commit，待 Task 6 一起合并 commit。

---

## Task 6: 后端 `/api/mcp/health` 端点（TDD）

**Files:**
- Modify: `server/tests/test_mcp_connect.py` (新增 health 端点测试)

- [ ] **Step 6.1：在 test_mcp_connect.py 末尾追加 health 端点测试**

Append to `server/tests/test_mcp_connect.py`：

```python
def test_health_ok_with_correct_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-xyz")
    test_app = build_test_app(monkeypatch)
    try:
        client = TestClient(test_app.app)
        resp = client.get("/api/mcp/health", headers={"X-MCP-Token": "secret-xyz"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
    finally:
        test_app.cleanup()


def test_health_401_with_wrong_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-xyz")
    test_app = build_test_app(monkeypatch)
    try:
        client = TestClient(test_app.app)
        resp = client.get("/api/mcp/health", headers={"X-MCP-Token": "wrong-token"})
        assert resp.status_code == 401
    finally:
        test_app.cleanup()


def test_health_401_with_no_token(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "secret-xyz")
    test_app = build_test_app(monkeypatch)
    try:
        client = TestClient(test_app.app)
        resp = client.get("/api/mcp/health")
        assert resp.status_code == 401
    finally:
        test_app.cleanup()


def test_health_401_when_token_disabled(monkeypatch):
    monkeypatch.setenv("GEO_MCP_TOKEN", "")
    test_app = build_test_app(monkeypatch)
    try:
        client = TestClient(test_app.app)
        # 即使带 token，配置空时也应 401（require_mcp_token 现有行为）
        resp = client.get("/api/mcp/health", headers={"X-MCP-Token": "anything"})
        assert resp.status_code == 401
    finally:
        test_app.cleanup()
```

- [ ] **Step 6.2：跑测试看绿（health 端点 Task 5 已注册）**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app pytest server/tests/test_mcp_connect.py -q
```

Expected: 全 7 个 test 通过。

- [ ] **Step 6.3：lint + format 检查**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app ruff check server/app/modules/mcp_catalog/connect_router.py server/tests/test_mcp_connect.py
docker compose -f docker-compose.dev.yml exec -T app ruff format --check server/app/modules/mcp_catalog/connect_router.py server/tests/test_mcp_connect.py
docker compose -f docker-compose.dev.yml exec -T app mypy server/app/modules/mcp_catalog/connect_router.py
```

Expected: 全绿。若 ruff format 不过：去掉 `--check` 再跑一次让它自动改写。

- [ ] **Step 6.4：commit**

Run:
```bash
git add server/app/modules/mcp_catalog/connect_router.py \
        server/tests/test_mcp_connect.py \
        server/app/main.py

git commit -m "$(cat <<'EOF'
feat(mcp): /api/mcp/status + /api/mcp/health 接入指引端点

- /api/mcp/status (user JWT)：configured / suggested_base_url / tools_count
- /api/mcp/health (MCP token)：用于前端测试 token 配置是否生效
- MCP_TOOLS_COUNT 常量与 server/mcp/tools/ 注册数同步

测试覆盖：configured/未配置/无 cookie；正确/错误/无/服务端未配置 token 四种 health 用例

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: 1 commit created。

---

## Task 7: 前端 — types.ts 加 NavKey + navItems

**Files:**
- Modify: `web/src/types.ts`

- [ ] **Step 7.1：更新 NavKey 类型**

Modify `web/src/types.ts` line 4：

旧：
```ts
export type NavKey = "agents" | "ai" | "content" | "prompts" | "image-library" | "media" | "tasks" | "system" | "hot-lists" | "admin" | "audit-logs" | "ai-models";
```

改为：
```ts
export type NavKey = "agents" | "ai" | "content" | "prompts" | "image-library" | "media" | "tasks" | "system" | "hot-lists" | "mcp" | "admin" | "audit-logs" | "ai-models";
```

- [ ] **Step 7.2：navItems 末尾追加 mcp 项**

Modify `web/src/types.ts`，找到 `navItems` 数组定义（约 line 519-552），最后一个普通项是 `{ key: "hot-lists", ... }`。

在 `{ key: "hot-lists", label: "热榜", icon: Flame },` 后面追加：
```ts
  { key: "mcp", label: "MCP 接入", icon: Plug },
```

并在文件顶部 `lucide-react` 的 import 加上 `Plug`：

旧 import line 1：
```ts
import { Bot, FileText, Flame, Images, MessagesSquare, MonitorCog, RadioTower, Send, Sparkles } from "lucide-react";
```

改为：
```ts
import { Bot, FileText, Flame, Images, MessagesSquare, MonitorCog, Plug, RadioTower, Send, Sparkles } from "lucide-react";
```

- [ ] **Step 7.3：typecheck**

Run:
```bash
pnpm --filter @geo/web typecheck
```

Expected: 全绿。如果有 NavKey 相关的 narrow check 报错（switch case），后续 Task 9 会在 App.tsx 加 case，先放着；只要 types.ts 自己 typecheck 过就行。

---

## Task 8: 前端 — `web/src/api/mcp.ts` API client

**Files:**
- Create: `web/src/api/mcp.ts`

- [ ] **Step 8.1：先看一个现有 api client 模板**

Run:
```bash
cat web/src/api/hot-lists.ts | head -40
```

记住它怎么 import `fetchJson` / 怎么 export 函数。

- [ ] **Step 8.2：创建 mcp.ts**

Create `web/src/api/mcp.ts`：

```ts
import { fetchJson } from "./client";

export type McpStatus = {
  configured: boolean;
  suggested_base_url: string;
  tools_count: number;
};

export type McpHealthResult =
  | { ok: true }
  | { ok: false; status: number; message: string };

export async function getMcpStatus(): Promise<McpStatus> {
  return fetchJson<McpStatus>("/api/mcp/status");
}

/**
 * 用用户输入的 token 探一次 `/api/mcp/health`。
 * 不带 user JWT cookie 没关系（require_mcp_token 不读 cookie），但 fetch 默认会带；后端忽略。
 *
 * 返回三态：
 * - 200 → { ok: true }
 * - 401 → { ok: false, status: 401, message: "token 错或服务端未配置" }
 * - 其它 → { ok: false, status: <code>, message: <error> }
 */
export async function pingMcpHealth(token: string): Promise<McpHealthResult> {
  try {
    const resp = await fetch("/api/mcp/health", {
      headers: { "X-MCP-Token": token },
      credentials: "include",
    });
    if (resp.status === 200) {
      return { ok: true };
    }
    if (resp.status === 401) {
      return { ok: false, status: 401, message: "token 错或服务端未配置 GEO_MCP_TOKEN" };
    }
    const text = await resp.text();
    return { ok: false, status: resp.status, message: text || `HTTP ${resp.status}` };
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : "网络错误";
    return { ok: false, status: 0, message: msg };
  }
}
```

- [ ] **Step 8.3：typecheck**

Run:
```bash
pnpm --filter @geo/web typecheck
```

Expected: 绿。若 `fetchJson` 的 import 路径不对，回 Step 8.1 看模板。

---

## Task 9: 前端 — McpConnectWorkspace 主组件（5 段）

**Files:**
- Create: `web/src/features/mcp/McpConnectWorkspace.tsx`

- [ ] **Step 9.1：创建文件 + 五段骨架**

Create `web/src/features/mcp/McpConnectWorkspace.tsx`：

```tsx
import { useEffect, useState } from "react";
import { Check, Copy, Eye, EyeOff, Loader2, X } from "lucide-react";
import { getMcpStatus, pingMcpHealth, type McpStatus, type McpHealthResult } from "../../api/mcp";

export function McpConnectWorkspace() {
  const [status, setStatus] = useState<McpStatus | null>(null);
  const [statusErr, setStatusErr] = useState<string | null>(null);

  const [token, setToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<McpHealthResult | null>(null);

  const [copied, setCopied] = useState(false);

  useEffect(() => {
    getMcpStatus()
      .then(setStatus)
      .catch((e: Error) => setStatusErr(e.message));
  }, []);

  const baseUrl = status?.suggested_base_url ?? "<YOUR_GEO_API_BASE_URL>";
  const isLocalhost =
    !!status && /(^https?:\/\/)(127\.0\.0\.1|localhost)(:\d+)?$/.test(status.suggested_base_url);

  const configJson = `{
  "mcpServers": {
    "geo": {
      "command": "python",
      "args": ["-m", "server.mcp"],
      "env": {
        "GEO_MCP_TOKEN": "<PASTE_YOUR_TOKEN_HERE>",
        "GEO_API_BASE_URL": "${baseUrl}",
        "PYTHONPATH": "<PATH_TO_YOUR_LOCAL_geo-collab_CLONE>"
      }
    }
  }
}`;

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(configJson);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // 浏览器可能拒绝 clipboard（HTTP 非 secure context）
      alert("浏览器拒绝复制；请手动选中代码块复制");
    }
  }

  async function handleTest() {
    if (!token.trim()) return;
    setTesting(true);
    setTestResult(null);
    try {
      const r = await pingMcpHealth(token.trim());
      setTestResult(r);
    } finally {
      setTesting(false);
    }
  }

  return (
    <div style={{ padding: "20px 24px", maxWidth: 920 }}>
      <h1 style={{ marginTop: 0 }}>MCP 接入</h1>

      {/* 段 ① 概览 */}
      <section style={{ marginBottom: 28 }}>
        <h2>概览</h2>
        <p>
          Claude Code 通过 MCP 协议调用 GEO 平台工具，自动跑生文 / 分发 / 评估周报三条 Loop。
          GEO 当前注册了 <strong>{status?.tools_count ?? "—"}</strong> 个 atomic tools。
        </p>
        <p style={{ color: "var(--text-secondary, #888)", fontSize: 13 }}>
          Loop 配方：<code>claude-loops/generation-loop.md</code> / <code>claude-loops/distribute-loop.md</code> / <code>claude-loops/weekly-report-loop.md</code>
        </p>
      </section>

      {/* 段 ② 服务端状态 */}
      <section style={{ marginBottom: 28 }}>
        <h2>服务端状态</h2>
        {statusErr && <p style={{ color: "tomato" }}>状态加载失败：{statusErr}</p>}
        {!status && !statusErr && <p><Loader2 size={14} className="spin" /> 加载中…</p>}
        {status && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div>
              {status.configured ? (
                <span style={{ color: "seagreen" }}>
                  <Check size={14} /> 服务端 token 已配置
                </span>
              ) : (
                <span style={{ color: "tomato" }}>
                  <X size={14} /> 服务端 token 未配置 — 请联系 admin 在 .env 加 GEO_MCP_TOKEN
                </span>
              )}
            </div>
            <div>
              建议的 <code>GEO_API_BASE_URL</code>: <code>{status.suggested_base_url}</code>
            </div>
            {isLocalhost && (
              <div style={{ color: "tomato", fontSize: 13 }}>
                ⚠️ 你看到的是本机地址，外部机器复制配置时请把 <code>GEO_API_BASE_URL</code> 改成公网域名（如 <code>https://geo.example.com</code>）。
              </div>
            )}
          </div>
        )}
      </section>

      {/* 段 ③ 客户端配置 */}
      <section style={{ marginBottom: 28 }}>
        <h2>客户端配置</h2>
        <p>在你的机器上编辑 <code>~/.claude.json</code>，粘贴以下片段：</p>
        <div style={{ position: "relative" }}>
          <pre
            style={{
              background: "var(--code-bg, #1e1e1e)",
              color: "var(--code-fg, #d4d4d4)",
              padding: 12,
              borderRadius: 6,
              overflow: "auto",
              fontSize: 13,
            }}
          >
            {configJson}
          </pre>
          <button
            type="button"
            onClick={handleCopy}
            style={{ position: "absolute", top: 8, right: 8 }}
          >
            {copied ? <><Check size={14} /> 已复制</> : <><Copy size={14} /> 复制 JSON</>}
          </button>
        </div>
        <ul style={{ fontSize: 13, color: "var(--text-secondary, #888)" }}>
          <li><code>GEO_MCP_TOKEN</code>：找 admin 获取</li>
          <li><code>GEO_API_BASE_URL</code>：默认填你浏览器看到的域名；如果 Claude Code 跑在容器里访问宿主，改成 <code>http://host.docker.internal:8000</code></li>
          <li><code>PYTHONPATH</code>：在自己机器上 <code>git clone https://github.com/geo-ihuanlegame/geo-collab.git</code> 后填克隆出来的绝对路径；Windows 注意双反斜杠</li>
        </ul>
      </section>

      {/* 段 ④ 测试连接 */}
      <section style={{ marginBottom: 28 }}>
        <h2>测试连接</h2>
        <p>粘贴你拿到的 token，点测试 — 仅验证 token + 网络可达性；不验证 Claude Code 能否起 MCP 子进程（后者请到 Claude Code <code>/mcp</code> 自查）。</p>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
          <input
            type={showToken ? "text" : "password"}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="<PASTE_YOUR_TOKEN_HERE>"
            style={{ flex: 1, padding: "6px 10px", fontFamily: "monospace" }}
            disabled={testing}
          />
          <button type="button" onClick={() => setShowToken((v) => !v)} title="显示/隐藏 token">
            {showToken ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
          <button type="button" onClick={handleTest} disabled={testing || !token.trim()}>
            {testing ? <><Loader2 size={14} className="spin" /> 测试中…</> : "测试"}
          </button>
        </div>
        {testResult && (
          testResult.ok ? (
            <p style={{ color: "seagreen" }}>
              <Check size={14} /> token 正确，网络可达
            </p>
          ) : (
            <p style={{ color: "tomato" }}>
              <X size={14} /> [{testResult.status || "网络错误"}] {testResult.message}
            </p>
          )
        )}
      </section>

      {/* 段 ⑤ 故障排查 */}
      <section style={{ marginBottom: 28 }}>
        <h2>故障排查</h2>
        <details>
          <summary><code>401 MCP token not configured</code></summary>
          <p>GEO 后端 <code>.env</code> 没读到 <code>GEO_MCP_TOKEN</code>。让 admin 检查 .env 内容并重启 uvicorn（docker 部署：<code>docker compose restart app</code>）。</p>
        </details>
        <details>
          <summary><code>401 invalid MCP token</code></summary>
          <p>两边 token 不一致。对照本页段 ④ 的「测试连接」验证你的 token 是否被服务端接受；不接受就让 admin 给你最新 token。</p>
        </details>
        <details>
          <summary><code>geo · connected · no tools</code>（Claude Code 看到 server 但工具列表空）</summary>
          <p>99% 是 <code>~/.claude.json</code> 的命令配成了 <code>python -m server.mcp.server</code>。正确写法是 <code>python -m server.mcp</code>（即段 ③ 模板里的 args）。改完重启 Claude Code。</p>
        </details>
        <details>
          <summary>Claude Code 完全看不到 <code>geo</code> server</summary>
          <p>JSON 格式坏（用 jq / 在线 lint 验证），或 Claude Code 没重启，或 <code>PYTHONPATH</code> 路径不对（路径要指向 geo-collab 仓库根，能 <code>cd</code> 进去看到 <code>server/</code> 子目录）。</p>
        </details>
      </section>
    </div>
  );
}
```

- [ ] **Step 9.2：typecheck**

Run:
```bash
pnpm --filter @geo/web typecheck
```

Expected: 绿。如果 `fetchJson` import 在 mcp.ts 找不到：开 `web/src/api/client.ts` 看实际 export 名补上。如果 lucide 图标缺：检查 lucide-react 实际 export。

---

## Task 10: 前端 — 接到 App.tsx 顶部 sidebar + ScrollPanel

**Files:**
- Modify: `web/src/App.tsx`

- [ ] **Step 10.1：加 lazy import**

Modify `web/src/App.tsx`，在现有 `lazy(...)` 系列后面（约 line 50 附近）加：

```tsx
const McpConnectWorkspace = lazy(() =>
  import("./features/mcp/McpConnectWorkspace").then((m) => ({ default: m.McpConnectWorkspace })),
);
```

- [ ] **Step 10.2：在 workspace section 加 ScrollPanel**

在 `web/src/App.tsx` 现有 `hot-lists` 的 ScrollPanel block 后面（约 line 333），插入：

```tsx
            {visitedTabs.has("mcp") && (
              <ScrollPanel id="mcp" active={activeNav === "mcp"}>
                <ErrorBoundary title="MCP 接入">
                  <Suspense fallback={<TabFallback />}>
                    <McpConnectWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
```

- [ ] **Step 10.3：typecheck + build**

Run:
```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```

Expected: 全绿。build 输出 bundle 大小应有一个新 chunk（`McpConnectWorkspace` lazy split）。

- [ ] **Step 10.4：移动端「更多」页加项**

Modify `web/src/components/MobileMorePage.tsx`：

先看现有结构：
```bash
cat web/src/components/MobileMorePage.tsx | head -60
```

参考 `hot-lists` 现有项，添加一个 `{ key: "mcp", label: "MCP 接入", icon: Plug }` 入口到「更多」列表里（具体 prop 用法照抄已存在的 `hot-lists` 一项）。

如果 MobileMorePage.tsx 是从 navItems 自动派生的（很多 sidebar shell 这么写）：什么都不用改。先 grep `MobileMorePage.tsx` 是否引用 `navItems`：

```bash
grep -n "navItems\|hot-lists" web/src/components/MobileMorePage.tsx
```

如果引用了 `navItems`：跳过此步（Task 7 已经同时改了移动端）。
如果是 hardcoded 列表：照葫芦画瓢加一项。

- [ ] **Step 10.5：commit**

Run:
```bash
git add web/src/types.ts \
        web/src/api/mcp.ts \
        web/src/features/mcp/McpConnectWorkspace.tsx \
        web/src/App.tsx \
        web/src/components/MobileMorePage.tsx 2>/dev/null

git status --short  # 确认 mcp 相关全 staged

git commit -m "$(cat <<'EOF'
feat(web): 「MCP 接入」tab — 状态指示 + 配置模板 + 测试连接

新增左侧 sidebar 「MCP 接入」入口（全员可见，热榜下方），单页五段：
- 概览（工具数动态从 /api/mcp/status 拿）
- 服务端状态（configured / 建议 base_url；localhost 红字提示外部机器需改公网域名）
- 客户端配置 ~/.claude.json JSON 模板 + 一键复制
- 测试连接（粘贴 token → /api/mcp/health 返回 200/401/网络错三态）
- 故障排查（4 条常见错误折叠面板）

新增 web/src/api/mcp.ts：getMcpStatus / pingMcpHealth 两个 client。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: 1 commit。`git log -1 --stat` 看到 5 个文件改动（types/api/component/App/mobile）。

---

## Task 11: 文档 — 公网部署 + 多机接入指引

**Files:**
- Modify: `docs/mcp-setup-notes.md` (追加节)
- Create: `requirements-mcp.txt`

- [ ] **Step 11.1：追加公网部署节**

打开 `docs/mcp-setup-notes.md`，在文件末尾追加：

```markdown

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
```

- [ ] **Step 11.2：创建 requirements-mcp.txt**

先 grep 现有 requirements.txt 看 MCP 真正需要的最小集：

```bash
grep -nE "^mcp|^httpx|^pydantic|^python-dotenv" requirements.txt
```

Expected: 看到 `mcp`、`httpx`、`pydantic`、`pydantic-settings`（如果有）、`python-dotenv` 等。

Create `requirements-mcp.txt`（用 grep 出来的实际版本号）：

```
# MCP 客户端最小依赖集
# 外部机器跑 `python -m server.mcp` 只需要 stdio 通信 + HTTP 调远端 GEO 后端，
# 不需要 sqlalchemy / playwright / minio / litellm 等服务端重依赖。
# 版本号与 requirements.txt 保持同步——升级时一起改。

mcp==<version>
httpx==<version>
pydantic==<version>
pydantic-settings==<version>
python-dotenv==<version>
```

把 `<version>` 替换成 `requirements.txt` 里的实际版本号。如果某个包没在 requirements.txt 里显式钉版本，写 `>=<最低版本>` 即可。

- [ ] **Step 11.3：commit**

Run:
```bash
git add docs/mcp-setup-notes.md requirements-mcp.txt

git commit -m "$(cat <<'EOF'
docs(mcp): 公网部署 + 多机接入指引 + requirements-mcp.txt 子集

- docs/mcp-setup-notes.md 追加「公网部署 + 多机接入」节
  - 服务端：Nginx/Caddy 反代 + X-Forwarded-Proto 透传 + uvicorn --proxy-headers
  - 客户端：git clone + venv + pip install -r requirements-mcp.txt + ~/.claude.json
  - 安全：token 传递、必须 HTTPS、POC 共享 token 风险

- requirements-mcp.txt：MCP 客户端最小依赖集，避免装 sqlalchemy/playwright 等重依赖

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: 1 commit。

---

## Task 12: 全量本地验证

**Files:** 无修改

- [ ] **Step 12.1：alembic 线性验证**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app alembic heads
docker compose -f docker-compose.dev.yml exec -T app alembic history | head -8
```

Expected: 单 head 指向 0048；history 上方依次 0048 → 0047 → 0046。

- [ ] **Step 12.2：后端 lint + format + 类型**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app ruff check server/
docker compose -f docker-compose.dev.yml exec -T app ruff format --check server/
docker compose -f docker-compose.dev.yml exec -T app mypy server/app
```

Expected: 全绿。若 `ruff format --check` 不过：去掉 `--check` 再跑一次让它自动改写，然后 `git add -u && git commit --amend --no-edit`（仅当 amend 是用户在 plan 内最后一个 commit 时安全；否则单开一个 chore commit）。

- [ ] **Step 12.3：后端单测**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app pytest server/tests/test_mcp_connect.py server/tests/test_mcp_catalog.py server/tests/test_mcp_errors.py server/tests/test_mcp_auth.py server/tests/test_save_article_mcp.py server/tests/test_auto_review.py -q
```

Expected: 全绿（覆盖本 PR 新增 + 直接相关）。

- [ ] **Step 12.4：跑一次全套 mcp 相关 + 与改动直接相关的测试集（更广）**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T app pytest server/tests/ -q -k "mcp or save_article or auto_review or performance or feishu_notify"
```

Expected: 全绿。如果某个旧测试因 main rebase 后失败：开测试看是不是依赖了 main 里改动的接口（accounts/wechat/toutiao），如果是、属于 main 那侧的责任，先 `xfail` 加注释或者跳过——但要在 PR 描述里说明。

- [ ] **Step 12.5：前端 typecheck + build**

Run:
```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```

Expected: 全绿。build 时间约 10-30s。

- [ ] **Step 12.6：实际开浏览器 smoke test**

启动后端 + 前端 dev：
```bash
docker compose -f docker-compose.dev.yml up -d app  # 后端
pnpm --filter @geo/web dev                           # 前端，新开终端
```

浏览器开 `http://127.0.0.1:5173`：
1. 登录 admin
2. 左侧 sidebar 最底部（admin 三项之前）应有「MCP 接入」入口
3. 点进去：
   - 段 ② 显示「✓ 服务端 token 已配置」（前提是 .env 里 GEO_MCP_TOKEN 非空）
   - 段 ③ JSON 块显示 `GEO_API_BASE_URL: "http://127.0.0.1:8000"` + localhost 红字警告
   - 段 ③ 点「复制 JSON」→ 粘贴到记事本验证内容完整
   - 段 ④ 输入正确 token → 「测试」→ 显示绿色 ✓
   - 段 ④ 输入错 token → 显示红色 ✗ 和 401 错
4. 用普通 user 账号（非 admin）登录：tab 应仍可见（spec 决策：全员可见）

如果任何一项不通过：返回对应 Task 修正。

---

## Task 13: rebase 验证 + push + PR

**Files:** 无修改

- [ ] **Step 13.1：再 fetch 一次 main 确保没有新动**

Run:
```bash
git fetch origin --no-tags
git rev-list --left-right --count origin/main...HEAD
```

Expected: 输出 `0 <N>`（落后 0、领先 N 个 commit）。如果落后 > 0：说明 plan 期间 main 又有人 push 了，重跑 `git rebase origin/main` + Task 12 验证。

- [ ] **Step 13.2：检查最终 commit 历史**

Run:
```bash
git log --oneline origin/main..HEAD
```

Expected: 应看到 ~9 个 commit（spec + WIP C1/C2/C4 + alembic 0048 + status/health 端点 + 前端 tab + docs/requirements-mcp）。顺序大致：
```
<sha> docs(mcp): 公网部署 + 多机接入指引 + requirements-mcp.txt 子集
<sha> feat(web): 「MCP 接入」tab — 状态指示 + 配置模板 + 测试连接
<sha> feat(mcp): /api/mcp/status + /api/mcp/health 接入指引端点
<sha> chore(alembic): 0047 → 0048 同号冲突规避
<sha> docs(mcp): generation-loop 零配置版 + 演示稿/setup notes 同步
<sha> feat(mcp): mcp_catalog 模块 + mcp_errors helper + __main__ 入口
<sha> feat(mcp): save_article 工具替代 compose_article（零配置 Loop）
<sha> docs(specs): MCP 接入 tab + 外网多机接入设计稿（D8 PR 收尾）
... (原有的 D1-D7 commit 9 个，已在 main rebase 进来前)
```

- [ ] **Step 13.3：push**

Run:
```bash
git push -u origin feat/geo-mcp-loop
```

如果远端已存在分支且有 rebase 历史变更：先用 `git push --force-with-lease` 而不是 `--force`，避免覆盖他人改动：
```bash
git push --force-with-lease origin feat/geo-mcp-loop
```

`--force-with-lease` 在远端被他人推了新 commit 时会拒绝 push，安全。

- [ ] **Step 13.4：创建 PR**

Run:
```bash
gh pr create --title "feat(mcp): MCP loop POC + 接入指引 tab" --body "$(cat <<'EOF'
## Summary
- MCP server (FastMCP stdio) + 17 atomic tools 三组（catalog/action/meta），三条 Loop 配方（生文 / 分发 / 评估周报）零配置版
- 前端「MCP 接入」tab：状态指示 / 配置模板 / 一键复制 / 测试连接 / 故障排查
- 后端两端点：`/api/mcp/status` (user JWT) + `/api/mcp/health` (MCP token)
- 公网部署 + 多机接入指引（`docs/mcp-setup-notes.md`）+ `requirements-mcp.txt` 子集依赖
- alembic 0047 → 0048 同号冲突规避

设计稿：`docs/superpowers/specs/2026-06-22-mcp-connect-tab-design.md`
实施计划：`docs/superpowers/plans/2026-06-22-mcp-connect-tab-and-pr-ready.md`

## Test plan
- [x] `alembic upgrade head` 线性升到 0048
- [x] `ruff check / ruff format --check / mypy server/app` 全绿
- [x] `pytest server/tests/test_mcp_*.py test_save_article_mcp.py test_auto_review.py` 全绿
- [x] `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build` 全绿
- [x] 浏览器进「MCP 接入」tab：状态显示 ✓ + 正确 token 测试连接成功 + 错误 token 失败
- [ ] Claude Code 按 tab 给的 JSON 配 `~/.claude.json` → `/mcp` 看到 `geo: connected` + 17 个工具
- [ ] generation-loop 在 Claude Code 里能拉问题 → save_article 落库 → submit_review_decision 评分

## Risks
- POC 共享 token：单机泄露影响全员，下一步引入 per-user token 单独 PR
- MCP server 当前 stdio-only，每台外部机器仍需克隆仓库；HTTP transport 待后续 PR
- 公网部署须配 `X-Forwarded-Proto` 透传 + uvicorn `--proxy-headers`，否则 tab 段 ② 显示内网地址

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL 输出。复制 URL 通报 user。

- [ ] **Step 13.5：等待 CI**

Run:
```bash
gh pr checks --watch
```

Expected: 等到所有 check 绿。若失败：开 failing 那个 check 的 log，定位是 main rebase 把哪条规则带来了——回 Task 12 修。

---

## Self-Review Checklist（执行 plan 前自查，执行后不必再过）

**Spec coverage：**
- [x] § 3.1 入口（NavKey + navItems） — Task 7
- [x] § 3.2 五段内容 — Task 9
- [x] § 3.3 API client — Task 8
- [x] § 3.4 移动端 — Task 10.4
- [x] § 4.1 /api/mcp/status — Task 5
- [x] § 4.2 /api/mcp/health — Task 6
- [x] § 4.3 文件归属（connect_router.py） — Task 5.3
- [x] § 4.4 测试 — Task 5.1 + 6.1
- [x] § 5 外网部署文档 + requirements-mcp.txt — Task 11
- [x] § 6.1 alembic 0047 → 0048 — Task 4
- [x] § 6.2 WIP 拆 commit — Tasks 1/2/3
- [x] § 6.3 PR 描述模板 — Task 13.4
- [x] § 7 验收清单 — Task 12 + 13

**Placeholder 扫描：**
- 唯一一处 `<version>` 占位在 Task 11.2 `requirements-mcp.txt`，明确要求 engineer 从 `requirements.txt` grep 出来填——这不是 plan 的偷懒，而是要求引擎师对照源头同步版本号
- 其它步骤均给出完整代码 / 完整命令 / 完整 expected 输出

**类型一致性：**
- `McpStatus` 字段（`configured` / `suggested_base_url` / `tools_count`）三处一致：后端 schema (Task 5.3)、前端类型 (Task 8.2)、测试断言 (Task 5.1)
- `McpHealthResult` discriminated union 在 Task 8.2 定义、Task 9.1 消费
- `NavKey` 加 `"mcp"` 在 Task 7.1 → Task 10.2 消费
- `MCP_TOOLS_COUNT = 17` 在 backend (Task 5.3)、test (Task 5.1)、PR 描述 (Task 13.4) 三处都是 17

---

## 完成判据

- [ ] feat/geo-mcp-loop 分支已 push 到 origin
- [ ] PR 已创建、URL 通报 user
- [ ] CI 全绿（ruff/mypy/pytest/typecheck/build）
- [ ] 浏览器实测「MCP 接入」tab 五段功能正常
- [ ] 工作树 `git status` clean
