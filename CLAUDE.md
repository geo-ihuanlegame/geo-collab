# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 同步对象：本仓库 `AGENTS.md` 只是一个指向本文档的指针。所有事实更新都改这一份。

## Project Overview

**Geo 协作平台** — 多平台内容自动化发布平台。后端 FastAPI + SQLAlchemy/Alembic（MySQL only），前端 React 19 + Vite + TypeScript + Tiptap，浏览器自动化 Playwright + Xvfb/x11vnc/websockify/noVNC（远程人工接管），AI 生文走 LiteLLM + LangGraph，生产部署用 Docker Compose。

## Dev Commands

Python 命令前先激活 conda 环境：

```bash
conda activate geo_xzpt
```

本地开发至少需要这三项环境变量（写在 `.env` 或 export）：

```bash
GEO_JWT_SECRET=<long-random-string>   # 必填，未设置时 create_app() 抛 RuntimeError
GEO_DATA_DIR=/path/to/local/data      # 必填，assets / browser_states / logs / exports 全部落在此
GEO_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_dev
# 或者拆开写：GEO_DB_HOST / GEO_DB_PORT / GEO_DB_USER / GEO_DB_PASS / GEO_DB_NAME
```

后端 / 前端 / 测试：

```bash
# 后端
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000

# 生产 worker（轮询 DB、执行发布任务，与 web 进程分离）
python -m server.worker.executor

# 前端（端口必须是 5173 —— CORS 只放行 5173）
pnpm --filter @geo/web dev
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build

# 后端测试（MySQL only，需要 GEO_TEST_DATABASE_URL，DB 名必须含 "test"）
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/ -q
pytest server/tests/test_assets_api.py -q -k chunked
pytest server/tests/test_articles_api.py::test_function_name -q

# 数据库迁移
alembic upgrade head

# Docker Compose
docker-compose up -d
docker-compose exec app python -m server.scripts.seed_users
```

Lint / format / typecheck（开发工具装在 `requirements-dev.txt`：`pip install -r requirements-dev.txt`）：

```bash
# 后端（配置在 pyproject.toml：ruff 选 E/F/I/B/UP，line-length=100，忽略 E501/B008；mypy 宽松）
ruff check server/
ruff format --check server/      # 去掉 --check 直接改写
mypy server/app

# 前端
pnpm --filter @geo/web lint       # eslint src
pnpm --filter @geo/web typecheck  # tsc -b
```

CI（`.github/workflows/ci.yml`，push 到 main 和所有 PR 触发）：**后端 ruff check / ruff format / mypy / pytest、前端 typecheck + build 都是硬门禁**；只剩前端 eslint 仍是 `continue-on-error` 的非阻塞步骤（存量 lint error 清完后再删掉 `continue-on-error` 变硬门禁）。CI 用 `mysql:8.0` service 起临时测试库 `geo_test`。

## Architecture

### Backend (`server/app/`)

- 应用工厂：`server/app/main.py:create_app()`。
- API 路由（全部挂在 `/api/` 下）：`auth`、`users`、`accounts`、`articles`、`article-groups`、`assets`、`chunked-assets`、`publish-records`、`system`、`tasks`、`skills`、`prompt-templates`、`generation`、`image-library`、`stock-images`、`audit-logs`。
- 鉴权：除 `auth`、`users`、`/api/bootstrap`、`/api/stock-images/*` 外，全部走 `Depends(get_current_user)` 的 JWT cookie。
  - `/api/stock-images/*` 是**有意公开**的图片文件服务，前端 image-library 依赖；改动前先确认。
  - `/api/audit-logs` 用 `require_admin`，只允许 admin。
