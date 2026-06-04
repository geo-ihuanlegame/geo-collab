# 03 · 技术架构设计文档

| 项 | 内容 |
|----|------|
| 关联文档 | [02 产品设计](./02-product-design.md) · [04 数据库设计](./04-database-design.md) · [05 API 接口](./05-api-reference.md) · [07 部署运维](./07-deployment-operations.md) |

> 本文描述系统如何被构建：整体架构、进程拓扑、模块划分、并发与一致性模型、浏览器自动化、AI 管线，以及背后的关键设计决策与权衡。

---

## 1. 技术栈总览

| 层 | 技术 | 备注 |
|----|------|------|
| Web API | FastAPI（应用工厂 `create_app()`） | 同时托管 SPA |
| ORM / 迁移 | SQLAlchemy + Alembic | **MySQL only**，无 SQLite 兼容 |
| 数据库 | MySQL 8（`mysql+pymysql`） | 全文检索用 `FULLTEXT ... WITH PARSER ngram` |
| 前端 | React 19 + Vite + TypeScript(strict) + Tiptap + Lucide | 端口 5173（CORS 限定） |
| 浏览器自动化 | Playwright（Chromium） | 持久化 profile + storage_state |
| 远程接管 | Xvfb → x11vnc → websockify → noVNC | 仅 Linux/Docker |
| AI | LiteLLM（统一网关）+ LangGraph（编排） | 禁止直接 import anthropic/openai SDK |
| 对象存储 | MinIO | 图片库 |
| 限流 | slowapi | `app.state.limiter` |
| 配置 | pydantic-settings（前缀 `GEO_`） | `get_settings()` lru_cache 单例 |
| 部署 | Docker Compose + Nginx | 单 worker 实例 |

---

## 2. 进程拓扑（运行时架构）

系统在生产中由**三类长生命周期进程** + 两个有状态依赖组成：

```
                         ┌────────────────────────── Nginx ──────────────────────────┐
   浏览器(运营)  ───────► │  / → SPA(FastAPI)   /api → web   /novnc → noVNC ws         │
   浏览器(接管)  ───────► │  （noVNC 仅绑宿主机 127.0.0.1，远程经 VPN/SSH 隧道）        │
                         └───────────────┬───────────────────────────┬───────────────┘
                                         │                           │
                          ┌──────────────▼─────────┐     ┌───────────▼───────────────┐
                          │  Web 进程（app）        │     │  发布 Worker（单实例）      │
                          │  FastAPI / uvicorn      │     │  server/worker/executor.py │
                          │  · REST API + SSE       │     │  · 轮询 DB 抢任务（乐观锁） │
                          │  · AI 生文后台线程       │     │  · 执行发布记录             │
                          │    （无独立 worker）     │     │  · _account_login_loop 子线程│
                          └───────────┬─────────────┘     └───────────┬───────────────┘
                                      │                               │
                          ┌───────────▼───────────────────────────────▼───────────────┐
                          │                MySQL 8（唯一协调点 / 状态源）               │
                          │     任务/记录/账号/文章/审计 + 租约(worker_lease/lease_until)│
                          └────────────────────────────────────────────────────────────┘
                          ┌────────────────────────────────────────────────────────────┐
                          │  MinIO（图片库对象存储）        飞书（告警 webhook / 选题多维表）│
                          └────────────────────────────────────────────────────────────┘
```

**关键点**：
- **DB 是唯一协调点**。Web 与 Worker 之间不直接通信，全部通过 MySQL 的状态字段 + 租约协调（无消息队列）。
- **AI 生文跑在 Web 进程的后台线程**里（`create_app()` 注入 `bg_session_factory = SessionLocal`，路由 spawn `Thread`），**不经发布 worker**。
- **发布 worker 必须单实例**：发布记录用乐观锁可扛多实例，但同进程内的账号登录处理器（profile 锁、本地浏览器会话字典）不是多实例安全的。
- **开发期**可不起 worker，用 `bg_session_factory` 在后台线程直接执行（测试用 monkeypatch 指到 `TestingSessionLocal`）。

---

## 3. 后端代码架构（模块化单体）

应用是**模块化单体**：一个 FastAPI app，按业务域切分自包含模块。

