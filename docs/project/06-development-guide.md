# 06 · 开发指南

| 项 | 内容 |
|----|------|
| 适用 | 后端 Python 3.12 / 前端 Node 24 + pnpm 10.4 |
| 关联文档 | [03 技术架构](./03-technical-architecture.md) · [07 部署运维](./07-deployment-operations.md) · [08 测试](./08-testing.md) · 仓库根 [`CLAUDE.md`](../CLAUDE.md) |

> 本文是"上手开发"的操作手册。底层约定的唯一事实源是仓库根 `CLAUDE.md`；本文做体系化串联与新人友好的步骤说明。

---

## 1. 环境准备

### 1.1 后端

```bash
conda activate geo_xzpt          # 项目 conda 环境
pip install -r requirements.txt           # 运行依赖
pip install -r requirements-dev.txt       # 开发依赖（ruff/mypy/pytest 插件）
playwright install chromium               # 浏览器（发布相关，本地仅用于非发布开发也可装）
```

### 1.2 前端

```bash
pnpm install
```

### 1.3 数据库

需要本地 MySQL 8（开发库 + 测试库各一个，测试库名必须含 `test`）。

```sql
CREATE DATABASE geo_dev  CHARACTER SET utf8mb4;
CREATE DATABASE geo_test CHARACTER SET utf8mb4;
```

### 1.4 必填环境变量（写进 `.env` 或 export）

```bash
GEO_JWT_SECRET=<long-random-string>     # 未设置 create_app() 抛 RuntimeError
GEO_DATA_DIR=/path/to/local/data        # assets/browser_states/logs/exports 落此
GEO_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_dev
# 或拆开：GEO_DB_HOST / GEO_DB_PORT / GEO_DB_USER / GEO_DB_PASS / GEO_DB_NAME
```

> 完整配置项见 [07 部署运维 §配置项清单](./07-deployment-operations.md)。

---

## 2. 常用命令

### 2.1 运行

```bash
# 后端（开发热重载）
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000

# 数据库迁移到最新
alembic upgrade head

# 前端 dev（必须 5173 端口，CORS 只放行 5173）
pnpm --filter @geo/web dev

# 生产 worker（轮询 DB、执行发布；与 web 进程分离，单实例）
python -m server.worker.executor
```

健康检查：`curl http://127.0.0.1:8000/api/system/status`（需 admin cookie）。

### 2.2 质量检查（与 CI 一致）

```bash
# 后端
ruff check server/                 # lint：E/F/I/B/UP，line-length=100，忽略 E501/B008
ruff format --check server/        # 格式（去掉 --check 直接改写）
mypy server/app                    # 宽松类型检查

# 前端
pnpm --filter @geo/web lint        # eslint src
pnpm --filter @geo/web typecheck   # tsc -b
pnpm --filter @geo/web build       # 产物 web/dist（FastAPI 托管 SPA 需要）
```

CI 门禁：**pytest（后端）+ typecheck/build（前端）必过**；ruff/format/mypy/eslint 为非阻塞步骤（`continue-on-error`），详见 [08 测试](./08-testing.md)。

### 2.3 测试

```bash
# 全量（需测试库，库名含 "test"）
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/ -q

# 单文件 / 单用例
pytest server/tests/test_assets_api.py -q -k chunked
pytest server/tests/test_articles_api.py::test_function_name -q
```

未设 `GEO_TEST_DATABASE_URL` 时，`@pytest.mark.mysql` 用例自动跳过 —— 裸跑 `pytest` 只跑无 DB 用例。

---

## 3. 代码组织与分层约定

每个业务域是自包含模块（`server/app/modules/<domain>/`）：

```
models.py    SQLAlchemy ORM
schemas.py   Pydantic 输入输出
service.py   业务逻辑（抛命名异常，不抛裸 ValueError）
router.py    HTTP 端点（鉴权依赖、限流装饰）
```

**铁律**：
- service 层抛 `ConflictError`（→409）/ `ValidationError`·`AccountError`·`ClientError`（→400）。**不要抛裸 `ValueError`**——没有全局兜底，会变 500。
- 新端点需要限流：`@limiter.limit("N/minute")`。
- 改环境变量后测试要 `get_settings.cache_clear()`（配置走 `@lru_cache`）。
- `core/security.py:require_local_token()` 是**死代码**，新接口勿照抄。

---

## 4. 新增一个发布平台驱动

这是最常见的扩展点。三步：

### 4.1 写驱动

```python
# server/app/modules/tasks/drivers/myplatform.py
from server.app.modules.tasks.drivers import register
from server.app.modules.tasks.drivers.base import PublishPayload, PublishResult


class MyPlatformDriver:
    code = "myplatform"
    name = "我的平台"
    home_url = "https://example.com"
    publish_url = "https://example.com/editor"

    def detect_logged_in(self, *, url, title, body) -> bool:
        return "退出登录" in body

    def publish(self, *, page, context, payload: PublishPayload, stop_before_publish: bool) -> PublishResult:
        # 用 Playwright 操作页面填内容、传封面、点发布
        # stop_before_publish=True 时：填好但停在预览，正常 return（不要抛 UserInputRequired）
        ...


register(MyPlatformDriver())
```