- Task SSE：`GET /api/tasks/{id}/stream` 推送执行进度。
- 数据库：**MySQL only**（`mysql+pymysql`）。`get_database_url()` 优先用 `GEO_DATABASE_URL`，否则拼 `GEO_DB_*`。`alembic.ini` 里的 `sqlalchemy.url` 是占位符，运行时被 `get_database_url()` 覆盖。无 SQLite 兼容。
- 全文检索：MySQL `FULLTEXT INDEX WITH PARSER ngram`（无 Elasticsearch）。覆盖 FTS 的迁移由 `test_fts_and_migrations.py` 验证。
- Auth：`POST /api/auth/login` 写 httpOnly cookie `access_token`。TTL 由 `GEO_JWT_EXPIRE_HOURS` 控制（默认 8）。Admin 引导走 `/api/bootstrap`。`User.role` ∈ `admin` | `operator`。`GEO_SEED_USERS`（JSON 数组）由 `server/scripts/seed_users.py` 在 Docker 启动时种入。`core/security.py` 里的 `require_local_token()` 是**死代码**，新接口不要照抄这条路径。
- CORS：写死 `http://127.0.0.1:5173` 和 `http://localhost:5173`，`allow_credentials=False`。Vite 必须跑 5173 端口，其它端口会被拒。
- 限流：`slowapi` 已挂到 `app.state.limiter`，新端点要限流时用 `@limiter.limit(...)` 装饰。
- 配置：`pydantic-settings`，前缀 `GEO_`。`get_settings()` 走 `@lru_cache`，测试改环境后要 `get_settings.cache_clear()`。
- 全局异常 → JSON：`ConflictError → 409`、`ValidationError / AccountError / ClientError → 400`、其它未捕获 → 500。**service 层抛这些命名异常，不要抛裸 `ValueError`**——没有针对 `ValueError` 的全局兜底。

### Domain Modules (`server/app/modules/`)

每个模块自包含：`models.py` + `schemas.py` + `service.py` + `router.py`。

- `system/` — `User`、`Platform`、`WorkerHeartbeat`；`auth_router.py` 和 `system_router.py`。
- `accounts/` — 账号 CRUD（`service.py`）、登录会话状态机（`auth.py`）、浏览器 profile 路径、Xvfb/x11vnc/websockify/noVNC 会话管理（`browser.py`）；路由在 `router.py`。
- `articles/` — 文章 CRUD（`service.py`）、文章分组、Tiptap 解析（`parser.py`）、附件存储（`store.py`）、分块上传（`uploader.py`）；从 `router.py` 导出四个路由。文章正文存三份并行结构（`content_json` Tiptap 文档、`content_html` 渲染 HTML、`plain_text` 发布纯文本），**改任何一份都要同步另外两份**。`ai_format.py` 跑标题识别 / 自动插图，通过 `Article.ai_checking` / `Article.ai_format_error` 暴露状态。
- `tasks/` — 任务 CRUD（`service.py`）、执行引擎（`executor.py`）、发布运行器（`runner.py`）、驱动注册表与具体驱动（`drivers/`）；从 `router.py` 导出 `tasks_router` 和 `publish_records_router`。
- `ai_generation/` — 生成 session CRUD（`service.py`）、LangGraph 流水线（`pipeline.py`）、Markdown→Tiptap 转换（`converter.py`）、问题库（`question_bank.py`，对应 `/api/generation/question-pools/*`，支持从飞书多维表同步）；路由在 `router.py`。
- `image_library/` — 图片库模型、MinIO 存储（`store.py`）、选图 / 插图、生文钩子（`hook.py`）；路由在 `router.py`。**需要 MinIO**：`GEO_MINIO_ENDPOINT` / `GEO_MINIO_ACCESS_KEY` / `GEO_MINIO_SECRET_KEY`（HTTPS 加 `GEO_MINIO_SECURE=true`）。图片按 `StockCategory` 分桶，文章通过 `article_stock_categories` 多对多关联类别。
- `skills/` — Skill 模型、CRUD、路由。
- `prompt_templates/` — PromptTemplate 模型、CRUD、路由。
- `audit/` — 审计日志：`AuditLog` 模型 + `service.list_audit_logs()` 游标分页；路由 `/api/audit-logs`，**仅 admin**，参数 `user_id` / `action_prefix` / `target_type` / `target_id` / `start_at` / `end_at` / `cursor` / `limit≤500`。

