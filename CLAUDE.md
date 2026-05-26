# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Geo 协作平台** — 多平台内容自动化发布平台。核心架构是 FastAPI 后端、React/TypeScript 前端、SQLAlchemy/Alembic、Playwright 浏览器自动化，以及 Xvfb/x11vnc/noVNC 远程人工介入。Docker Compose 是推荐部署方式。

## Dev Commands

Always activate the Python environment before Python commands:

```bash
conda activate geo_xzpt
```

Minimum required env vars for local dev (`.env` file or exported):

```bash
GEO_JWT_SECRET=<any-long-random-string>   # required — server raises RuntimeError without it
GEO_DATA_DIR=/path/to/local/data          # required — stores assets, browser states, logs
GEO_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_dev  # or use GEO_DB_HOST/USER/PASS/NAME
```

Backend development server:

```bash
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000
```

Production worker (polls DB, executes tasks, separate process from the web server):

```bash
python -m server.worker.executor
```

Frontend development server:

```bash
pnpm --filter @geo/web dev
```

Frontend typecheck/build:

```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```

Backend tests:

```bash
pytest server/tests/ -v
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/ -q
pytest server/tests/test_assets_api.py -q
# Run a single test
pytest server/tests/test_articles_api.py::test_function_name -q
```

Database migrations:

```bash
alembic upgrade head
```

Docker Compose:

```bash
docker-compose up -d
docker-compose exec app python -m server.scripts.seed_users
```

## Architecture

### Backend (`server/app/`)

- Entry point: `server/app/main.py:create_app()`.
- API routes: `auth`, `accounts`, `articles`, `article-groups`, `assets`, `chunked-assets`, `publish-records`, `system`, `tasks`.
- Database: MySQL only. Runtime DB URL comes from `get_database_url()`; set `GEO_DATABASE_URL` or `GEO_DB_HOST/GEO_DB_USER/GEO_DB_NAME`.
- Auth: `/api/auth/login` sets `access_token` as an httpOnly JWT cookie. Admin bootstrap is checked through `/api/bootstrap`.
- Config: pydantic-settings with the `GEO_` prefix. `get_settings()` is cached; call `.cache_clear()` after env changes in tests.

### Domain Modules (`server/app/modules/`)

Each module is self-contained: `models.py` + `schemas.py` + `service.py` + `router.py`.

- `system/` — `User`, `Platform`, `WorkerHeartbeat` models; auth and system routers (`auth_router.py`, `system_router.py`).
- `accounts/` — account CRUD (`service.py`), login session state (`auth.py`), browser profile paths, Xvfb/x11vnc/websockify/noVNC session management (`browser.py`); router at `router.py`.
- `articles/` — article CRUD (`service.py`), article groups, Tiptap parsing (`parser.py`), asset storage (`store.py`), chunked upload (`uploader.py`); four routers exported from `router.py`. Article body is stored in three parallel forms: `content_json` (Tiptap doc), `content_html` (rendered), `plain_text` (for publish); always keep all three in sync. `ai_format.py` runs AI-based heading detection + auto image insertion on draft articles, tracking state via `Article.ai_checking` / `Article.ai_format_error`.
- `tasks/` — task CRUD (`service.py`), execution engine (`executor.py`), publish runner (`runner.py`), platform driver registry/drivers (`drivers/`); two routers exported from `router.py`.
- `ai_generation/` — generation session CRUD (`service.py`), LangGraph pipeline (`pipeline.py`), Markdown→Tiptap converter (`converter.py`); router at `router.py`.
- `image_library/` — stock image models, MinIO store (`store.py`), image selector/inserter, generation hook (`hook.py`); router at `router.py`. Requires a running MinIO instance; configure via `GEO_MINIO_ENDPOINT`, `GEO_MINIO_ACCESS_KEY`, `GEO_MINIO_SECRET_KEY`. Images are grouped into `StockCategory` buckets; articles reference categories via `article_stock_categories` many-to-many table.
- `skills/` — Skill model, CRUD (`service.py`), router.
- `prompt_templates/` — PromptTemplate model, CRUD (`service.py`), router.

