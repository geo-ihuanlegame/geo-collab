# AGENTS.md — Geo 协作平台

Always `conda activate geo_xzpt` before any Python command.
Docker 环境使用 `docker-compose exec app` 运行所有 Python 命令。

## Dev commands (PowerShell)

```powershell
# backend (port 8000)
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000

# frontend (port 5173, proxies /api → :8000)
pnpm --filter @geo/web dev

# typecheck (uses tsc -b from web/package.json)
pnpm --filter @geo/web typecheck

# build (tsc -b && vite build)
pnpm --filter @geo/web build

# whole repo typecheck + build (also in root package.json)
pnpm typecheck
pnpm build

# tests — MySQL ONLY, requires GEO_TEST_DATABASE_URL
pytest server/tests/ -v
pytest server/tests/test_tasks_api.py -v --tb=short
pytest server/tests/test_assets_api.py -q -k chunked

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
- **Entry point**: Docker CMD: `alembic upgrade head && uvicorn server.app.main:app`. Dev: `uvicorn server.app.main:app --reload`.
- **Monorepo**: pnpm workspace at root; packages defined in `pnpm-workspace.yaml`. `server/` is Python (FastAPI), `web/` is React 19 + Vite + TypeScript (`@geo/web`). Root `package.json` only has `pnpm --filter @geo/web` forwarding scripts.
- **Routes**: 9 modules under `/api/` (`auth`, `accounts`, `articles`, `article-groups`, `assets`, `chunked-assets`, `publish-records`, `system`, `tasks`). Task SSE at `GET /api/tasks/{id}/stream`.
- **Database**: **MySQL only** (`mysql+pymysql`). `GEO_DATABASE_URL` or `GEO_DB_HOST`/`GEO_DB_USER`/`GEO_DB_NAME` required at runtime. `alembic.ini` `sqlalchemy.url` is a placeholder — overridden by `get_database_url()`.
  - FTS via MySQL `FULLTEXT INDEX WITH PARSER ngram` (not SQLite FTS5).
  - No SQLite support. No `sqlite:///` fallback. Tests require real MySQL via `GEO_TEST_DATABASE_URL`.
- **Frontend**: React 19, Vite, TypeScript strict, Tiptap rich-text, Lucide icons. Feature-split: `features/content/`, `accounts/`, `tasks/`, `system/`.
- **Asset upload**: `<3MB → `POST /api/assets`; `>=3MB → chunked upload (`/api/chunked-assets/*`, 3MB chunks, 4 concurrent). **Frontend must NOT compute SHA256** — `upload-start` takes `{ total_size }` only; `file_hash` ignored. Backend computes SHA256 at `merge_chunks()` time.
- **Modules** (`server/app/modules/`):
  - `tasks/` — engine + driver registry + Playwright publish pipeline (`task_Executor.py`, `task_Crud.py`, `publish_Runner.py`, `drivers/toutiao.py`)
  - `accounts/` — CRUD, login session state machine, Xvfb + x11vnc + websockify → noVNC remote browser (`account_Auth.py`, `account_Crud.py`, `browser_Session.py`)
  - `articles/` — CRUD, Tiptap JSON parsing (`article_Crud.py`, `tiptap_Parser.py`, `asset_Store.py`)
- **Shared** (`server/app/shared/`): `errors.py` (exception classes), `feishu.py` (webhook), `diagnostics.py`, `system_status.py`
- **Config**: pydantic-settings with `GEO_` prefix. `get_settings()` is `@lru_cache`'d — call `.cache_clear()` after env changes.
- **Data dir**: `GEO_DATA_DIR`. Subdirs: `assets/`, `browser_states/<platform_code>/<account_key>/`, `logs/`, `exports/`.
- **Startup order** (`create_app()`): ensure_data_dirs → import driver modules (registers drivers) → `recover_stuck_records` (resets leases expired during crash) → register exception handlers → uvicorn serve.
- **Docker Compose**: mysql:8.0, app (FastAPI + static files), worker (publish executor + account login processor), nginx (80 → app, noVNC proxy). Worker is **single-instance** — do NOT `--scale worker=N`.
- **Exception hierarchy** (`shared/errors.py`): `ClientError(Exception)` → 400, `ConflictError(ClientError)` → 409, `AccountError(ClientError)` → 400, `ValidationError(ClientError)` → 400. **Raise these in service code, not raw `ValueError`** — there is no global handler for uncaught `ValueError` → 500.

## Task execution — concurrency model

- **Two modes**:
  - **Test/dev**: `POST /api/tasks/{id}/execute` spawns background thread via `bg_session_factory` (monkeypatched to `TestingSessionLocal` in tests). Returns 202.
  - **Production**: `worker` Docker service polls DB, claims tasks via optimistic locking on `worker_id`/`worker_lease_until`, calls `execute_task()` synchronously. API only releases stale claims.
- **Three-level concurrency control**:
  1. **Per-task lock** (`threading.Lock` in `_task_locks` dict) — prevents re-entering `execute_task`
  2. **Global semaphore** (`_global_publish_sem`, `MAX_CONCURRENT_RECORDS=5`, configurable via `GEO_PUBLISH_MAX_CONCURRENT_RECORDS`) — limits concurrent record execution
  3. **Per-account lock** (`threading.Lock` in `_account_locks` dict) — serializes publishes to the same account