```
server/app/
├── main.py                 # create_app()：注册路由/异常/SPA fallback/启动恢复
├── core/
│   ├── config.py           # Settings（GEO_ 前缀）、上传上限、magic bytes
│   ├── security.py         # JWT 签发/校验、get_current_user、require_admin、用户缓存
│   ├── limiter.py          # slowapi
│   ├── paths.py            # 数据目录解析
│   └── time.py             # UTC 时间
├── db/
│   ├── base.py             # Declarative Base
│   └── session.py          # SessionLocal；import 时 ensure_data_dirs()
├── shared/
│   ├── errors.py           # ClientError/ConflictError/AccountError/ValidationError
│   ├── feishu.py           # webhook 告警（任务终态）
│   ├── feishu_bitable.py   # 多维表读取（选题同步）
│   ├── diagnostics.py      # 发布诊断事件（线程局部）
│   └── system_status.py    # 健康检查
└── modules/                # 每个模块 = models + schemas + service + router
    ├── system/             # User/Platform/WorkerHeartbeat；auth/users/system 三路由
    ├── accounts/           # 账号 + 登录会话(auth.py) + 浏览器(browser.py)
    ├── articles/           # 文章 + 分组 + 资源 + 分块上传 + AI 排版(ai_format.py)
    ├── tasks/              # 任务 + 执行引擎(executor) + 运行器(runner) + drivers/
    ├── ai_generation/      # LangGraph 管线 + 转换器 + 问题库
    ├── image_library/      # 图片库 + MinIO(store) + selector/inserter/hook
    ├── skills/             # Skill CRUD
    ├── prompt_templates/   # Prompt 模板 CRUD（generation/ai_format 两 scope）
    └── audit/              # 审计日志（admin only）
```

**分层约定（每模块内）**：
- `models.py`：SQLAlchemy ORM。
- `schemas.py`：Pydantic 输入输出。
- `service.py`：业务逻辑，**抛命名异常**（`ConflictError`/`ValidationError`/`AccountError`/`ClientError`），不抛裸 `ValueError`（无全局兜底）。
- `router.py`：HTTP 端点，鉴权依赖、限流装饰。

**异常 → HTTP 映射（`main.py` 全局处理器）**：

| 异常 | HTTP |
|------|------|
| `ConflictError` | 409 |
| `ValidationError` / `AccountError` / `ClientError` | 400 |
| 其它未捕获 | 500 |

---

## 4. 鉴权与会话

- 登录 `POST /api/auth/login` → 校验 bcrypt 密码 → 签发 JWT，写 httpOnly cookie `access_token`（HS256，payload `{sub, role, exp}`，TTL=`GEO_JWT_EXPIRE_HOURS` 默认 8h）。
- `get_current_user` 依赖解析 cookie → 校验 token → 查 User（**60s 用户缓存**降库压）→ 校验 `is_active`；`must_change_password=True` 返回 403。
- `require_admin` 依赖校验 `role=='admin'`。
- 引导：`GET /api/bootstrap`（公开）判断是否需创建初始 admin；`GEO_SEED_USERS`（JSON 数组）由 `seed_users.py` 在 Docker 启动种入。
- CORS 写死 `127.0.0.1:5173` / `localhost:5173`，`allow_credentials=False`。
- 启动**必须**有 `GEO_JWT_SECRET`，否则 `create_app()` 抛 `RuntimeError`。
- `core/security.py` 的 `require_local_token()` 是**死代码**，新接口勿照抄。

---

## 5. 发布执行引擎（核心难点）

发布是系统最复杂的子系统，涉及并发、租约、浏览器生命周期与人工接管。

### 5.1 任务编排

- 创建任务时按类型构建分配：`single`（单文章单账号）或 `group_round_robin`（分组内文章按 `article_index % N_accounts` 轮询到账号），每个(文章,账号)生成一条 `PublishRecord`。
- `POST /api/tasks/preview` 不落库返回分配结果。
- `POST /api/tasks/{id}/execute` 立即 `202`；真正执行由 worker 抢占（或开发期后台线程）。

### 5.2 三级并发控制

