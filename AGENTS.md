# AGENTS.md — Geo 协作平台

This file overlaps heavily with `CLAUDE.md` — when editing one, sync the other or fold them together.

Always `conda activate geo_xzpt` before any Python command.

## Dev commands (PowerShell)

```powershell
# backend (port 8000)
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000

# frontend (port 5173, proxies /api → :8000)
pnpm --filter @geo/web dev

# typecheck (tsc -b)
pnpm --filter @geo/web typecheck

# build (tsc -b && vite build)
pnpm --filter @geo/web build

# whole repo typecheck + build
pnpm typecheck
pnpm build

# tests — MySQL ONLY, requires GEO_TEST_DATABASE_URL
pytest server/tests/ -v
pytest server/tests/test_tasks_api.py -v --tb=short
pytest server/tests/test_assets_api.py -q -k chunked

# single test
pytest server/tests/test_articles_api.py::test_function_name -q

# migrations
alembic upgrade head

# Docker Compose
docker-compose up -d
docker-compose exec app python -m server.scripts.seed_users

# health check
Invoke-RestMethod http://127.0.0.1:8000/api/system/status
```

## Setup

```powershell
pip install -r requirements.txt
playwright install chromium
alembic upgrade head
pnpm install
```

## Architecture

- **JWT cookie auth** — `POST /api/auth/login` sets `access_token` httpOnly cookie. Requires `GEO_JWT_SECRET`. TTL via `GEO_JWT_EXPIRE_HOURS` (default 8h).
  - `User.role` = `admin` | `operator`; admin creates sub-accounts, operator sees own data only.
  - `/api/bootstrap` checks for any admin; if none, frontend shows first-time setup.
  - `GEO_SEED_USERS` (JSON array) seeds users on Docker startup via `seed_users.py`.
  - `require_local_token()` in `security.py` is **dead code** — not used by any route.