### Shared (`server/app/shared/`)

- `errors.py` — `ClientError`, `ConflictError`, `AccountError`, `ValidationError`.
- `diagnostics.py` — publish diagnostics.
- `feishu.py` — Feishu webhook helper.
- `system_status.py` — system health/status helpers.

Service code should raise the shared named exceptions instead of raw `ValueError`; raw `ValueError` is not globally converted to a client-safe response.

### Frontend (`web/`)

React 19 + Vite + TypeScript. Feature folders live under `web/src/features/`; API clients live under `web/src/api/`. The frontend proxies `/api` to backend port `8000` during development.

## Asset Upload

- Small uploads use `POST /api/assets`.
- Files larger than `3MB` use `web/src/api/chunked-upload.ts` and `/api/chunked-assets/*`.
- Chunk size is `3MB`; frontend upload concurrency is `4`.
- Upload start accepts JSON body `{ "total_size": <bytes> }`. `file_hash` is deprecated and ignored for old-client compatibility.
- Frontend must not compute SHA256 for chunked uploads. Do not call `crypto.subtle.digest()` for this flow.
- Backend computes SHA256 while merging chunks in `ChunkedUploadManager.merge_chunks()` and stores it on the `Asset` record.
- `complete_chunked_upload` must preserve `HTTPException` statuses such as `415 Unsupported file type`; do not wrap them as generic `500`.

## PlatformDriver

Implement `server/app/modules/tasks/drivers/__init__.py`'s `PlatformDriver` protocol and register the driver at module import time:

```python
from server.app.modules.tasks.drivers import register


class MyDriver:
    code = "myplatform"
    name = "我的平台"
    home_url = "https://..."
    publish_url = "https://..."

    def detect_logged_in(self, *, url, title, body) -> bool: ...

    def publish(
        self, *, page, context, payload: PublishPayload, stop_before_publish: bool
    ) -> PublishResult: ...


register(MyDriver())
```

Then import the module in `server/app/main.py:create_app()` to trigger registration.

Drivers receive a prebuilt `PublishPayload`; they should not import ORM article/account/asset modules directly during browser automation.

## Toutiao Automation Notes

- Toutiao uses ByteDance components such as `byte-btn`, `byte-btn-primary`, and `syl-toolbar-tool`; it is not Ant Design.
- Verify selectors with Playwright against the live DOM before changing automation code.
- Cover image is mandatory: `ToutiaoDriver._handle_cover()` raises when `article.cover_asset is None`.
- Cover upload flow: click `.add-icon`, choose local upload, use `expect_file_chooser()`, set files, wait for "已上传 1 张图片", then confirm.
- Publishing is two-step: click "预览并发布", then "确认发布". `stop_before_publish=True` stops after preview and requires manual confirmation.
- Close post-publish popups such as "作品同步授权" and "加入创作者计划".
- Close the AI assistant drawer before editing body content.

## Task Execution

- `POST /api/tasks/{id}/execute` returns `202`.
- Test/dev can run in a background thread through `bg_session_factory`, which tests monkeypatch to `TestingSessionLocal`.
- Production uses `server/worker/executor.py` to poll, claim, and execute records with optimistic locking and leases.
- Execution uses a per-task lock, per-account serialization, and `MAX_CONCURRENT_RECORDS=5`.
- `bg_session_factory` is imported lazily inside route functions to avoid circular imports; do not toplevel-import it.

## Testing Notes

- `build_test_app(monkeypatch)` requires `GEO_TEST_DATABASE_URL`, rebuilds a disposable MySQL schema, creates temp data dir, admin user, and JWT cookie.
- Tests using `build_test_app` must call `test_app.cleanup()` in `finally`.
- Tests that execute tasks must pass `"stop_before_publish": false`, or records stay in `waiting_manual_publish`.
- Mock publish runners with `monkeypatch.setattr("server.app.modules.tasks.executor.build_publish_runner_for_record", lambda r: stub_runner)`.
- Chunked asset tests live in `server/tests/test_assets_api.py`; the focused command is `pytest server/tests/test_assets_api.py -q -k chunked`.