### Shared (`server/app/shared/`)

- `errors.py` — `ClientError`、`ConflictError`、`AccountError`、`ValidationError`。
- `diagnostics.py` — 发布诊断。
- `feishu.py` — 飞书 webhook 推送（`GEO_FEISHU_WEBHOOK_URL`）。**注意**：问题库从多维表同步走的是 `GEO_FEISHU_APP_ID` / `GEO_FEISHU_APP_SECRET`（用来换 `tenant_access_token`），和 webhook 是两条不同的凭据。
- `system_status.py` — 系统健康检查。

### Frontend (`web/`)

React 19 + Vite + TypeScript（strict）+ Tiptap + Lucide。Feature 拆分在 `web/src/features/`：`content/`、`accounts/`、`tasks/`、`system/`、`ai-generation/`、`image-library/`、`prompt-templates/`、`auth/`。API 客户端在 `web/src/api/`（按后端路由对应）。开发时 Vite 把 `/api` 代理到 `127.0.0.1:8000`。

## Asset Upload

- 小文件走 `POST /api/assets`。
- 文件大于 `3MB` 走 `web/src/api/chunked-upload.ts` + `/api/chunked-assets/*`。
- chunk size = `3MB`，前端并发 = `4`。
- 服务端硬上限在 `core/config.py`：`MAX_ASSET_BYTES = 20MB`（单图）、`MAX_ZIP_BYTES = 50MB`（账号导出 ZIP）。
- `upload-start` 请求体 `{ "total_size": <bytes> }`。`file_hash` 已废弃，仅为兼容旧前端忽略不校验。
- **前端不要算 SHA256**（不要 `crypto.subtle.digest()`）。SHA256 由 `ChunkedUploadManager.merge_chunks()` 在合并时计算并写到 `Asset` 记录。
- `complete_chunked_upload` 必须 re-raise `HTTPException`（例如 `415 Unsupported file type`），不要包成通用 `500`。

## PlatformDriver

按 `server/app/modules/tasks/drivers/__init__.py` 里的 `PlatformDriver` Protocol 实现，并在模块 import 时 `register(...)`：

```python
from server.app.modules.tasks.drivers import register
from server.app.modules.tasks.drivers.base import PublishPayload, PublishResult


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

然后在 `server/app/main.py:create_app()` 顶部加一行 `import server.app.modules.tasks.drivers.myplatform  # noqa: F401` 触发注册。

驱动**拿到的是已构建好的 `PublishPayload`**，不要在浏览器自动化里直接 import 文章 / 账号 / 资源的 ORM 模块。

驱动异常（与 Protocol 同一目录定义）：

- `PublishError(message, screenshot=None)` — 驱动级失败。可选 screenshot bytes 会随发布记录持久化。
- `UserInputRequired` — 遇到验证码 / 登录态失效，需要 noVNC 人工接管。**`stop_before_publish=True` 的正常停顿不要抛这个**，正常返回 `PublishResult` 即可；`UserInputRequired` 只用于非预期的人工干预。

## Toutiao Automation Notes

- 头条用字节自家的设计系统：`byte-btn`、`byte-btn-primary`、`syl-toolbar-tool`，**不是 Ant Design**。
- 改自动化代码前先用 Playwright 对着实时 DOM 校验选择器。
- 封面图必填：`ToutiaoDriver._handle_cover()` 在 `article.cover_asset is None` 时抛错。
- 封面上传链路：点 `.add-icon` → 选本地上传 → `expect_file_chooser()` → `set_files()` → 等待 "已上传 1 张图片" → 确认。
- 发布两步：点 "预览并发布" → 点 "确认发布"。`stop_before_publish=True` 时停在预览，等待 `POST /api/publish-records/{id}/manual-confirm`。
- 关闭发布后浮窗（"作品同步授权"、"加入创作者计划"）。
- 编辑正文前先关 AI 助手抽屉。

