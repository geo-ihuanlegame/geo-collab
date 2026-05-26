# Geo 协作平台

Geo 协作平台云端发布管理系统。**仅支持 Linux 服务器部署（Docker Compose）**。

---

## 新手入门指南

### 这个项目是什么

Geo 是一个**多平台内容自动化发布平台**。

核心场景：运营人员在平台上写好一篇文章，点击"发布"，系统自动用 Playwright 控制浏览器登录头条号、百家号等平台，把文章内容、封面图、标题一并填好并发出去——全程不需要人工操作。

```
用户写文章
    ↓
创建发布任务（选平台 + 账号）
    ↓
Worker 进程拉取任务
    ↓
Playwright 打开对应平台的浏览器
    ↓
自动填写内容 → 点击发布
    ↓
记录发布结果
```

如果遇到验证码或登录态过期，系统支持通过 noVNC 远程接管浏览器，手工处理后继续。

---

### 技术栈一览

| 层 | 技术 |
|---|---|
| 后端 API | FastAPI + SQLAlchemy (MySQL) |
| 浏览器自动化 | Playwright + Xvfb（无头 Linux 环境） |
| 前端 | React 19 + TypeScript + Vite + Tiptap 富文本 |
| AI 生文 | LiteLLM + LangGraph |
| 部署 | Docker Compose |

---

### 核心概念

理解这四个概念，基本上就理解了整个系统：

**Account（账号）**
平台账号（如某个头条号）。每个账号保存一份 Playwright 的 `storage_state.json`（浏览器登录态），避免每次发布都要重新登录。

**Article（文章）**
待发布的内容。正文用 Tiptap 格式存储（同时保存 HTML 和纯文本副本），封面图作为 Asset 关联。

**Task / Record（任务 / 发布记录）**
一个 Task 对应"把某篇文章发布到某个账号"。Task 下有多条 Record，每次执行对应一条 Record（支持重试）。Record 有状态机：`pending → running → success / failed / waiting_manual_publish`。

**PlatformDriver（平台驱动）**
每个平台（头条、百家等）有独立的 Driver，实现 `detect_logged_in()` 和 `publish()` 两个方法。新增平台只需新增一个 Driver 文件并注册。

---

### 新手推荐阅读顺序

#### 第 0 步：先跑起来

```bash
# 1. 激活 Python 环境
conda activate geo_xzpt

# 2. 复制并填写环境变量（必填三项见下方说明）
cp .env.example .env

# 3. 启动后端
alembic upgrade head
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000

# 4. 启动前端（另开终端）
pnpm --filter @geo/web dev
```

`.env` 必填项：

```
GEO_JWT_SECRET=随便一个长字符串
GEO_DATA_DIR=/tmp/geo-data
GEO_DATABASE_URL=mysql+pymysql://user:pass@127.0.0.1:3306/geo_dev
```

**先跑起来，在浏览器里点几下，再看代码效果翻倍。**

---

#### 第 1 步：项目骨架（配置 + 数据库）

| 文件 | 看什么 |
|---|---|
| `server/app/main.py` | `create_app()` — 路由如何注册、中间件如何挂载 |
| `server/app/core/config.py` | 所有 `GEO_` 环境变量的含义 |
| `server/app/db/session.py` | 数据库连接方式 |

---

#### 第 2 步：数据模型（理解业务实体）

按顺序读各模块的 `models.py`：

```
modules/system/models.py    → User、Platform
modules/accounts/models.py  → Account、AccountLoginSession
modules/articles/models.py  → Article、Asset、Tag、ArticleGroup
modules/tasks/models.py     → PublishTask、PublishRecord、TaskLog
```

不需要深读，知道每张表存什么就够了。

---

#### 第 3 步：一条完整链路（串联理解）

跟着"发布一篇文章"走完整流程：

```
1. articles/router.py        → POST /api/articles        创建文章
2. tasks/router.py           → POST /api/tasks           创建任务
3. tasks/router.py           → POST /api/tasks/:id/execute  触发执行
4. tasks/executor.py         → claim_and_run()           Worker 拉取并执行
5. tasks/runner.py           → run_publish()             启动 Xvfb + Playwright
6. tasks/drivers/toutiao.py  → publish()                 头条具体操作逻辑
```

每一步只需看懂"入参是什么、出参是什么、调用了谁"，不必逐行读。

---

#### 第 4 步：前端结构

```
web/src/
├── features/
│   ├── content/     ← 文章列表、编辑器
│   ├── accounts/    ← 账号管理、登录态
│   ├── tasks/       ← 任务列表、发布记录
│   └── system/      ← 用户、平台配置
└── api/             ← 后端接口封装（对应后端路由）
```

前端代理 `/api` 到后端 `8000` 端口，可以直接在 Network 面板看接口调用。

---

#### 可以暂时跳过的部分

| 目录 / 文件 | 跳过理由 |
|---|---|
| `server/alembic/versions/` | 数据库迁移历史，用到时再查 |
| `modules/ai_generation/` | 独立的 AI 生文功能，不影响主流程理解 |
| `modules/image_library/` | 需要 MinIO，本地开发环境复杂 |
| `skills/` | Skill 模板文件，和业务逻辑无关 |
| `server/worker/executor.py` 细节 | 先理解接口，Worker 内部机制后面再读 |

---

### 新增一个平台（最小示例）

```python
# server/app/modules/tasks/drivers/myplatform.py
from server.app.modules.tasks.drivers import register, PlatformDriver

class MyPlatformDriver:
    code = "myplatform"
    name = "我的平台"
    home_url = "https://example.com"
    publish_url = "https://example.com/editor"

    def detect_logged_in(self, *, url, title, body) -> bool:
        return "退出登录" in body

    def publish(self, *, page, context, payload, stop_before_publish):
        # 用 Playwright 操作页面...
        pass

register(MyPlatformDriver())
```

然后在 `server/app/main.py` 的 `create_app()` 里加一行 import 即可。

---

## Docker Compose 快速启动

```bash
cp .env.example .env
# 编辑 .env，设置 MYSQL_ROOT_PASSWORD、GEO_JWT_SECRET、GEO_SEED_USERS
docker-compose up -d
docker-compose exec app python -m server.scripts.seed_users
# 打开浏览器访问 http://服务器IP/
```

Docker 启动时自动执行 `alembic upgrade head`，无需手动迁移。

发布 worker 按单实例设计，请不要使用 `docker compose up --scale worker=N`。noVNC 端口默认只绑定宿主机 `127.0.0.1`，远程处理登录/验证码请通过 VPN 或 SSH 隧道访问。


## 环境

- Python 使用 conda 环境：`geo_xzpt`
- 前端使用 Node.js + pnpm

## 后端开发

```bash
conda activate geo_xzpt
pip install -r requirements.txt
# 需要 MySQL；设置 GEO_DATABASE_URL，或设置 GEO_DB_HOST/GEO_DB_USER/GEO_DB_NAME
alembic upgrade head
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/api/system/status
```

## 前端开发

```bash
pnpm install
pnpm --filter @geo/web dev
```

## 数据目录

`GEO_DATA_DIR` 环境变量控制数据存储位置，Docker 内默认为 `/app/data`：

```
data/
├── assets/             # 上传图片
├── browser_states/     # Playwright persistent profiles
│   └── <platform_code>/
│       └── <account_key>/
│           ├── profile/            # Chromium profile 目录
│           └── storage_state.json  # 登录态快照
├── exports/            # 账号授权导出 ZIP
└── logs/               # 远程浏览器 session 进程日志
```
