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
- API routes: `auth`, `users`, `accounts`, `articles`, `article-groups`, `assets`, `chunked-assets`, `publish-records`, `system`, `tasks`, `skills`, `prompt-templates`, `generation`, `image-library`, `stock-images`. All except `auth`, `users`, `stock-images`, and `/api/bootstrap` require a JWT cookie. `/api/stock-images/*` serves image files publicly by design — do not add auth there without checking the image-library frontend.
- Task SSE: `GET /api/tasks/{id}/stream` for live execution updates.
- Database: MySQL only. Runtime DB URL comes from `get_database_url()`; set `GEO_DATABASE_URL` or `GEO_DB_HOST/GEO_DB_USER/GEO_DB_NAME`. `alembic.ini`'s `sqlalchemy.url` is a placeholder — it is overridden at runtime by `get_database_url()`.
- Full-text search uses MySQL `FULLTEXT INDEX WITH PARSER ngram` (no Elasticsearch). Migrations covering FTS are exercised by `test_fts_and_migrations.py`.
- Auth: `/api/auth/login` sets `access_token` as an httpOnly JWT cookie. TTL via `GEO_JWT_EXPIRE_HOURS` (default `8`). Admin bootstrap is checked through `/api/bootstrap`. `User.role` is `admin` | `operator`. `GEO_SEED_USERS` (JSON array) seeds users on Docker startup via `server/scripts/seed_users.py`. `require_local_token()` in `core/security.py` is dead code — do not model new auth flows on it.
- CORS: pinned to `http://127.0.0.1:5173` and `http://localhost:5173`. The Vite dev server must run on port 5173 — other ports will be rejected.
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
- Absolute server-side caps live in `core/config.py`: `MAX_ASSET_BYTES = 20MB` (single image), `MAX_ZIP_BYTES = 50MB` (account-export ZIP).
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

Driver-side exceptions (defined alongside the protocol):

- `PublishError(message, screenshot=None)` — driver failure. Optional screenshot bytes are persisted with the publish record.
- `UserInputRequired` — raised when the driver hits a captcha/login-state issue that needs the human-in-the-loop noVNC flow. **Do not** raise this for the `stop_before_publish=True` case; return a normal `PublishResult` instead. `UserInputRequired` is only for unexpected captcha/login interruptions.

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
- Production uses `server/worker/executor.py` to poll, claim, and execute records with optimistic locking on `worker_id` / `worker_lease_until`. Worker periodically (every ~60 iterations) re-runs `recover_stuck_records` and `recover_stuck_task_claims` to release expired leases.
- The publish worker is **single-instance** — never `docker compose up --scale worker=N`. The optimistic locking covers publish records, but the account-login processor inside the same worker is not safe to multi-instance.
- Concurrency: per-task lock → global semaphore (`MAX_CONCURRENT_RECORDS=5`, override via `GEO_PUBLISH_MAX_CONCURRENT_RECORDS`) → per-account serialization.
- `_release_account_lock` runs in a `finally` block; never insert `return` or `raise` between the lock acquisition and that `finally`, or the account stays locked until restart.
- DB session is **not thread-safe**. Inside `run_in_executor`, do all `db` ops (flush / commit / refresh) inside the executor thread — do not pass an open session across the boundary.
- `bg_session_factory` is imported lazily inside route functions to avoid circular imports; do not toplevel-import it.

## Testing Notes

- `build_test_app(monkeypatch)` requires `GEO_TEST_DATABASE_URL`, rebuilds a disposable MySQL schema, creates temp data dir, admin user, and JWT cookie. It also calls `browser._reset_globals()` to prevent cross-test browser-session leaks.
- The test DB name **must contain `"test"`** (safety check). Override with `GEO_ALLOW_NON_TEST_DATABASE_FOR_TESTS=1` if you really need to.
- `conftest.py` registers a `@pytest.mark.mysql` marker and auto-skips MySQL-marked tests when `GEO_TEST_DATABASE_URL` is unset — so a bare `pytest server/tests/ -v` without the env var only runs the no-DB tests.
- Tests using `build_test_app` must call `test_app.cleanup()` in `finally`.
- Tests that execute tasks must pass `"stop_before_publish": false`, or records stay in `waiting_manual_publish`.
- Mock publish runners with `monkeypatch.setattr("server.app.modules.tasks.executor.build_publish_runner_for_record", lambda r: stub_runner)`.
- Chunked asset tests live in `server/tests/test_assets_api.py`; the focused command is `pytest server/tests/test_assets_api.py -q -k chunked`.