```
per-task 锁（同一任务不并发执行，进程内 threading.Lock，非阻塞获取）
   └─► 全局信号量 MAX_CONCURRENT_RECORDS=5（GEO_PUBLISH_MAX_CONCURRENT_RECORDS，硬上限 5）
          └─► per-account 串行锁（同一账号同时最多 1 条记录，降低风控）
```
- per-account 锁的 `_release_account_lock` 写在 `finally`，**禁止**在获取锁与该 finally 之间插入 `return`/`raise`，否则账号锁泄漏到下次重启。

### 5.3 Worker 轮询与乐观锁租约

`server/worker/executor.py`：
- `WORKER_ID = hostname-pid`。
- 主循环：写心跳 → 每 ~60 轮跑 `recover_stuck_records` / `recover_stuck_task_claims` → `_claim_next_task` → 执行 → 释放认领。
- **认领乐观锁**：`UPDATE publish_tasks SET worker_id=?, worker_lease_until=now+10min WHERE id=? AND worker_id IS NULL`，`rowcount==1` 才算抢到（CAS）。
- **记录级租约**：执行中 `lease_until` 防崩溃后重复执行；过期记录在启动时由 `recover_stuck_records` 重置回 `pending` 并写 warn 日志。
- **子线程** `_account_login_loop`：独立处理账号登录会话请求（与发布解耦）。
- **心跳续租**：执行中持续延长 worker 租约与活跃 profile 锁（`BrowserProfileLock`，900s）。

### 5.4 浏览器运行器与驱动

- `runner.py`：编排 Playwright —— 起/复用浏览器会话、解析素材、调驱动、管理 noVNC、捕获诊断与截图。**驱动拿到的是构建好的 `PublishPayload`**，不直接 import 文章/账号/资源 ORM。
- 驱动注册：实现 `PlatformDriver` Protocol（`code/name/home_url/publish_url/detect_logged_in/publish`），模块 import 时 `register(...)`，并在 `main.py` 顶部 import 触发注册。当前仅 `toutiao` 已实现。
- 驱动异常：`PublishError(message, screenshot=)` 驱动级失败；`UserInputRequired` 仅用于非预期人工干预（验证码/失效），**正常停顿（stop_before_publish）不抛它**。

### 5.5 人工接管闭环

| 状态 | 触发 | 出口 |
|------|------|------|
| `waiting_manual_publish` | `stop_before_publish=true` 停在预览 | `manual-confirm` → succeeded/failed |
| `waiting_user_input` | 驱动抛 `UserInputRequired`（附 novnc_url） | `resolve-user-input` → 重置 pending 重跑 |

---

## 6. 浏览器自动化与 noVNC 接管

发布与登录都需要一个"人能看见、也能接管"的真实浏览器：

```
Playwright(Chromium, 持久化 profile)
        │  渲染到
        ▼
   Xvfb（虚拟 X display :N）
        │  被
        ▼
   x11vnc（VNC server，绑 127.0.0.1:5900+N）
        │  代理为 WebSocket
        ▼
   websockify ──► noVNC（浏览器内的 VNC 客户端，6080+N）
```
- 端口分配：display base 99、vnc base 5900、novnc base 6080（可配）。
- 跨进程可见：`BrowserSession` 表镜像进程内会话，使 Web 进程能查询 Worker 持有的会话。
- 空闲清理：`GEO_PUBLISH_REMOTE_BROWSER_IDLE_TIMEOUT_SECONDS` 默认 300s。
- 安全：noVNC 默认只绑宿主机 `127.0.0.1`（docker-compose），公网前需自加鉴权。
- **平台限制**：这套全家桶只在 Docker 镜像里有，Windows 本地缺失 → 本地可跑文章 CRUD / AI 生文 / 上传，**发布只能在容器内**。

---

## 7. AI 生文管线（LangGraph + LiteLLM）