- **Crash recovery**: Worker does periodic (every ~60 iterations) recovery of tasks stuck in `"running"` with all records terminal. `recover_stuck_records` runs at startup. `recover_stuck_task_claims` (startup + periodic) releases expired worker leases.
- **Account lock cleanup**: `_release_account_lock` called in `finally` block. If `_finish_record_future` raises, the outer `try/finally` ensures locks are released.
- **DB session safety**: `run_in_executor` calls must do ALL `db` operations (including `flush`/`commit`/`refresh`) inside the executor thread. Do NOT access the same `Session` from both an executor thread and the event loop thread — SQLAlchemy `Session` is not thread-safe.

## Playwright automation

- **Selectors**: ByteDance design system (`byte-btn`, `byte-btn-primary`, `syl-toolbar-tool`), **not** Ant Design. Verify with `playwright-cli` — ByteDance DOM changes frequently.
- **Cover upload**: click `.add-icon` → dialog → "本地上传" → `expect_file_chooser()` + `set_files()` → wait for "已上传 1 张图片" text (max 60s) → confirm.
- **Body image upload**: click toolbar image button → open drawer → select file → confirm → wait for `<img>` insertion into contenteditable.
- **Two-step publish**: click "预览并发布" → wait → click "确认发布" (different button).
- **`stop_before_publish=True`**: driver returns `PublishResult` normally (does NOT raise), framework sets `waiting_manual_publish` status. User calls `POST /api/publish-records/{id}/manual-confirm` to complete.
- **Post-publish popups**: dismiss "作品同步授权" dialog and "加入创作者计划" popup.
- **AI drawer**: close before each operation (`.close-btn`).
- **Cover image is mandatory**: `ToutiaoDriver._handle_cover()` raises if `article.cover_asset is None`.
- **Browser context**: uses `managed_remote_browser_session` (Xvfb + noVNC). Account state stored at `browser_states/<platform_code>/<account_key>/`.

## PlatformDriver — adding a new platform

Implement `server/app/modules/tasks/drivers/__init__.py`'s `PlatformDriver` Protocol:

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

Import in `server/app/main.py:create_app()` to trigger registration:
```python
import server.app.modules.tasks.drivers.myplatform  # noqa: F401
```

**Data classes** (from `driver_Base.py`):
- `PublishPayload(title, cover_asset_path, body_segments, account_key, state_path, display_name, platform_code)` — pre-resolved; drivers **must not** access ORM.
- `PublishResult(url, title, message)` / `PublishPausedForManualConfirm` — publish outcomes.
- `PublishError(message, screenshot)` / `UserInputRequired(...)` — driver exceptions handled by framework.

## Testing quirks

- `build_test_app(monkeypatch)` in `server/tests/utils.py` requires `GEO_TEST_DATABASE_URL`. Rebuilds disposable MySQL schema, creates temp data dir + admin user + JWT cookie. **Every test must call `test_app.cleanup()`** (or use `try/finally`).
- Tests that execute tasks must pass `"stop_before_publish": False` (or the task pauses at preview).
- Mock the publish runner: `monkeypatch.setattr("server.app.modules.tasks.task_Executor.build_publish_runner_for_record", lambda r: stub_runner)`.
- Background task execution uses `bg_session_factory` — patched to `TestingSessionLocal` by `build_test_app`.
- `build_test_app` calls `browser_Session._reset_globals()` to prevent cross-test browser session leaks.
- Test database name must contain `"test"` (safety check in `get_test_database_url()`). Override with `GEO_ALLOW_NON_TEST_DATABASE_FOR_TESTS=1`.
- `pytest.skip` is called inside `get_test_database_url()` when `GEO_TEST_DATABASE_URL` is missing — usable only from within a test function body, not at module scope.

## Gotchas

- `ensure_data_dirs()` runs at **module import** of `server/app/db/session.py`.
- `bg_session_factory` (module-level var in `server/app/api/routes/tasks.py`) is imported **lazily** inside functions to avoid circular imports. Do NOT top-level import it.
- `TaskCreate.platform_code` default is `"toutiao"` — backend fills in when frontend omits it.
- **Route ordering**: `POST /api/accounts/{account_id:int}/login-session` MUST be registered before `POST /api/accounts/{platform_code}/login-session`. The `:int` converter prevents platform_code routes from swallowing numeric account IDs.
- **Database constraints**: `article_groups.name` has a per-user unique constraint `(user_id, name)`, NOT a global unique. The old global unique index was dropped (migration 0021).
- **Unique constraints**: `client_request_id` on `articles` and `publish_tasks` now include `user_id` (migration 0020). Cross-user conflicts no longer cause `IntegrityError` → 500.
- **Chunked upload errors**: `complete_chunked_upload` must re-raise `HTTPException` so 415/4xx status isn't wrapped as 500.
- **`stop_before_publish` flow**: Driver returns `PublishResult` normally. Record status becomes `waiting_manual_publish`. Do NOT `raise UserInputRequired` from driver for this case.
- **Account lock release**: `_release_account_lock` is always called in `finally` block — never add `return` or `raise` between `_finish_record_future` and it, or the account permanently deadlocks.
- **`None` values in `ArticleUpdate`**: `model_dump(exclude_unset=True)` includes `None` values. `article_Crud.py` filters them out with `and update_data[field] is not None` before `setattr` to avoid `IntegrityError` on NOT NULL columns.
- **`docs/` directory**: `CHUNKED_UPLOAD.md` (chunked upload impl), `UPLOAD_OPTIMIZATION.md`. `scripts/deploy_check.py` for pre-deployment checks.
- **Current migration head**: 0021 (`0021_drop_article_groups_name_unique.py`). Last non-migration change: Codex SQLite removal (commit `fe697d3`).