## AI 生文模块

Design rationale, roadmap, and the LangGraph diagram live in `AI_GENERATION.md`. Operational rules for editing this code:

- **All model calls go through LiteLLM.** Do not import `anthropic` or `openai` SDKs directly anywhere in this module.
- Two model configs (both LiteLLM):
  - `GEO_AI_MODEL` / `GEO_AI_API_KEY` — main writing model (default `claude-3-5-sonnet-20241022`).
  - `GEO_AI_FORMAT_MODEL` / `GEO_AI_FORMAT_API_KEY` — format-adjustment / heading detection / image insertion (default `deepseek/deepseek-v4-flash`).
- Generation runs on background threads from the API server — there is no dedicated worker for it. `create_app()` injects `bg_session_factory = SessionLocal` into `ai_generation.router`; the route spawns a `Thread`, and that thread's LangGraph nodes create sessions from this factory. Do not assume the production `server/worker/executor.py` is involved.
- Plan agent runs sequentially and is the only stage allowed to read/write the skill shared-state files (`article-plan.md`, `companion-pool.md`). Writing agents run concurrently (`max_workers=4`) and must not touch those shared files.
- Generated articles go straight into the existing `articles` table via `create_article()`. `client_request_id` provides idempotency for concurrent retries. Batch metadata lives in a separate `generation_sessions` table (with `article_ids` as a JSON array).
- Markdown → Tiptap / HTML conversion lives in `server/app/modules/ai_generation/converter.py` (`markdown_to_tiptap`, `markdown_to_html`); the `save_article` LangGraph tool calls these before `create_article()`.
- Skill = folder containing `SKILL.md` + `references/` + `skeletons/` + `assets/`, managed via `/api/skills`. Prompt templates are independent assets via `/api/prompt-templates` — they compose with skills, they do not belong to them.

## Gotchas

- `ensure_data_dirs()` runs at import time in `server/app/db/session.py`.
- On boot, `create_app()` calls `recover_stuck_records()` to reset records left in `status='running'` by a prior crash. Failures are logged but non-fatal — if you see records stuck in `running`, check startup logs.
- The FastAPI app also serves the SPA: any non-`/api/` path returns `web/dist/index.html`. So accessing the UI through the FastAPI port (8000) requires a prior `pnpm --filter @geo/web build`. During dev, use the Vite dev server (5173) instead.
- Route ordering matters: `POST /api/accounts/{account_id:int}/login-session` must be registered before `POST /api/accounts/{platform_code}/login-session`.
- `TaskCreate.platform_code` defaults to `"toutiao"`.
- `build_publish_runner_for_record(record)` routes by `platform_code` extracted from account state path.
- Retry is only for original records, not retry records.
- `ArticleUpdate` patches drop explicit `null` values. `model_dump(exclude_unset=True)` includes them, but the service layer filters `None` before `setattr`, so `PATCH {"field": null}` does **not** clear the field — use a sentinel or a dedicated clear endpoint if you need that.
- `ArticleCreate` does not accept `stock_category_id` / `stock_category_ids`; only `ArticleUpdate` does. Set the categories with a follow-up `PATCH` if you need them at creation time.
- `complete_chunked_upload` must re-raise `HTTPException` (e.g. `415 Unsupported file type`) instead of wrapping into a generic `500`.
- noVNC default binds to `127.0.0.1` only on the host (Docker compose). Remote operators access it through a VPN or SSH tunnel — do not expose it publicly without explicit auth in front.
- Browser-automation publish flow requires `Xvfb`, `x11vnc`, `websockify`, and noVNC on PATH (or paths set via `GEO_PUBLISH_XVFB_PATH` etc.). These are present in the Docker image but absent on Windows local dev — publishing only works inside the container. Everything else (article CRUD, AI generation, asset upload) works locally.
- AI keys (`GEO_AI_API_KEY`, `GEO_AI_FORMAT_API_KEY`) are not validated at boot — missing/invalid keys fail at request time when the LiteLLM call runs.

See `README.md` "新手推荐阅读顺序" for a guided codebase tour. `AGENTS.md` overlaps heavily with this file and is currently the more concise reference for some gotchas — when editing one, sync the other or fold them together.