## Task Execution

- `POST /api/tasks/{id}/execute` 立即返回 `202`。
- 测试 / 开发可走 `bg_session_factory` 在后台线程里跑（测试用 monkeypatch 把它指到 `TestingSessionLocal`）。
- 生产用 `server/worker/executor.py` 轮询 + 抢占（基于 `worker_id` / `worker_lease_until` 的乐观锁）。worker 还有 `_account_login_loop` 子线程处理账号登录会话请求。worker 周期性（约每 60 轮）重跑 `recover_stuck_records` 和 `recover_stuck_task_claims` 释放过期租约。
- 发布 worker **单实例**——不要 `docker compose up --scale worker=N`。发布记录的乐观锁能扛多实例，但同一 worker 内的账号登录处理器不安全。
- 并发：per-task 锁 → 全局信号量（`MAX_CONCURRENT_RECORDS=5`，覆盖用 `GEO_PUBLISH_MAX_CONCURRENT_RECORDS`）→ 每账号串行锁。
- `_release_account_lock` 写在 `finally` 里，**不要**在锁获取和这个 `finally` 之间塞 `return` / `raise`，否则账号锁到下次重启才释放。
- DB session **不是线程安全**的。`run_in_executor` 里所有 `db` 操作（flush / commit / refresh）都要在执行器线程内完成，不要把打开的 session 跨线程传递。
- `bg_session_factory` 在 route 内**懒导入**避免循环依赖；不要 toplevel 导入。

## Testing Notes

- `build_test_app(monkeypatch)` 要 `GEO_TEST_DATABASE_URL`，会重建一个一次性 MySQL schema、临时 data 目录、admin 用户、JWT cookie。它也会调 `browser._reset_globals()` 防止跨测试浏览器会话泄漏。
- 测试 DB 名必须含 `"test"`（安全检查）。确实需要时用 `GEO_ALLOW_NON_TEST_DATABASE_FOR_TESTS=1` 覆盖。
- `conftest.py` 注册 `@pytest.mark.mysql` 标记，`GEO_TEST_DATABASE_URL` 未设时自动跳过这些用例——所以裸跑 `pytest` 只跑无 DB 的用例。
- 用 `build_test_app` 的测试必须在 `finally` 里 `test_app.cleanup()`。
- 执行任务的测试要传 `"stop_before_publish": false`，否则记录会停在 `waiting_manual_publish`。
- Mock 发布运行器：`monkeypatch.setattr("server.app.modules.tasks.executor.build_publish_runner_for_record", lambda r: stub_runner)`。
- 分块上传相关测试集中在 `server/tests/test_assets_api.py`：`pytest server/tests/test_assets_api.py -q -k chunked`。

## AI 生文模块

设计 rationale、路线图、LangGraph 图见 `AI_GENERATION.md`。改这块代码的运营规则：

- **所有模型调用走 LiteLLM**。不要 import `anthropic` / `openai` SDK。
- 两套模型配置（都走 LiteLLM）：
  - `GEO_AI_MODEL` / `GEO_AI_API_KEY` — 主写作模型（默认 `claude-3-5-sonnet-20241022`）。
  - `GEO_AI_FORMAT_MODEL` / `GEO_AI_FORMAT_API_KEY` — 格式调整 / 标题识别 / 配图（默认 `deepseek/deepseek-v4-flash`）。超时由 `GEO_AI_FORMAT_TIMEOUT_SECONDS` 控制（默认 120）。
