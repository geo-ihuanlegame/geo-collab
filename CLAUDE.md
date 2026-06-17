# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 同步对象：本仓库 `AGENTS.md` 只是一个指向本文档的指针。所有事实更新都改这一份。

## Project Overview

**Geo 协作平台** — 多平台内容自动化发布平台。后端 FastAPI + SQLAlchemy/Alembic（MySQL only），前端 React 19 + Vite + TypeScript + Tiptap，浏览器自动化 Playwright + Xvfb/x11vnc/websockify/noVNC（远程人工接管），AI 生文走 LiteLLM + LangGraph，生产部署用 Docker Compose。

仓库里还 vendored 一个独立的 Node 子服务 `services/dailyhot-api/`（第三方 [DailyHotApi](https://github.com/imsyy/DailyHotApi)，热榜聚合，端口 6688）——后端 `hot_lists` 模块只是它的反向代理。这是仓库内**唯一**的非 Python 子服务，本地默认不跑、CI 不测；详见下文「热榜」与 `hot_lists/` 模块。

**设计文档 / 在途计划**：模块级 rationale 见各专题文档（`AI_GENERATION.md`、`DEPLOYMENT.md`）；feature 级计划 / 设计稿按日期命名落在 `docs/plans/YYYY-MM-DD-*.md` 与 `docs/specs/*-design.md`（superpowers 流程产出在 `docs/superpowers/`）。改某块前先 grep `docs/` 找有没有现成计划，避免与在途方案打架。`openspec/` 是新引入的变更提案工作流（`changes/` + `specs/`），目前主要走 archive。

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
pnpm --filter @geo/web lint          # eslint src
pnpm --filter @geo/web typecheck     # tsc -b
pnpm --filter @geo/web format:check  # prettier --check src（去掉 :check 直接改写）
```

> 前端**没有单元测试框架**（无 vitest / jest）——`typecheck` + `build` 就是前端的 CI 门禁，没有 `pnpm test`。

CI（`.github/workflows/ci.yml`，push 到 main 和所有 PR 触发）：**后端 ruff check / ruff format / mypy / pytest、前端 typecheck + build 都是硬门禁**；只剩前端 eslint 仍是 `continue-on-error` 的非阻塞步骤（存量 lint error 清完后再删掉 `continue-on-error` 变硬门禁）。CI 用 `mysql:8.0` service 起临时测试库 `geo_test`。

## Architecture

### Backend (`server/app/`)

- 应用工厂：`server/app/main.py:create_app()`。
- API 路由（全部挂在 `/api/` 下）：`auth`、`users`、`accounts`、`articles`、`article-groups`、`assets`、`chunked-assets`、`publish-records`、`system`、`tasks`、`prompt-templates`、`generation`、`pipelines`、`image-library`、`stock-images`、`audit-logs`、`hot-lists`。（`skills` 已下线、不再挂载，见下文 `skills/` 与「AI 生文模块」。）
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
- `tasks/` — 任务 CRUD（`service.py`）、执行引擎（`executor.py`）、发布运行器（`runner.py`）、驱动注册表与具体驱动（`drivers/`）；从 `router.py` 导出 `tasks_router` 和 `publish_records_router`。`VALID_TASK_TYPES = single / group_round_robin / article_round_robin`（`article_round_robin` 由 `TaskCreate.article_ids` 触发，pipeline 的 `distribute` 节点用它发指定文章列表；都复用 `_validate_articles_approved` 审核门禁 + `_build_assignments` round-robin 派号）。`publish_tasks.task_type` 有 DB CHECK 约束，新增类型要写迁移改约束（见 `0042`）。
- `ai_generation/` — **当前主流程是「问题池 → 方案池 → 方案运行」**：问题池镜像飞书多维表（`question_bank.py`，`/api/generation/question-pools/*`；**问题池全员共享**——所有登录用户同看 / 同建 / 改名（`PATCH`）/ 同步同一批池，`QuestionPool.user_id` 仅作"创建者"溯源不再过滤可见性，唯独 **`DELETE` 收归 admin**（`require_admin`，软删 `is_deleted`）。配套 `question_source` 节点也已去掉属主校验。**方案 / 方案运行仍按用户私有**（`_get_owned_scheme`））、方案 CRUD + 校验 + 问题快照（`scheme_service.py`，`/api/generation/schemes/*`）、方案运行 executor（`scheme_executor.py`，`/api/generation/scheme-runs/*`，`ThreadPoolExecutor(max_workers=4)` 并发生文，每篇成功后 best-effort 调 `ai_format` 自动排版 + 全 bucket 智能配图）、写作模型调用（`article_writer.py`）、问题池定时同步后台线程（`sync_scheduler.py`，开关 `GEO_QUESTION_POOL_AUTO_SYNC_ENABLED`、周期 `GEO_QUESTION_POOL_SYNC_INTERVAL_SECONDS`）、Markdown→Tiptap 转换（`converter.py`）；路由在 `router.py` + `scheme_router.py`。**旧的 `/api/generation/sessions` 问题池直连 + LangGraph 流水线（`pipeline.py` / `service.py` / `generation_sessions` 表）已硬切 410，保留休眠不删。**
- `pipelines/` — **可视化工作流编排**（前端 UI 叫「智能体管理」，后端 / DB 一律叫 pipeline / 工作流，注意命名错位）。`/api/pipelines/*`。一条 pipeline 是一串线性节点（`PipelineNode`，按 `node_index` 顺序执行），节点类型走和 driver 一样的**注册表模式**：`nodes/base.py` 的 `register(node_type, handler)` + `nodes/__init__.py` 触发导入，`main.py` 顶部 `import server.app.modules.pipelines.nodes` 激活注册。内置 8 个节点：源节点 `input` / `question_source`（`question_types` 多选问题类型，含未分类哨兵 `__uncategorized__`；可选 `question_record_ids` 按飞书 record_id 精选具体问题——非空即只发这些、忽略类型；兼容旧单选 `question_type`） / `article_group_source`（`group_id` 可选，留空按 FIFO 自动选「最早一个含 已审核+未分发 文章」的分组，输出该组已审未发子集） / `approved_content_source`（已审核且未分发过=去重、跨分组），处理 `ai_generate` / `ai_compose`，动作 `to_review`（送审，支持 `daily_group` 开关：开启后按 `GEO_SCHEDULER_TZ` 当天日期归入同一个「每日生成 · 日期」分组（去重追加），关闭=每次运行新建组（默认）；**`ai_generate` 与 `ai_compose`（AI创作，新建 pipeline 主用生文节点）同有 `daily_group` 开关**（默认关；共用 helper `nodes/daily_group_stream.py:make_group_streamer`）：开启则生成前先建好同名「每日生成 · 日期」分组、每篇生成成功即流式进组+标待审+commit（运行中可实时逐篇观察、中途失败不丢已生成的），输出 group_id 后 to_review 靠「上游已带 group_id 就透传」守卫让位、executor 不再兜底成组；并发安全＝append 绝不动组行 + sort_order 走进程内计数器（不用 DB FOR UPDATE），详见 `docs/superpowers/specs/2026-06-15-streaming-daily-group-design.md`）/ `distribute`（**优先消费上游 `article_ids`→`article_round_robin`**，否则 `group_id`→`group_round_robin`，空 `article_ids` 安静跳过不建任务；内部 `create_task` 带审核门禁。**必须先判 `article_ids` 再判 `group_id`**：`article_group_source` 默认透传会同时带二者，先走分组路径会重拉全组、丢弃「已审未发」子集，见 #46）。**「已分发/在途」判定＝文章存在「未软删 且 `status` 非 `failed`/`cancelled`」的 `PublishRecord`**（failed/cancelled/软删记录允许重新分发、不永久埋没；`approved_content_source` 与 `article_group_source` 共用此判定，见 #40）。运行日志 `GET /api/pipelines/{id}/logs` 从 `PipelineRun.node_results` 派生逐节点日志（服务端分页 + 起止日期筛选，纯函数 `run_logs.py:build_run_log_rows`），前端全页 `AgentLogsView`。节点间数据传递 / 跳过条件是纯逻辑在 `flow_meta.py`（`inputMapping` 默认透传整个上游输出，`condition` 控制跳过）。草稿 / 发布版本快照在 `service.py` + `snapshot.py`（`Pipeline.draft_snapshot` / `PipelineVersion`）；**运行创建时冻结节点快照（`PipelineRun.snapshot`），执行只读快照**，创建→执行之间的改动不影响本次运行。执行：`executor.py` 后台线程线性跑节点，全局并发闸 `GEO_PIPELINE_MAX_CONCURRENT_RUNS`（默认 3）+ 同一 pipeline 行锁串行化（活跃 run 抛 `ConflictError`）；和 generation 一样**没有独立 worker**，`create_app()` 注入 `bg_session_factory = SessionLocal`。定时调度 `scheduler.py`（开关 `GEO_PIPELINE_SCHEDULER_ENABLED`、周期 `GEO_PIPELINE_SCHEDULER_INTERVAL_SECONDS`、时区 `GEO_SCHEDULER_TZ`），`run_due_pipelines_once` 纯函数式可测、条件 UPDATE claim 防重叠。`recovery.py:recover_stuck_pipeline_runs()` 在启动时把残留 running/pending 全量置 failed（无租约，进程刚起时它们必是僵死）。测试在 `server/tests/test_pipeline*.py` 等；另有 `test_auto_distribute.py`（分发/`article_round_robin`/`approved_content_source`）、`test_group_source_auto.py`（分组源 FIFO 自动选组）、`test_question_source_multiselect.py`（问题源多选）、`test_pipeline_logs.py`（运行日志）——这几个不匹配 `test_pipeline*` glob。
- `image_library/` — 图片库模型、MinIO 存储（`store.py`）、选图 / 插图、生文钩子（`hook.py`）；路由在 `router.py`。**需要 MinIO**：`GEO_MINIO_ENDPOINT` / `GEO_MINIO_ACCESS_KEY` / `GEO_MINIO_SECRET_KEY`（HTTPS 加 `GEO_MINIO_SECURE=true`）。图片按 `StockCategory` 分桶，文章通过 `article_stock_categories` 多对多关联类别。
- `skills/` — Skill 模型、CRUD、路由。**`/api/skills` 已下线**（`main.py` 不再 import / mount），模块文件、`skills` 表、`GenerationSession.skill_id` 全部保留休眠（不 drop、不写迁移）。新方案流不使用 Skill。
- `prompt_templates/` — PromptTemplate 模型、CRUD、路由。
- `audit/` — 审计日志：`AuditLog` 模型 + `service.list_audit_logs()` 游标分页；路由 `/api/audit-logs`，**仅 admin**，参数 `user_id` / `action_prefix` / `target_type` / `target_id` / `start_at` / `end_at` / `cursor` / `limit≤500`。
- `hot_lists/` — **「热榜」tab 的后端代理**，故意**不遵守**「每模块 `models.py + schemas.py + service.py + router.py`」约定：无 model / 无 schema / 无 DB / 无缓存（缓存交给上游的 NodeCache），只有 `service.py`（`httpx.AsyncClient` 异步转发）+ `router.py`。把请求转给独立 Node 子服务 `services/dailyhot-api/`（DailyHotApi）。上游地址读 **`GEO_HOTLIST_API_URL`**（默认 `http://127.0.0.1:6688`），**直接 `os.environ` 读、故意不进 `Settings`/`get_settings()`**（避免与在途改 `config.py` 的 WIP 冲突）。路由 `/api/hot-lists`（`/all` 全量）和 `/api/hot-lists/{source}`（单源，`limit≤500` + `cache` 开关；`source` 必须匹配 `^[a-z0-9-]+$`，否则 400）。上游连不上 / 超时 → service 抛 `HotListUpstreamError`，router 转 **502**。鉴权在 `main.py` 注册时统一加 `Depends(get_current_user)`，router 自身不带。测试 `test_hot_lists_service.py`（用 `httpx.MockTransport` 打桩、**无需 DB**）和 `test_hot_lists_api.py`。

### Shared (`server/app/shared/`)

- `errors.py` — `ClientError`、`ConflictError`、`AccountError`、`ValidationError`。
- `diagnostics.py` — 发布诊断。
- `feishu.py` — 飞书 webhook 推送（`GEO_FEISHU_WEBHOOK_URL`）。**注意**：问题库从多维表同步走的是 `GEO_FEISHU_APP_ID` / `GEO_FEISHU_APP_SECRET`（用来换 `tenant_access_token`），和 webhook 是两条不同的凭据。
- `system_status.py` — 系统健康检查。

### Frontend (`web/`)

React 19 + Vite + TypeScript（strict）+ Tiptap + Lucide。Feature 拆分在 `web/src/features/`：`content/`、`accounts/`、`tasks/`、`system/`、`ai-generation/`、`pipelines/`（UI 标题「智能体管理」，含 `PipelineEditor` 节点编辑器 + `VersionHistory` + `AgentLogsView`）、`image-library/`、`prompt-templates/`、`hot-lists/`（「热榜」tab，`HotListsWorkspace`）、`auth/`。API 客户端在 `web/src/api/`（按后端路由对应，pipelines 对应 `web/src/api/pipelines.ts`、热榜对应 `web/src/api/hot-lists.ts`）。`App.tsx` 顶部 tab 用 `visitedTabs` 懒挂载 + `display:none` 缓存，每个 tab 包 `ErrorBoundary`。开发时 Vite 把 `/api` 代理到 `127.0.0.1:8000`。

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

驱动分两类：浏览器驱动（默认，实现 `publish(page, context, ...)`）和 **API 驱动**
（类属性 `mode = "api"`，实现 `publish_api(payload: ApiPublishPayload)`，发布不起浏览器，
`build_publish_runner_for_record` 据此分叉到 `runner_api.run_publish_api`）。
微信公众号（`wechat_mp`）是首个 API 驱动：终点为草稿箱（draft/add 即 succeeded，不调
freepublish），封面自动压 JPG≤64KB、正文图压 ≤1MB 转传换微信 URL。账号凭据存
`Account.api_credentials`（AppID/AppSecret，API 永不回传 secret 原文），token 缓存在
`Account.api_token_cache` 跨进程共享；`POST /api/accounts/{id}/verify-credentials` 验证凭据。
注意：微信接口要求服务器出口 IP 在公众平台白名单内，否则 40164。

## Toutiao Automation Notes

- 头条用字节自家的设计系统：`byte-btn`、`byte-btn-primary`、`syl-toolbar-tool`，**不是 Ant Design**。
- 改自动化代码前先用 Playwright 对着实时 DOM 校验选择器。
- 封面图必填：`ToutiaoDriver._handle_cover()` 在 `article.cover_asset is None` 时抛错。
- 封面上传链路：点 `.add-icon` → 选本地上传 → `expect_file_chooser()` → `set_files()` → 等待 "已上传 1 张图片" → 确认。
- 发布两步：点 "预览并发布" → 点 "确认发布"。`stop_before_publish=True` 时停在预览，等待 `POST /api/publish-records/{id}/manual-confirm`。
- 关闭发布后浮窗（"作品同步授权"、"加入创作者计划"）。
- 编辑正文前先关 AI 助手抽屉。
- 头条发布驱动可切换：`GEO_TOUTIAO_DRIVER=inpage` 走页内 API 适配器（`drivers/toutiao_inpage.py`，调头条官方发布接口），未设或其它值走默认的 Playwright DOM 驱动（`drivers/toutiao.py`）。两者都注册，便于灰度与回滚。

## Task Execution

- `POST /api/tasks/{id}/execute` 立即返回 `202`。
- 测试 / 开发可走 `bg_session_factory` 在后台线程里跑（测试用 monkeypatch 把它指到 `TestingSessionLocal`）。
- 生产用 `server/worker/executor.py` 轮询 + 抢占（基于 `worker_id` / `worker_lease_until` 的乐观锁）。worker 还有 `_account_login_loop` 子线程处理账号登录会话请求。worker 主循环周期性（约每 60 轮，`_periodic_recovery`）重跑 `recover_stuck_records`、`recover_stuck_task_claims`（释放过期租约、复位卡死记录/认领，不只在启动跑一次——#7）和 `_check_stuck_tasks`，各包 try/except 互不拖累。
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
    - 写作模型可在前端下拉切换：候选来自 `GEO_AI_ENGINES`（JSON 数组，每项 `label/model/api_key/base_url`，`api_key` 空则回落 `GEO_AI_API_KEY`）。下拉存 model 串，运行时 `config.resolve_engine()` 回查该引擎的 key/base_url 显式传给 LiteLLM。`AiEngineRead` 只暴露 `label/model`，绝不下发 key。
  - `GEO_AI_FORMAT_MODEL` / `GEO_AI_FORMAT_API_KEY` — 格式调整 / 标题识别 / 配图（默认 `deepseek/deepseek-v4-flash`）。超时由 `GEO_AI_FORMAT_TIMEOUT_SECONDS` 控制（默认 120）。
- 生文跑在 API server 的后台线程，**没有独立 worker**。`create_app()` 把 `bg_session_factory = SessionLocal` 注入 `ai_generation.router` 和 `scheme_router`，并启动问题池定时同步线程（`start_auto_sync`）。方案运行路由 spawn `Thread`，线程里 `scheme_executor` 用 `ThreadPoolExecutor(max_workers=4)` 并发跑 task，每个 worker 自建 session（session 非线程安全）。生产 `server/worker/executor.py` 不参与生文。
- Plan agent 顺序执行，是**唯一**允许读写 skill 共享文件（`article-plan.md`、`companion-pool.md`）的阶段。写作 agent 并发跑（`max_workers=4`），不要碰共享文件。
- 生成的文章直接通过 `create_article()` 落到现有 `articles` 表。`client_request_id` 做并发重试幂等。批次元数据放在独立的 `generation_sessions` 表（`article_ids` 用 JSON 数组存）。
- Markdown → Tiptap / HTML 转换在 `server/app/modules/ai_generation/converter.py`（`markdown_to_tiptap`、`markdown_to_html`）；LangGraph 的 `save_article` tool 在调 `create_article()` 前会调这两个函数。
- **Skill 已下线**：`/api/skills` 不再挂载、模块休眠（旧 LangGraph agent 的能力入口）。新方案流不用 Skill，只用提示词模板（`/api/prompt-templates`，覆盖 `generation` + `ai_format` 两种 scope）。
- 问题库（question pools）走 `/api/generation/question-pools/*`，支持从飞书多维表同步：依赖 `GEO_FEISHU_APP_ID` / `GEO_FEISHU_APP_SECRET`（与发飞书通知的 `GEO_FEISHU_WEBHOOK_URL` 是不同凭据）。
- 方案池 / 方案运行（scheme pool / scheme run）：方案以 `question_type`（`QuestionItem.category`）为粒度，每行选问题 + 文章数 + 允许的提示词模板；`POST /api/generation/schemes/{id}/runs` 异步展开 task 并发生文，`GET /api/generation/scheme-runs/{run_id}` 查状态（`done` / `partial_failed` / `failed`）。运行只读方案保存时的问题快照，飞书后续改动不影响已存方案。

## Gotchas

- `ensure_data_dirs()` 在 `server/app/db/session.py` import 时就执行。
- 启动时 `create_app()` 会跑 `recover_stuck_records()` 复位上次崩溃留下的 `status='running'` 记录。失败只记日志、不致命——遇到僵死的 `running` 记录先看启动日志。pipeline run 同理由 `recover_stuck_pipeline_runs()` 在启动时全量复位（无租约，进程刚起时 running/pending 必是僵死）。定时触发本身靠条件 UPDATE claim（`last_scheduled_run_at < slot`，`rowcount==1` 才算抢到）跨进程去重是安全的；但这个**无租约的全量复位**意味着**别跑多实例 web**——第二个实例启动会把第一个实例正在跑的 pipeline run 误判成僵死置 failed。
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
- 微信公众号发布走纯 HTTP（无浏览器），Windows 本地 / CI 可全链路跑；但需要服务器出口公网 IP 加入公众平台 IP 白名单（报 40164 时先查这个）。`distribution_enabled=false` 的账号会被 pipeline distribute 自动派号过滤（全停用时节点安静跳过）。