### 4.2 触发注册

在 `server/app/main.py:create_app()` 顶部加一行：

```python
import server.app.modules.tasks.drivers.myplatform  # noqa: F401
```

### 4.3 关键约束

- 驱动**只拿到构建好的 `PublishPayload`**，不要在驱动里 import 文章/账号/资源 ORM。
- 异常：
  - `PublishError(message, screenshot=None)` —— 驱动级失败，可附截图 bytes（随发布记录持久化）。
  - `UserInputRequired` —— **仅**用于非预期人工干预（验证码/登录失效），需 noVNC 接管。`stop_before_publish=True` 的正常停顿**不要**抛它。
- 改自动化代码前用 Playwright 对实时 DOM 校验选择器（平台 DOM 常变）。
- 头条特例（参考 `drivers/toutiao.py`）：用字节设计系统（`byte-btn`/`syl-toolbar-tool`，非 Ant Design）；封面必填；发布两步（预览→确认）；先关 AI 助手抽屉与发布后浮窗。

---

## 5. 新增一个业务模块

1. 在 `server/app/modules/<domain>/` 建 `models.py`+`schemas.py`+`service.py`+`router.py`。
2. 在 `db/base.py` 的 metadata 能发现新模型（保证 import 链可达）。
3. 写 Alembic 迁移：`alembic revision --autogenerate -m "..."`，**人工核对**生成结果（autogen 不可全信，尤其 MySQL TEXT/索引）。
4. 在 `main.py:create_app()` 用 `include_router(...)` 挂载，决定前缀与鉴权依赖。
5. 前端在 `web/src/api/` 加对应客户端、`web/src/features/` 加工作区。

---

## 6. 关键 Gotchas（高频踩坑）

> 完整清单见 `CLAUDE.md → Gotchas`，这里列最易中招的：

- **正文三份同步**：`content_json`/`content_html`/`plain_text` 改一份要同步另两份（后端 `converter.py`/`parser.py`）。前端只编辑 Tiptap JSON。
- **PATCH 不清空**：`ArticleUpdate` 的 PATCH 传 `null` 不会清空字段（service 过滤 None）。需清空用专用端点/哨兵。
- **建文章带类别**：`ArticleCreate` 不收 `stock_category_id(s)`，建后用 PATCH 补。
- **分块上传 415**：`complete_chunked_upload` 必须 re-raise `HTTPException`，别包成 500。
- **前端不算 SHA256**：别用 `crypto.subtle.digest()`，SHA256 由 `merge_chunks()` 服务端算。
- **账号锁 finally**：`_release_account_lock` 在 `finally`，别在获取锁与该 finally 之间塞 `return`/`raise`，否则锁泄漏到重启。
- **DB session 非线程安全**：`run_in_executor` 内的 db 操作（flush/commit/refresh）必须在执行器线程内完成，别跨线程传 session。
- **路由顺序**：`/{account_id:int}/login-session` 必须先于 `/{platform_code}/login-session`。
- **数据目录副作用**：`ensure_data_dirs()` 在 `db/session.py` import 时即执行。
- **SPA 托管**：从 8000 端口看 UI 需先 `pnpm --filter @geo/web build`；开发用 Vite(5173)。
- **AI Key 不在启动校验**：缺/错时调用 LiteLLM 才报错。
- **bg_session_factory 懒导入**：在路由内懒导入避免循环依赖，别 toplevel import。

---

## 7. AI 生文模块开发约定

- **所有模型调用走 LiteLLM**，禁止 import `anthropic`/`openai` SDK。
- 两套模型：`GEO_AI_MODEL`/`GEO_AI_API_KEY`（主写作）；`GEO_AI_FORMAT_MODEL`/`GEO_AI_FORMAT_API_KEY`（标题/配图，`GEO_AI_FORMAT_TIMEOUT_SECONDS` 默认 120）。
- 生文跑在 **API server 后台线程**（无独立 worker）：`create_app()` 注入 `bg_session_factory=SessionLocal`，路由 spawn `Thread`。
- Plan 阶段**顺序执行**，是唯一可读写 skill 共享文件（`article-plan.md`/`companion-pool.md`）的阶段；写作 agent 并发（`max_workers=4`），不碰共享文件。
- 生成的文章经 `create_article()` 落 `articles` 表；`client_request_id` 做幂等；批次元数据在 `generation_sessions`。
- 设计 rationale / LangGraph 图见 `AI_GENERATION.md`。

---

## 8. 提交前自检清单

- [ ] `ruff check server/` 与 `ruff format --check server/` 无新增告警
- [ ] `mypy server/app` 无新增错误
- [ ] 相关 `pytest` 用例通过（执行任务的测试记得传 `"stop_before_publish": false`）
- [ ] 前端 `pnpm --filter @geo/web typecheck` + `lint` 通过
- [ ] 改了 model → 有对应 Alembic 迁移且人工核对
- [ ] 改了正文结构 → 三份同步
- [ ] 新端点 → 鉴权依赖、限流、错误用命名异常
- [ ] 更新了相关文档的「状态」标记

---

> 下一篇：[07 部署与运维](./07-deployment-operations.md)。
