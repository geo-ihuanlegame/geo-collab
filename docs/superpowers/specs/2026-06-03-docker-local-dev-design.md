# Docker 本地集成测试环境 — 设计稿

- 日期：2026-06-03
- 状态：已与用户对齐，待实现
- 目标读者：在 Windows 桌面上做本地前后端集成测试的开发者

## 1. 背景与目标

`geo-collab` 现有的 Docker 配置（`docker-compose.yml` + `Dockerfile` + `Dockerfile.nginx` + `nginx.conf`）是**生产部署导向**的：

- nginx 在构建期把前端静态产物烤进镜像，改前端要重新 build，**没有热更新**。
- app 镜像很重：包含 Playwright + Chromium + noVNC + 中文字体，用于浏览器自动化发布。

开发者的诉求是**本地前后端集成测试**，同时保留**前端改动实时预览（HMR）**。现有生产配置无法同时满足这两点。

本设计提供一套**独立的开发用 Docker 编排**，与生产配置并存、互不影响。

### 非目标（YAGNI）

- 不在本地测试发布流程（浏览器自动化 / noVNC 人工接管）。因此**不构建重型镜像、不跑 worker**。
- 不改动生产的 `docker-compose.yml` / `Dockerfile` / `Dockerfile.nginx` / `nginx.conf`。
- 不做 Windows 桌面原生部署（项目本就不支持）。

## 2. 架构

```
宿主机 (Windows)
├── Vite dev server :5173   ← 改前端，秒级热更新 (HMR)
│     └─ /api 代理 → 127.0.0.1:8000   (vite.config.ts 默认值，无需改)
└── Docker (WSL2 后端)
      ├── app   :8000        FastAPI（精瘦镜像 + 源码挂载 --reload）
      ├── mysql :3306        MySQL 8.0（命名卷持久化）
      └── minio :9000/9001   对象存储（图片库依赖）
      ✗ 不跑 worker / nginx
```

数据流：浏览器只访问 `localhost:5173`；Vite 在服务端把 `/api/*` 转发到 `127.0.0.1:8000`（Docker 内的 app）。浏览器视角是同源，**不触发 CORS**（后端白名单本就含 `localhost:5173`）。鉴权 cookie 经 Vite 代理回写（去 `Secure`、域改写到 localhost），本地 http 下也能正常登录。

## 3. 组件清单

### 3.1 `Dockerfile.app`（新增）— 精瘦后端镜像

- 基础镜像 `python:3.12-slim`。
- 清华 pip 镜像加速，`COPY requirements.txt` 先装依赖（利用层缓存），再 `COPY` 源码。
- **不装** Chromium / noVNC / 中文字体，**不跑** `playwright install chromium`。
- 依据：app（web 进程）不发布，只 serve API。驱动只在顶层 `from playwright.sync_api import BrowserContext, Page` 导入**类型**，需要 pip 包（已在 requirements）但不需要浏览器二进制。`pystray` 不在 `server/` 内使用，无 X 依赖。
- 预期：镜像体积与构建时间从「GB 级 / 数分钟」降到「数百 MB / 约 1–2 分钟」。

### 3.2 `docker-compose.dev.yml`（新增）— 开发编排

服务：

- **mysql**：`mysql:8.0`，healthcheck（`mysqladmin ping`），命名卷 `mysql_data`，端口绑 `127.0.0.1:3306`。
- **minio**：`minio/minio`，命名卷 `minio_data`，端口 `127.0.0.1:9000/9001`。
- **app**：
  - `build: { dockerfile: Dockerfile.app }`。
  - `depends_on` mysql healthy、minio started。
  - `env_file: .env`，并注入 `GEO_DB_*`（指向 `mysql:3306`）、`GEO_DATA_DIR=/app/data`、`GEO_MINIO_*`（指向 `minio:9000`）。
  - **bind mount `./server:/app/server`** 实现后端热重载；命名卷 `app_data:/app/data` 持久化上传产物。
  - `WATCHFILES_FORCE_POLLING=true`（Windows bind mount 文件事件不可靠的兜底）。
  - `command`: `alembic upgrade head && python -m server.scripts.seed_users && uvicorn server.app.main:app --host 0.0.0.0 --port 8000 --reload`。
  - 端口绑 `127.0.0.1:8000:8000`。
