# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Geo 协作平台** — Linux 服务器多平台内容自动化发布平台。管理文章并自动发布到头条号等平台（后续可扩展搜狐、网易、小红书等）。架构：FastAPI 后端 + React/TypeScript 前端 + Playwright 浏览器自动化 + Xvfb/x11vnc/noVNC 远程人工介入。**仅支持 Linux 服务器部署（Docker Compose）**。

## Dev Commands

**激活 Python 环境**（必须）：
```bash
conda activate geo_xzpt
```

**后端开发服务器**（端口 8000）：
```bash
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000
```

**前端开发服务器**（端口 5173，代理 `/api` → `:8000`）：
```bash
pnpm --filter @geo/web dev
```

**运行测试：**
```bash
pytest server/tests/
pytest server/tests/test_tasks_api.py  # 单文件
```

**数据库迁移：**
```bash
alembic upgrade head
```

**构建前端：**
```bash
pnpm --filter @geo/web build
```

**Docker Compose 启动（推荐开发/部署方式）：**
```bash
docker-compose up --build
```

## Architecture

### Backend (`server/app/`)

FastAPI app，SQLAlchemy + Alembic migrations。**测试用 SQLite 内存库；生产用 MySQL**（`GEO_DB_*` 或 `GEO_DATABASE_URL`）。必填环境变量：`GEO_DATA_DIR`、`GEO_JWT_SECRET`。参考 `.env.example`。

**Core models** (`server/app/models/`):
- `Platform` — 发布目标平台（如 toutiao）
- `Account` — 平台账号，含 Playwright storage state 路径
- `Article` — 文章内容：JSON（Tiptap 编辑器）、HTML、纯文本三份存储
- `ArticleGroup` + `ArticleGroupItem` — 文章集合，用于批量发布
- `Asset` — 上传的图片，存储在 `data_dir/assets/`
- `PublishTask` → `PublishRecord` → `TaskLog` — 任务执行状态机

**Routes** (`server/app/api/routes/`): accounts, articles, groups, assets, tasks, records, system.

**Services** (`server/app/services/`):
- `drivers/__init__.py` — **PlatformDriver Protocol + 注册表**（`register` / `get_driver` / `all_driver_codes`）；新平台只需新建一个 driver 文件
- `drivers/toutiao.py` — **ToutiaoDriver**：实现头条号全部 Playwright 发布逻辑，模块 import 时自动调用 `register(ToutiaoDriver())`
- `publish_runner.py` — **`run_publish()`**：通用发布编排，按 `account.state_path` 中的 platform_code 取 driver，启 Xvfb 远程会话，调 `driver.publish(...)`
- `browser_sessions.py` — Xvfb + x11vnc + websockify → noVNC 流水线（Linux only）
- `accounts.py` — 账号登录/校验/导入导出；路径按 `platform_code` 区分（`browser_states/<platform_code>/<account_key>/`）
- `tasks.py` — 任务执行引擎；`build_publish_runner_for_record(record)` → `run_publish()`
- `assets.py` — 文件存储（`store_bytes` / `resolve_asset_path`）

### Frontend (`web/`)

React 19 + Vite + TypeScript。主账号 UI 在 `web/src/features/accounts/AccountsWorkspace.tsx`，使用 `DEFAULT_PLATFORM_CODE = "toutiao"` 控制当前平台（后续加平台时在此加选择器）。Tiptap 富文本编辑器。Lucide React 图标。

### Data Directory

`GEO_DATA_DIR`（必须设置，Docker 内默认 `/app/data`）：
- `geo.db` — SQLite 数据库
- `assets/` — 上传的图片
- `browser_states/<platform_code>/<account_key>/` — Playwright 持久化 profile + `storage_state.json`
- `exports/` — 账号授权导出 ZIP
- `logs/browser-sessions/` — 每个远程浏览器 session 的进程日志

## PlatformDriver — 扩展新平台

实现 `server/app/services/drivers/__init__.py` 中的 `PlatformDriver` Protocol：

