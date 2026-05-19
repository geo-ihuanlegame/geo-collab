# AGENTS.md — Geo 协作平台

Always `conda activate geo_xzpt` before any Python command.
Docker 环境使用 `docker-compose exec app` 运行所有 Python 命令。

## Dev commands (PowerShell)

```powershell
# backend (port 8000)
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000

# frontend (port 5173, proxies /api → :8000)
pnpm --filter @geo/web dev

# typecheck
pnpm --filter @geo/web typecheck

# build
pnpm --filter @geo/web build

# tests (SQLite, 不依赖 Docker)
pytest server/tests/ -v
pytest server/tests/test_tasks_api.py -v --tb=short
pytest server/tests/ -m "not mysql" -q          # skip MySQL integration tests

# migrations
alembic upgrade head

# Docker Compose（推荐部署/开发方式）
docker-compose up -d
docker-compose exec app python -m server.scripts.seed_users

# health check
Invoke-RestMethod http://127.0.0.1:8000/api/system/status
```

## Setup prerequisites

```powershell
pip install -r requirements.txt
playwright install chromium
alembic upgrade head
pnpm install
```

## Architecture

- **JWT cookie auth** — `/api/auth/login` 下发 JWT 作为 `access_token` httpOnly cookie。需要 `GEO_JWT_SECRET` 环境变量（测试中设为 `"test-secret"`），过期时间 `GEO_JWT_EXPIRE_HOURS`（默认 8 小时）。
  - 多用户模型：`User` 表有 `role`（admin / operator）和 `is_active`。admin 可创建子账号 (`POST /api/auth/users`)，operator 只能看到自己的任务。
  - 登录引导：前端通过 `/api/bootstrap` 检查是否存在 admin → 无则显示首次设置页面。
  - `GEO_SEED_USERS` 环境变量（JSON 数组格式）在 Docker 启动时通过 `seed_users.py` 预建用户。
  - `require_local_token()` 在 `security.py` 中但**未被任何路由使用**，是遗留死代码。
- **Entry point**: Docker CMD 执行 `alembic upgrade head && uvicorn server.app.main:app --host 0.0.0.0 --port 8000`，开发时用 `uvicorn server.app.main:app --reload`。
- **Backend**: FastAPI, SQLAlchemy, Alembic. 9 route modules under `/api/`（auth、accounts、articles、article-groups、assets、chunked-assets、publish-records、system、tasks）。任务状态变更通过 `GET /api/tasks/{id}/stream`（SSE）推送。
- **Database**: 开发/测试用 **SQLite** (`check_same_thread=False`, WAL mode, busy_timeout=5000, foreign_keys=ON)，Docker 用 **MySQL** (`mysql+pymysql`)。`alembic.ini` 的 `sqlalchemy.url` 是占位符，运行时由 `get_database_url()` 覆盖。
- **Frontend**: React 19 + Vite + TypeScript (`web/`), feature-split (`features/content/`, `features/accounts/`, `features/tasks/`, `features/system/`), Tiptap rich-text editor, Lucide icons.
- **Modules** (under `server/app/modules/`):
  - `tasks/` — 任务引擎 + 驱动注册表 + Playwright 发布管线（`task_Executor.py`, `task_Crud.py`, `publish_Runner.py`, `drivers/toutiao.py` 等）
  - `accounts/` — 账号 CRUD、登录 session 状态机、Xvfb + x11vnc + websockify → noVNC 远程浏览器（`account_Auth.py`, `account_Crud.py`, `browser_Session.py`）
  - `articles/` — 文章/分组 CRUD、Tiptap JSON 解析（`article_Crud.py`, `tiptap_Parser.py`, `asset_Store.py`）
- **Shared** (under `server/app/shared/`): `errors.py`（异常类）、`feishu.py`（飞书 Webhook）、`diagnostics.py`（发布诊断）、`system_status.py`
- **Config**: pydantic-settings with `GEO_` prefix, `get_settings()` is `@lru_cache`'d — call `.cache_clear()` after env changes.
- **Data dir**: `GEO_DATA_DIR`（Docker 内默认 `/app/data`）。Subdirs: `assets/`, `browser_states/<platform_code>/<account_key>/`, `logs/`, `exports/`.
- **Task execution — two modes**:
  - **Test/dev**: `POST /api/tasks/{id}/execute` starts a background thread via `bg_session_factory` (monkeypatched to `TestingSessionLocal` in tests). Returns 202 immediately.
  - **Production**: `worker` Docker service (`server/worker/executor.py`) polls the DB, claims tasks via optimistic locking on `worker_id`/`worker_lease_until`, calls `execute_task()` synchronously. API only releases stale claims and returns 202.
  - Internal: `threading.Lock` per task_id, up to `MAX_CONCURRENT_RECORDS=5` records via `Semaphore`, per-account locks for serialization. Records have `lease_until` for crash recovery.