- **不含** worker、nginx。

卷：`mysql_data`、`minio_data`、`app_data`。

### 3.3 `.env`（新增，已被 `.dockerignore` / git 忽略）

由脚本在缺失时生成，含开发默认值：

- `MYSQL_ROOT_PASSWORD` / `MYSQL_DATABASE=geo_collab` / `MYSQL_USER=geo_user` / `MYSQL_PASSWORD`
- `GEO_JWT_SECRET`（随机生成）
- `GEO_SEED_USERS=[{"username":"admin","password":"admin12345","role":"admin"}]`
- `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` + `GEO_MINIO_ENDPOINT=minio:9000` / `GEO_MINIO_ACCESS_KEY` / `GEO_MINIO_SECRET_KEY`
- AI Key 留空（启动不校验，用到才报错）。

### 3.4 `scripts/dev-docker.ps1`（新增）— 一键编排

- 缺 `.env` 时生成（随机 JWT secret）。
- `docker compose -f docker-compose.dev.yml up -d --build`。
- 轮询等待 app 在 8000 就绪。
- 打印 admin 账号密码。
- 在宿主启动 Vite：`pnpm --filter @geo/web dev`。
- 子命令：`-Down`（停）、`-Logs`（看日志）、`-Rebuild`（重建镜像）。
- 文件以 **UTF-8 BOM** 保存（Windows PowerShell 5.1 中文解析要求）。

### 3.5 `vite.config.ts`

无需改动：代理目标默认 `http://127.0.0.1:8000`，正对 Docker 内 app 暴露的端口。

## 4. Docker 安装（winget）

1. `wsl --install`（装 WSL2 + 启用虚拟机平台 + 默认发行版）。
2. **重启**（本会话会中断，重启后继续后续步骤）。
3. `winget install Docker.DockerDesktop`。
4. 启动 Docker Desktop，确认使用 WSL2 后端。

风险点：

- 需管理员 / UAC 提权。
- 必须重启。
- 依赖 BIOS 已开启 CPU 虚拟化（VT-x / AMD-V）。当前 `HypervisorPresent=False`；若 WSL2 起不来，需进 BIOS 开启虚拟化。

## 5. 验收标准

装完 Docker 后执行 `.\scripts\dev-docker.ps1`：

1. mysql / minio / app 三个容器起来且 app 健康。
2. `http://127.0.0.1:5173` 可打开。
3. 用 `admin` / `admin12345` 登录成功。
4. 文章列表能加载（证明 前端 ↔ 后端 ↔ MySQL 全链路打通）。
5. 改 `web/src/` 下文件保存后页面热更新；改 `server/` 下文件后容器内 uvicorn 自动 reload。

## 6. 优化点

- 精瘦镜像：砍掉 ~1GB 的 Chromium/noVNC/字体，构建快、占用小。
- dev 不起 worker/nginx：资源省、启动快。
- 后端源码挂载 `--reload`：改后端无需重建镜像。
- pip 清华镜像 + requirements 层缓存：依赖安装快。
- 生产 compose/Dockerfile 原样保留：不影响线上与团队协作。

## 7. 风险与回滚

- 若 BIOS 未开虚拟化 → WSL2/Docker 起不来。需用户进 BIOS 开启，本会话无法代办。
- 若 Windows bind mount 的 `--reload` 仍不触发 → 已用 `WATCHFILES_FORCE_POLLING=true` 兜底；仍失败则退化为「改后端后 `-Rebuild`」。
- 回滚极简：删除新增的 4 个文件 + `.env`，对生产配置零影响。