- **CORS**: pinned to `http://127.0.0.1:5173` and `http://localhost:5173`. Do not add wildcard origins.
- **Monorepo**: pnpm workspace at root; `server/` is Python (FastAPI), `web/` is React 19 + Vite + TypeScript (`@geo/web`). Root `package.json` only forwards via `--filter @geo/web`.
- **Routes** under `/api/`: `auth`, `users`, `accounts`, `articles`, `article-groups`, `assets`, `chunked-assets`, `publish-records`, `system`, `tasks`, `skills`, `prompt-templates`, `generation`, `image-library`, `stock-images`. Task SSE at `GET /api/tasks/{id}/stream`. **`/api/stock-images/*` has no auth dependency** — serves image files publicly by design. All other routes except `auth`, `users`, and `/api/bootstrap` require JWT cookie.
- **Database**: **MySQL only** (`mysql+pymysql`). `GEO_DATABASE_URL` or `GEO_DB_HOST`/`GEO_DB_USER`/`GEO_DB_NAME` required. `alembic.ini` `sqlalchemy.url` is a placeholder — overridden by `get_database_url()`. FTS via MySQL `FULLTEXT INDEX WITH PARSER ngram`. No SQLite support.
- **Frontend**: React 19, Vite, TypeScript strict, Tiptap rich-text, Lucide icons. Feature-split: `features/content/`, `accounts/`, `tasks/`, `system/`.
- **Article body triple storage**: `content_json` (Tiptap doc), `content_html` (rendered), `plain_text` (for publish) — **always keep all three in sync**.
- **Asset upload**: `<3MB → POST /api/assets`; `>=3MB → chunked upload (`/api/chunked-assets/*`, 3MB chunks, 4 concurrent). **Frontend must NOT compute SHA256** — `upload-start` takes `{ total_size }`; `file_hash` ignored. Backend computes SHA256 at `merge_chunks()` time.
- **Publish = Docker only**: Playwright automation requires `Xvfb`, `x11vnc`, `websockify`, noVNC — absent on Windows. Article CRUD, AI generation, asset upload work locally; publishing only inside the container.
- **Modules** (`server/app/modules/`):
  - `tasks/` — engine + driver registry + Playwright publish pipeline
  - `accounts/` — CRUD, login session state machine, Xvfb + x11vnc + websockify → noVNC remote browser
  - `articles/` — CRUD, Tiptap JSON parsing, chunked upload, AI format helpers (`ai_format.py` tracks state via `Article.ai_checking`/`Article.ai_format_error`)
  - `ai_generation/` — LangGraph pipeline, markdown→Tiptap converter, question-bank submodule (`question_bank.py`, exposed via `GET/POST /api/generation/question-pools/*`)
  - `skills/` / `prompt_templates/` — CRUD for Skill folders and prompt templates
  - `image_library/` — Stock image gallery CRUD. **Requires MinIO** (`GEO_MINIO_ENDPOINT`/`_ACCESS_KEY`/`_SECRET_KEY`). Images grouped into `StockCategory` buckets; articles link via `article_stock_categories` many-to-many.
- **Shared** (`server/app/shared/`): `errors.py` (exception classes), `feishu.py` (webhook), `diagnostics.py`, `system_status.py`
- **Config**: pydantic-settings with `GEO_` prefix. `get_settings()` is `@lru_cache`'d — call `.cache_clear()` after env changes. **AI keys not validated at boot** — missing/invalid keys fail at request time.
- **Data dir**: `GEO_DATA_DIR`. Subdirs: `assets/`, `browser_states/<platform_code>/<account_key>/`, `logs/`, `exports/`.
- **Startup order** (`create_app()`): ensure_data_dirs → import driver modules (registers drivers) → `recover_stuck_records` (resets leases expired during crash) → register exception handlers → mount static files → SPA fallback. Startup recovery failures are logged but non-fatal.
- **SPA serving**: `create_app()` mounts `web/dist/` and serves `/api/` routes + SPA fallback on port 8000. Requires `pnpm --filter @geo/web build` first; during dev use Vite on port 5173 instead.
- **Docker Compose**: mysql:8.0, minio, app (FastAPI + static files), worker (publish executor + account login processor), nginx (80 → app, noVNC proxy). Worker is **single-instance** — do NOT `--scale worker=N`.
- **Exception hierarchy**: `ClientError(Exception)` → 400, `ConflictError(ClientError)` → 409, `AccountError(ClientError)` → 400, `ValidationError(ClientError)` → 400. **Raise these in service code, not raw `ValueError`** — no global handler for uncaught `ValueError` → 500.

## Task execution

- **Two modes**: Test/dev via `POST /api/tasks/{id}/execute` spawns background thread (returns 202). Production via `python -m server.worker.executor` polls DB with optimistic locking on `worker_id`/`worker_lease_until`.
- **Concurrency**: per-task lock → global semaphore (`MAX_CONCURRENT_RECORDS=5`, `GEO_PUBLISH_MAX_CONCURRENT_RECORDS`) → per-account lock.
- **Crash recovery**: `recover_stuck_records` runs at startup. Worker periodically (every ~60 iterations) recovers tasks stuck in `"running"`. `recover_stuck_task_claims` releases expired worker leases.
- **Account lock release**: `_release_account_lock` in `finally` block — never add `return` or `raise` before it.
- **DB session safety**: `run_in_executor` calls must do ALL `db` ops (flush/commit/refresh) inside the executor thread. SQLAlchemy `Session` is not thread-safe.

## AI generation

- **All model calls go through LiteLLM**, never direct SDK. Two model configs:
  - `GEO_AI_MODEL` / `GEO_AI_API_KEY` — main writing model (default `claude-3-5-sonnet-20241022`)
  - `GEO_AI_FORMAT_MODEL` / `GEO_AI_FORMAT_API_KEY` — format adjustment (default `deepseek/deepseek-v4-flash`)
- **Flow**: LangGraph — plan agent (sequential, reads/writes skill shared-state files) → fan-out to N writing agents (concurrent, `max_workers=4`, each calls `save_article` tool) → fan-in → done.
- **Skill = folder** with `SKILL.md` + `references/` + `skeletons/` + `assets/`. Prompts independent via `/api/prompt-templates`.
- **Format**: `converter.py` provides `markdown_to_tiptap()` and `markdown_to_html()`. No placeholder article pattern.
- **No dedicated worker**: runs on background threads from the API server.

## Playwright automation (Toutiao)

- **Selectors**: ByteDance design system (`byte-btn`, `byte-btn-primary`, `syl-toolbar-tool`), **not** Ant Design.
- **Two-step publish**: click "预览并发布" → wait → click "确认发布". `stop_before_publish=True` stops after preview → `POST /api/publish-records/{id}/manual-confirm`.
- **Cover image mandatory** — `ToutiaoDriver._handle_cover()` raises if `article.cover_asset is None`.
- **Cover upload**: click `.add-icon` → "本地上传" → `expect_file_chooser()` + `set_files()` → wait for "已上传 1 张图片" (max 60s) → confirm.
- **Post-publish**: dismiss "作品同步授权" + "加入创作者计划" popups. Close AI drawer (`.close-btn`) before each operation.

## PlatformDriver — adding a new platform

Implement the `PlatformDriver` Protocol in `server/app/modules/tasks/drivers/__init__.py`:

```python
class MyDriver:
    code = "myplatform"
    name = "我的平台"
    home_url = "https://..."
    publish_url = "https://..."
    def detect_logged_in(self, *, url, title, body) -> bool: ...
    def publish(self, *, page, context, payload: PublishPayload, stop_before_publish: bool) -> PublishResult: ...

from server.app.modules.tasks.drivers import register
register(MyDriver())
```

Then import in `server/app/main.py:create_app()`: `import server.app.modules.tasks.drivers.myplatform  # noqa: F401`

**Key rules**: Drivers receive prebuilt `PublishPayload` — **must not** import ORM or access DB. Use `PublishError(message, screenshot)` and `UserInputRequired` for driver exceptions. Do NOT `raise UserInputRequired` for `stop_before_publish` flow (return `PublishResult` normally).

## Testing

- `build_test_app(monkeypatch)` in `server/tests/utils.py` requires `GEO_TEST_DATABASE_URL` (MySQL). Rebuilds disposable schema + temp data dir + admin user + JWT cookie. **Must call `test_app.cleanup()`** (or `try/finally`).
- Tests that execute tasks must pass `"stop_before_publish": false`.
- Mock publish runner: `monkeypatch.setattr("server.app.modules.tasks.executor.build_publish_runner_for_record", lambda r: stub_runner)`.
- `bg_session_factory` patched to `TestingSessionLocal` by `build_test_app`. `build_test_app` calls `browser._reset_globals()` to prevent cross-test leaks.
- Test DB name must contain `"test"` (override via `GEO_ALLOW_NON_TEST_DATABASE_FOR_TESTS=1`). `pytest.skip` inside `get_test_database_url()` works only inside a test function body.
- Conftest uses `@pytest.mark.mysql` markers; auto-skipped when `GEO_TEST_DATABASE_URL` absent.
- Run focused: `pytest server/tests/test_assets_api.py -q -k chunked`

## Gotchas

- `ensure_data_dirs()` runs at **module import** of `server/app/db/session.py`.
- `bg_session_factory` (in `tasks/router.py`) imported **lazily** inside functions to avoid circular imports. Do NOT top-level import it.
- `TaskCreate.platform_code` default is `"toutiao"`.
- **Route ordering**: `POST /api/accounts/{account_id:int}/login-session` MUST be before `POST /api/accounts/{platform_code}/login-session`.
- **`ArticleUpdate` `None` values**: `model_dump(exclude_unset=True)` includes `None`. Service code filters with `and update_data[field] is not None` before `setattr`.
- **`ArticleCreate` has no stock category fields** — only `ArticleUpdate` accepts `stock_category_id`/`stock_category_ids`.
- **Chunked upload errors**: `complete_chunked_upload` must re-raise `HTTPException` so 4xx status isn't wrapped as 500.
- **Current migration head**: 0033 (`0033_skill_content_column.py`). 0031/0032 added the question bank tables; 0033 added the skill content column.
- **`docs/`**: `CHUNKED_UPLOAD.md`, `UPLOAD_OPTIMIZATION.md`. `scripts/deploy_check.py` for pre-deployment checks.