- **Startup order** (`create_app()`): `ensure_data_dirs` → import driver modules (registers drivers) → `recover_stuck_records` → uvicorn serve.
- **`TaskCreate.platform_code`** 默认值是 `"toutiao"`——前端不传时后端自动填入。
- **Docker Compose services**: mysql (8.0), app (FastAPI + static files), worker (publish executor + account login processor), nginx (80 → app, noVNC proxy).
- **Exception hierarchy** (`server/app/shared/errors.py`): `ClientError(Exception)` → 400, `ConflictError(ClientError)` → 409, `AccountError(ClientError)` → 400, `ValidationError(ClientError)` → 400. Service code should raise these, NOT raw `ValueError`. `ValueError` in low-level code has NO global handler → 500.

## Playwright automation details

- **Selectors**: 头条使用 ByteDance 自有设计系统 (`byte-btn`, `byte-btn-primary`, `syl-toolbar-tool`)，**不是** Ant Design。不要猜测选择器——用 `playwright-cli` 检查真实页面 DOM（ByteDance DOM 经常变动）。
- **Cover upload**: 点击 `.add-icon` → 对话框 → "本地上传" → `expect_file_chooser()` + `set_files()` → 等待 "已上传 1 张图片" 文本（最多 60s）→ 确定。
- **Body image upload**: 点击工具栏图片按钮 → 打开抽屉 → 选择文件 → 确认 → 等待 `<img>` 插入 contenteditable 区域。
- **Two-step publish**: 点击 "预览并发布" → 等待 → 点击 "确认发布"（不是同一个按钮）。`stop_before_publish=True` 在预览后停止。
- **Post-publish popups**: "作品同步授权" 对话框和 "加入创作者计划" 弹窗需要关闭。
- **AI drawer**: 操作前先关闭 AI 助手抽屉 (`.close-btn`)。
- **Browser context**: 发布使用 `managed_remote_browser_session`（Xvfb + noVNC），账号浏览器状态存储在 `browser_states/<platform_code>/<account_key>/` 下。
- Cover image is **mandatory**: `ToutiaoDriver._handle_cover()` raises if `article.cover_asset is None`。

## PlatformDriver — 扩展新发布平台

实现 `server/app/modules/tasks/drivers/__init__.py` 的 `PlatformDriver` Protocol：

```python
class MyDriver:
    code = "myplatform"
    name = "我的平台"
    home_url = "https://..."
    publish_url = "https://..."

    def detect_logged_in(self, *, url, title, body) -> bool: ...

    def publish(
        self, *, page, context, payload: PublishPayload, stop_before_publish: bool
    ) -> PublishResult: ...
```

文件底部注册：
```python
from server.app.modules.tasks.drivers import register
register(MyDriver())
```

然后在 `server/app/main.py:create_app()` 顶部 import 触发注册：
```python
import server.app.modules.tasks.drivers.myplatform  # noqa: F401
```

**数据类**（`driver_Base.py`）:
- `PublishPayload(title, cover_asset_path, body_segments, account_key, state_path, display_name, platform_code)` — 预解析的文章数据，driver **不应访问 ORM**。
- `PublishResult(url, title, message)` — 发布结果。
- `PublishError(message, screenshot)` / `UserInputRequired(...)` — 驱动异常，框架统一处理。

**扩展已有驱动**：修改选择器前用 `playwright-cli` 检查实时页面 DOM，不要猜测类名（ByteDance DOM 经常变动）。

## Testing quirks

- `build_test_app(monkeypatch)` in `server/tests/utils.py` creates temp data dir + SQLite DB + FTS5 tables + admin user + JWT cookie. Every test **must** call `test_app.cleanup()` in `finally` (deletes temp dir, clears settings cache).
- FTS5 tables created manually (not via Alembic) — any test using full-text search needs those triggers.
- Tests that execute tasks **must** pass `"stop_before_publish": False` or the task stays in `waiting_manual_publish`.
- Mock the publish runner: `monkeypatch.setattr("server.app.modules.tasks.task_Executor.build_publish_runner_for_record", lambda r: stub_runner)`.
- Background task execution uses `bg_session_factory` — patched in `build_test_app` to `TestingSessionLocal` for cross-thread DB access.
- `build_test_app` also calls `browser_Session._reset_globals()` to reset browser sessions (prevents cross-test leaks).

## Gotchas

- `ensure_data_dirs()` runs at **module import** of `server/app/db/session.py`.
- Alembic `alembic.ini`: `sqlalchemy.url` is a placeholder — runtime override via `get_database_url()`.
- `ToutiaoDriver.publish(...)` — `stop_before_publish` stops after "预览并发布", user must call `POST /api/publish-records/{id}/manual-confirm`.
- Retry only on original records (not retry records).
- `build_publish_runner_for_record(record)` in `task_Executor.py` routes by `platform_code` extracted from `account.state_path` — multi-platform ready via driver registry.
- `bg_session_factory` (module-level var in `server/app/api/routes/tasks.py`) is imported lazily inside functions in both `tasks.py` and `publish_records.py` to avoid circular imports. Do **NOT** toplevel-import it.
- **Route ordering**: `POST /api/accounts/{account_id:int}/login-session` MUST be registered before `POST /api/accounts/{platform_code}/login-session`. The `:int` converter prevents platform_code routes from swallowing numeric account IDs.
- **`docs/` 目录**：包含 `CHUNKED_UPLOAD.md`（分片上传实现）和 `UPLOAD_OPTIMIZATION.md`。`scripts/deploy_check.py` 可做部署前检查。