```
run_pipeline()  ── LangGraph ──►
   planner_node      （准备任务清单；task_specs 由 _build_task_specs() 构建）
        │
   parallel_write_node  ThreadPoolExecutor(max_workers=4)
        │   每篇： litellm.completion(GEO_AI_MODEL)
        │        → 解析 Markdown、取 # 标题
        │        → markdown_to_tiptap / markdown_to_html（converter.py）
        │        → create_article()（落 articles 表）
        │        → 问题库 item 标记 consumed（手动模式）
        │
   finalize_node     会话 status = done / failed，写 error_message
```
- **两套模型（都走 LiteLLM）**：`GEO_AI_MODEL`（主写作，默认 claude-3-5-sonnet）；`GEO_AI_FORMAT_MODEL`（标题识别/配图，默认 deepseek-v4-flash，超时 `GEO_AI_FORMAT_TIMEOUT_SECONDS`）。
- **共享文件纪律**：plan 阶段顺序执行，是唯一可读写 skill 共享文件的阶段；写作 agent 并发，不碰共享文件。
- **幂等**：`client_request_id` 做并发重试幂等；批次元数据存独立 `generation_sessions`（`article_ids` JSON）。
- **AI 排版**（`articles/ai_format.py`）：用 `GEO_AI_FORMAT_MODEL` 识别小标题（段落升级为 h2，从不降级既有标题）+ 可选自动插图；状态经 `ai_checking` / `ai_format_error` 暴露，后台线程执行、前端轮询。

---

## 8. 数据 / 存储模型要点

- 数据库 MySQL only；`get_database_url()` 优先 `GEO_DATABASE_URL`，否则拼 `GEO_DB_*`；`alembic.ini` 的 url 是占位符，运行时被覆盖。
- 全文检索：MySQL `FULLTEXT ... WITH PARSER ngram`（无 Elasticsearch），迁移由 `test_fts_and_migrations.py` 验证。
- 软删除：核心表用 `is_deleted` + `deleted_at`，查询统一过滤。
- 文件存储：`GEO_DATA_DIR` 下 `assets / browser_states / exports / logs`；`ensure_data_dirs()` 在 `db/session.py` import 时执行。
- 图片库走 MinIO，按 `StockCategory` 分桶，文章经 `article_stock_categories` 多对多关联分类。
- 详见 [04 数据库设计](./04-database-design.md)。

---

## 9. 关键设计决策与权衡（Why）

| 决策 | 选择 | 理由 / 权衡 |
|------|------|-------------|
| 协调机制 | **DB 租约**，无 MQ | 部署简单、单实例足够；代价是轮询延迟、worker 不能水平扩 |
| 发布 worker 数 | **单实例** | 账号登录处理器与本地浏览器字典非多实例安全；记录乐观锁本可扩，但整体受限于登录处理器 |
| 自动化失败处理 | **人工接管(noVNC)** 而非整单失败 | 直面平台风控/验证码痛点（P3），把"卡住"变可恢复，代价是引入有状态浏览器会话与端口管理 |
| AI 网关 | **LiteLLM 统一** | 模型可换、成本可分（主写作 vs 格式两套模型）；禁止直连 SDK 保证可替换 |
| AI 生文执行位置 | **Web 进程后台线程** | 复用 DB session 工厂、免独立 worker；代价是与 Web 争 CPU、重启会中断（靠会话状态可重试） |
| 正文存储 | **三份并行**(json/html/text) | 编辑、渲染、发布各取所需，避免反复转换；代价是写入需三份同步（后端转换保证） |
| 鉴权 | **JWT httpOnly cookie** | 简单、防 XSS 读取；CORS 收紧到 5173 |
| 模块结构 | **模块化单体** | 团队小、迭代快；边界清晰为未来拆分留余地 |
| 数据库 | **MySQL only** | 统一 ngram 全文检索与生产环境；放弃 SQLite 测试便利换取一致性 |
| 上传 | **>3MB 分块** | 大图稳定上传；SHA256 服务端算（前端不算）|

---

## 10. 非功能性约束

- **可靠性**：启动 `recover_stuck_records()` 复位崩溃残留的 `running` 记录（失败只记日志不致命）。
- **一致性**：DB session 非线程安全，`run_in_executor` 内所有 db 操作必须在执行器线程内完成，不跨线程传 session。
- **安全**：生产 HTTPS 设 `GEO_SECURE_COOKIE=true`；审计日志敏感字段（password/token/secret 等）自动脱敏；AI Key 启动不校验，调用时才报错。
- **限流**：登录 5/min；新端点用 `@limiter.limit(...)`。
- **可观测**：任务 SSE 流、TaskLog（含截图 asset）、审计日志、`/api/system/status`、`WorkerHeartbeat`。

---

> 下一篇：[04 数据库设计](./04-database-design.md)。