```python
class MyPlatformDriver:
    code = "myplatform"       # 与 Platform.code 一致
    name = "我的平台"
    home_url = "https://..."
    publish_url = "https://..."

    def detect_logged_in(self, *, url, title, body) -> bool: ...
    def publish(self, *, page, context, article, account, state_path, stop_before_publish): ...

# 文件底部
from server.app.services.drivers import register
register(MyPlatformDriver())
```

然后在 `server/app/main.py:create_app()` 顶部 import 一次触发注册即可。

## PlatformDriver — Toutiao 实现细节

`server/app/services/drivers/toutiao.py` 自动化头条号发布。

**关键实现细节：**
- 使用 `launch_persistent_context`（profile 目录与 storage_state 分离）
- **头条号使用字节自研组件库**（`byte-btn`、`byte-btn-primary`、`publish-btn-last`）— **不是** Ant Design 类名
- 操作正文编辑区前必须先关闭 AI 创作助手抽屉（`.close-btn`）
- 封面图**必填** — `_handle_cover()` 在 `article.cover_asset` 为 None 时直接抛出
- 封面上传流程：点击 `.add-icon` → 对话框 → "本地上传" → `expect_file_chooser()` + `set_files()` → 等待"已上传 1 张图片"文字（最长 60s）→ 点"确定"
- 发布**两步走**：点"预览并发布" → 等 1.5s → 点"确认发布"（两个不同按钮）
- 发布后处理弹窗："作品同步授权"对话框和"加入创作者计划"弹窗都需要关闭

**修改 Playwright 选择器时：** 用 `playwright-cli`（`@playwright/cli`）检查实时页面，拿到真实 `ref=eXXX` 元素句柄。不要猜测选择器 — 头条号 DOM 变化频繁。用 `open`、`snapshot`、`click`、`screenshot` 命令验证实际页面结构后再写代码。

## Task Execution Model

`POST /api/tasks/{id}/execute` 立即返回 202 — 任务**异步执行**。

**生产环境（Worker 进程）：** 独立 worker 轮询 DB 认领并执行任务，API 仅清理过期认领后返回。启动命令：
```bash
python -m server.worker.executor
```

**测试环境：** `bg_session_factory` 被 monkeypatch 为 `TestingSessionLocal` 时，execute 端点在后台线程本地执行任务（无需 worker 进程）。

**并发控制：** 每个任务一把 `threading.Lock` 防止重复执行；每个账号一把 Lock 串行处理；全局上限 `MAX_CONCURRENT_RECORDS = 5`（`ThreadPoolExecutor`）。

**发布流程：** `build_publish_runner_for_record(record)` → `run_publish(article, account, ...)` → `managed_remote_browser_session` 启 Xvfb → `driver.publish(...)`

**`stop_before_publish=true` 流程：** driver 点"预览并发布"但跳过"确认发布"，record 留在 `waiting_manual_publish` 状态。调 `POST /api/publish-records/{id}/manual-confirm` 带 `{"outcome": "succeeded"|"failed", ...}` 解决。

**Error convention：** 使用 `server/app/services/errors.py` 中的具名异常，不要 raise 裸 `ValueError`：
- `ValidationError` → HTTP 400（用户输入校验失败）
- `AccountError` → HTTP 400（账号不存在/过期/平台不匹配）
- `ClientError` → HTTP 400（其他客户端可见错误的基类）
- `ConflictError(ClientError)` → HTTP 409（乐观锁/幂等冲突）

## Testing

测试用 `pytest`。`server/tests/utils.py` 中 `build_test_app` / `build_test_client` 构建内存 SQLite 应用并将 `GEO_DATA_DIR` monkeypatch 到临时目录。覆盖所有 API 路由；浏览器自动化不做单元测试。

**在任务测试中 mock 发布器：**
```python
monkeypatch.setattr(tasks, "build_publish_runner_for_record", lambda r: stub_runner)
# stub_runner 签名：(article, account, *, stop_before_publish=False) -> PublishFillResult
```

**Driver 测试：** `server/tests/test_drivers.py` — 测试注册表和 ToutiaoDriver 属性/登录检测逻辑。

**发布编排测试：** `server/tests/test_publish_runner.py` — 用 stub driver 和 monkeypatch 测 `run_publish()` 路由和异常处理，不真实启动 Playwright。