## AI 生文模块

### 设计概览

AI 生文是独立的新功能模块，计划路径：P1 平台搭配 skill 生文 → P1 标题拆分 → P1.5 飞书采集 → P2 自动配图 → P3 每日问题库生文。

**交互 Demo**：`ai-generation-demo.html`（根目录），直接浏览器打开，覆盖一键生成、技能库、提示词库三个模块。

### 技术栈

- **模型抽象**：LiteLLM（100+ 模型统一接口，换模型只改配置，不改业务代码）
- **流程编排**：LangGraph（多步 Agent 状态管理、fan-out/fan-in 并发、节点重试）
- **任务调度**：复用现有 `server/worker/executor.py`（P3 每日定时生文）

不要直接使用 `anthropic` SDK 或 `openai` SDK；所有模型调用走 LiteLLM。

两套模型配置（均通过 LiteLLM 调用）：
- `GEO_AI_MODEL` / `GEO_AI_API_KEY` — 生文主模型（默认 `claude-3-5-sonnet-20241022`）
- `GEO_AI_FORMAT_MODEL` / `GEO_AI_FORMAT_API_KEY` — 格式调整专用模型（默认 `deepseek/deepseek-v4-flash`，用于标题识别和配图，低成本）

### LangGraph 流程

```
规划 Agent（1次顺序调用）
    ↓ 输出 N 份写作任务规格（已分配主题/陪衬/体例，避免并发共享文件冲突）
fan-out → 写作 Agent × N（并发，max_workers=4）
    ↓ 每个 Agent 调用 save_article tool 直接写库
fan-in → 格式化标题 → 自动配图（P2）→ 完成
```

规划阶段顺序执行，负责读写 skill 的共享状态文件（`article-plan.md`、`companion-pool.md`）；写作阶段并行执行，不读写任何共享文件。

### Skill 结构

Skill 是文件夹形式（如 `geo-article-v2/`），包含：
- `SKILL.md` — frontmatter（`name`、`description`）+ 指令
- `references/` — 知识库文件（产品知识、写作规范等）
- `skeletons/` — 文章骨架模板
- `assets/` — 工作文件（`article-plan.md` 等）

Skill 上传到服务器存储，通过 `/api/skills` 管理；提示词（Prompt）是独立资产，通过 `/api/prompt-templates` 管理，与 Skill 并列组合使用，不存在从属关系。

### 数据库

- 生成的文章直接 `INSERT` 进现有 `articles` 表，零 schema 改动
- `create_article` 有 `client_request_id` 幂等保护，并发重试安全
- 新增独立 `generation_sessions` 表记录批次元数据（`article_ids` JSON 数组），不影响现有数据
- 无"占位文章"模式，不存在并发覆盖同一 slot 的问题

### 格式转换

现有代码有 Tiptap 序列化工具（`articles/parser.py`）但无 Markdown → Tiptap 转换。需新增：

```python
# server/app/modules/ai_generation/converter.py
def markdown_to_tiptap(md: str) -> dict: ...   # 段落 + 标题节点
def markdown_to_html(md: str) -> str: ...       # 用 python-markdown
```

`save_article` LangGraph tool 调用这两个函数后再调 `create_article()`。

## Gotchas

- `ensure_data_dirs()` runs at import time in `server/app/db/session.py`.
- Route ordering matters: `POST /api/accounts/{account_id:int}/login-session` must be registered before `POST /api/accounts/{platform_code}/login-session`.
- `TaskCreate.platform_code` defaults to `"toutiao"`.
- `build_publish_runner_for_record(record)` routes by `platform_code` extracted from account state path.
- Retry is only for original records, not retry records.