- 生文跑在 API server 的后台线程，**没有独立 worker**。`create_app()` 把 `bg_session_factory = SessionLocal` 注入 `ai_generation.router`；路由 spawn `Thread`，线程里的 LangGraph 节点从该工厂建 session。生产 `server/worker/executor.py` 不参与生文。
- Plan agent 顺序执行，是**唯一**允许读写 skill 共享文件（`article-plan.md`、`companion-pool.md`）的阶段。写作 agent 并发跑（`max_workers=4`），不要碰共享文件。
- 生成的文章直接通过 `create_article()` 落到现有 `articles` 表。`client_request_id` 做并发重试幂等。批次元数据放在独立的 `generation_sessions` 表（`article_ids` 用 JSON 数组存）。
- Markdown → Tiptap / HTML 转换在 `server/app/modules/ai_generation/converter.py`（`markdown_to_tiptap`、`markdown_to_html`）；LangGraph 的 `save_article` tool 在调 `create_article()` 前会调这两个函数。
- Skill = 文件夹（`SKILL.md` + `references/` + `skeletons/` + `assets/`），通过 `/api/skills` 管理。Prompt 模板是独立资源走 `/api/prompt-templates`——它们和 skill 是组合关系，不从属于 skill。
- 问题库（question pools）走 `/api/generation/question-pools/*`，支持从飞书多维表同步：依赖 `GEO_FEISHU_APP_ID` / `GEO_FEISHU_APP_SECRET`（与发飞书通知的 `GEO_FEISHU_WEBHOOK_URL` 是不同凭据）。

## Gotchas

- `ensure_data_dirs()` 在 `server/app/db/session.py` import 时就执行。
- 启动时 `create_app()` 会跑 `recover_stuck_records()` 复位上次崩溃留下的 `status='running'` 记录。失败只记日志、不致命——遇到僵死的 `running` 记录先看启动日志。
- FastAPI app 同时服务 SPA：任何非 `/api/` 路径返回 `web/dist/index.html`。所以从 FastAPI 端口（8000）访问 UI 必须先 `pnpm --filter @geo/web build`；开发时用 Vite dev server（5173）。
- 路由顺序：`POST /api/accounts/{account_id:int}/login-session` 必须在 `POST /api/accounts/{platform_code}/login-session` 之前注册。
- `TaskCreate.platform_code` 默认值是 `"toutiao"`。
- `build_publish_runner_for_record(record)` 通过账号 state path 提取的 `platform_code` 选驱动。
- 重试只对原始记录生效，对重试记录无效。
- `ArticleUpdate` 的 PATCH 会丢掉显式 `null`：`model_dump(exclude_unset=True)` 虽然包含 `None`，但 service 层在 `setattr` 前会过滤 `None`，所以 `PATCH {"field": null}` **不会**清空字段——需要清空时用哨兵值或专用清空端点。
- `ArticleCreate` 不接受 `stock_category_id` / `stock_category_ids`，只有 `ArticleUpdate` 接受；建文章时要带类别请用一次后续 PATCH。
- `complete_chunked_upload` 必须 re-raise `HTTPException`（例如 `415 Unsupported file type`），不要包成通用 `500`。
- noVNC 端口默认只绑 host `127.0.0.1`（docker-compose 设的）。远程操作者通过 VPN 或 SSH 隧道访问；公网暴露前自己加鉴权。
- 浏览器自动化发布依赖 `Xvfb` / `x11vnc` / `websockify` / `noVNC` 在 PATH（或通过 `GEO_PUBLISH_XVFB_PATH` 等指定）。这些只在 Docker 镜像里有，Windows 本地缺这套环境——本地能跑文章 CRUD / AI 生文 / 上传，**发布只能在容器里跑**。
- AI Key（`GEO_AI_API_KEY` / `GEO_AI_FORMAT_API_KEY`）**启动时不校验**，缺 / 错时在调用 LiteLLM 的请求里才报错。
- 生产 HTTPS 部署记得设 `GEO_SECURE_COOKIE=true`，否则 cookie 不会带 Secure 标志。
- 当前迁移头部随版本变化，看 `server/alembic/versions/` 最新一个文件即可；不要在文档里写死版本号。
