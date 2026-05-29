# Geo 协作平台

多平台内容自动化发布平台：运营写好文章 → 选平台 + 账号 → 平台用 Playwright 控制浏览器自动登录、填写内容、点击发布。遇到验证码或登录态过期，通过 noVNC 远程接管浏览器，处理完继续。

> 仅支持 **Linux 服务器 + Docker Compose** 部署（浏览器自动化依赖 Xvfb / x11vnc / websockify / noVNC）。Windows 可本地开发非发布功能。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 API | FastAPI + SQLAlchemy + Alembic（MySQL only）|
| 浏览器自动化 | Playwright + Xvfb + x11vnc + websockify + noVNC |
| 前端 | React 19 + Vite + TypeScript + Tiptap |
| AI 生文 | LiteLLM + LangGraph |
| 对象存储 | MinIO（图片库）|
| 部署 | Docker Compose |

## 快速启动（Docker Compose，推荐）

```bash
cp .env.example .env
# 编辑 .env：至少填 MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD / GEO_JWT_SECRET / GEO_SEED_USERS
docker-compose up -d
docker-compose exec app python -m server.scripts.seed_users
```

容器启动时自动跑 `alembic upgrade head`。打开浏览器访问 `http://<服务器 IP>/`。

发布 worker **必须单实例**——不要 `docker compose up --scale worker=N`。noVNC 默认只绑宿主机 `127.0.0.1`，远程接管走 VPN 或 SSH 隧道。

## 本地开发

```bash
# Python 环境
conda activate geo_xzpt
pip install -r requirements.txt
playwright install chromium

# 必填三项环境变量
export GEO_JWT_SECRET=<long-random-string>
export GEO_DATA_DIR=/tmp/geo-data
export GEO_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_dev

# 数据库迁移
alembic upgrade head

# 后端（开发模式）
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000

# 前端（必须 5173 端口，CORS 只放行 5173）
pnpm install
pnpm --filter @geo/web dev
```

健康检查：

```bash
curl http://127.0.0.1:8000/api/system/status
```

## 核心概念

- **Account（账号）**：某个平台的运营账号，保存一份 Playwright `storage_state.json` 维持登录态。
- **Article（文章）**：待发布的内容，正文以 Tiptap JSON 为主，同时保存 HTML 和纯文本副本。
- **Task / Record**：一个 Task 对应"把某篇文章发布到某个账号"。每次执行落一条 Record，状态机：`pending → running → success / failed / waiting_manual_publish`。
- **PlatformDriver**：每个平台一个驱动文件，实现 `detect_logged_in()` 和 `publish()`。新增平台只需新增驱动 + 在 `create_app()` 中 import。

## 数据目录

`GEO_DATA_DIR` 控制数据存储位置（Docker 内默认 `/app/data`）：

```
data/
├── assets/             # 上传的图片/封面
├── browser_states/     # Playwright persistent profiles
│   └── <platform_code>/<account_key>/
│       ├── profile/            # Chromium profile
│       └── storage_state.json  # 登录态快照
├── exports/            # 账号授权导出 ZIP
└── logs/               # 远程浏览器 session 日志
```

## 项目结构

```
server/app/
├── main.py             # create_app()，路由注册、异常处理、SPA fallback
├── core/               # config / security / paths / limiter
├── db/                 # SessionLocal、Base
├── shared/             # errors / feishu / diagnostics / system_status
├── modules/            # 业务模块（每个含 models + schemas + service + router）
│   ├── system/         # User / Platform / 鉴权 / 健康检查
│   ├── accounts/       # 账号 + 登录会话 + 浏览器 profile + noVNC
│   ├── articles/       # 文章 + 分组 + 资源 + 分块上传 + AI 排版
│   ├── tasks/          # 任务 + 执行引擎 + 驱动注册表
│   ├── ai_generation/  # LangGraph 生文 + Markdown→Tiptap + 问题库
│   ├── image_library/  # 图片库 + MinIO
│   ├── skills/         # Skill 文件夹 CRUD
│   ├── prompt_templates/  # Prompt 模板 CRUD
│   └── audit/          # 审计日志（admin only）
worker/
└── executor.py         # 生产 worker：轮询、执行、账号登录子线程

web/src/
├── api/                # 按后端路由对应的 HTTP 客户端
└── features/           # content / accounts / tasks / system / ai-generation / image-library / prompt-templates / auth
```

## 文档

- **[CLAUDE.md](./CLAUDE.md)** — 开发约定、命令清单、模块速查、注意事项（AI 工具与人类开发者共用）。
- **[AI_GENERATION.md](./AI_GENERATION.md)** — AI 生文模块设计 rationale、LangGraph 流程图、路线图。
- **[AGENTS.md](./AGENTS.md)** — 指向 CLAUDE.md。

## 测试

```bash
# 需要独立的 MySQL 测试 DB（DB 名必须含 "test"）
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/ -q

# 单文件 / 单用例
pytest server/tests/test_assets_api.py -q -k chunked
pytest server/tests/test_articles_api.py::test_function_name -q
```

不设 `GEO_TEST_DATABASE_URL` 时，所有 `@pytest.mark.mysql` 用例自动跳过——裸跑 `pytest` 只会跑无 DB 的用例。

## 新增一个平台

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
        # 用 Playwright 操作页面…
        ...


register(MyPlatformDriver())
```

然后在 `server/app/main.py:create_app()` 顶部加一行 `import server.app.modules.tasks.drivers.myplatform  # noqa: F401`。

更多约定见 [CLAUDE.md → PlatformDriver](./CLAUDE.md#platformdriver)。
