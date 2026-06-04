# 07 · 部署与运维文档

| 项 | 内容 |
|----|------|
| 部署形态 | Linux 服务器 + Docker Compose（**仅此一种**） |
| 关联文档 | [03 技术架构](./03-technical-architecture.md) · [06 开发指南](./06-development-guide.md) · 仓库 [`DEPLOYMENT.md`](../DEPLOYMENT.md)（详尽脚本与备份恢复 runbook） |

> 本文是上线与日常运维的总览。**完整的部署脚本、备份/恢复操作步骤以仓库根 `DEPLOYMENT.md` 为准**（含 `scripts/backup_db.sh`、`restore_db.sh`、`backup_files.sh`、`restore_files.sh` 的用法）；本文做拓扑、配置清单、监控与排障的体系化说明，避免重复抄录长脚本。

---

## 1. 部署拓扑（Docker Compose）

`docker-compose.yml` 定义 5 个服务 + 3 个命名卷：

| 服务 | 镜像/构建 | 端口（宿主机） | 职责 |
|------|-----------|----------------|------|
| `nginx` | `Dockerfile.nginx` | **80** | 反向代理：SPA / `/api` / `/novnc` ws |
| `app` | `Dockerfile` | 内部 8000 | Web API + SPA + AI 生文后台线程；启动跑 `alembic upgrade head` |
| `worker` | `Dockerfile` | 内部（noVNC 经 nginx 代理） | **单实例**发布 worker，命令 `alembic upgrade head && python -m server.worker.executor` |
| `mysql` | `mysql:8.0` | 不暴露（内网 `mysql:3306`） | 数据库，带 healthcheck |
| `minio` | `minio/minio` | `127.0.0.1:9000/9001` | 图片库对象存储 + 控制台 |

| 卷 | 内容 |
|----|------|
| `mysql_data` | MySQL 数据 |
| `app_data` | `/app/data`：assets / browser_states / exports / logs（app 与 worker 共享挂载） |
| `minio_data` | 图片库对象 |

> **关键约束**：
> - 发布 `worker` **只能 1 个实例**，不要 `--scale worker=N`（账号登录处理器与本地浏览器会话非多实例安全）。
> - `app` 与 `worker` 同时跑 `alembic upgrade head`；并发迁移由 Alembic 自身串行处理，但**首次部署建议先单独把 DB 迁移好**再起全量。
> - noVNC 仅经 nginx 代理 / 绑宿主机 `127.0.0.1`，公网暴露前自加鉴权（VPN / SSH 隧道 / nginx basic auth）。

镜像内含：Chromium + Xvfb + x11vnc + websockify + noVNC + 中文字体（`fonts-noto-cjk`）+ Playwright Chromium。Windows 本地无此环境，**发布只能在容器内跑**。

---

## 2. 首次部署（标准流程）

```bash
# 1. 准备环境变量
cp .env.example .env
# 编辑 .env：至少填 MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD / GEO_JWT_SECRET / GEO_SEED_USERS
#            以及 MINIO_ROOT_USER / MINIO_ROOT_PASSWORD（compose 要求必填）

# 2. 构建并启动
docker compose up --build -d

# 3. 种入初始用户（读取 GEO_SEED_USERS）
docker compose exec app python -m server.scripts.seed_users

# 4. 验证
docker compose ps
curl http://127.0.0.1/                 # SPA
docker compose logs -f app             # 看启动日志
```

打开浏览器访问 `http://<服务器 IP>/`。`DEPLOYMENT.md §1` 提供一键 `deploy-fresh.sh`。

---

## 3. 配置项清单（环境变量，前缀 `GEO_`）

> 来源：`server/app/core/config.py`、`.env.example`、`docker-compose.yml`。compose 已自动从 `MYSQL_*` 填充 `GEO_DB_*`。

### 3.1 必填

| 变量 | 说明 |
|------|------|
| `GEO_JWT_SECRET` | JWT 签名密钥（`openssl rand -hex 32`）。未设 app 启动即抛 RuntimeError |
| `GEO_DATA_DIR` | 数据目录（容器内 `/app/data`） |
| `MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD` | MySQL 密码（compose 必填） |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | MinIO 凭据（compose 必填 `:?required`） |
| `GEO_SEED_USERS` | 初始用户 JSON 数组，如 `[{"username":"admin","password":"..."}]` |

### 3.2 数据库

| 变量 | 默认 | 说明 |
|------|------|------|
| `GEO_DATABASE_URL` | — | 完整 URL（优先）；密码含特殊字符需手动 URL-encode |
| `GEO_DB_HOST/PORT/USER/PASS/NAME` | —/3306/—/—/— | 拆分凭据（推荐，免转义） |

### 3.3 鉴权 / Cookie

| 变量 | 默认 | 说明 |
|------|------|------|
| `GEO_JWT_EXPIRE_HOURS` | 8 | token TTL |
| `GEO_SECURE_COOKIE` | false | **生产 HTTPS 必须设 true**，否则 cookie 无 Secure 标志 |

### 3.4 发布引擎 / 远程浏览器

| 变量 | 默认 | 说明 |
|------|------|------|
| `GEO_PUBLISH_MAX_CONCURRENT_RECORDS` | 5（硬上限 5） | 全局并发发布数；生产建议先设 2–3 降风控 |
| `GEO_PUBLISH_RECORD_TIMEOUT_SECONDS` | 300 | 单记录超时 |
| `GEO_PUBLISH_BROWSER_CHANNEL` | chromium | 浏览器渠道 |
| `GEO_PUBLISH_BROWSER_EXECUTABLE_PATH` | 自动发现 | Chromium 路径 |
| `GEO_PUBLISH_XVFB_PATH` / `X11VNC_PATH` / `WEBSOCKIFY_PATH` | Xvfb/x11vnc/websockify | 可执行路径 |
| `GEO_PUBLISH_NOVNC_WEB_DIR` | — | noVNC 静态目录（容器内 `/usr/share/novnc`） |
| `GEO_PUBLISH_REMOTE_BROWSER_HOST` | 127.0.0.1 | 对外 host（容器内设 `0.0.0.0`，经 nginx 收口） |
| `GEO_PUBLISH_REMOTE_BROWSER_DISPLAY_BASE` / `VNC_BASE_PORT` / `NOVNC_BASE_PORT` | 99 / 5900 / 6080 | 端口分配基址 |
| `GEO_PUBLISH_REMOTE_BROWSER_START_TIMEOUT_SECONDS` | 15 | 启动超时 |
| `GEO_PUBLISH_REMOTE_BROWSER_IDLE_TIMEOUT_SECONDS` | 300 | 空闲清理 |

### 3.5 AI 生文（LiteLLM）

| 变量 | 默认 | 说明 |
|------|------|------|
| `GEO_AI_MODEL` / `GEO_AI_API_KEY` | claude-3-5-sonnet-20241022 / — | 主写作模型（**启动不校验**，调用时才报错） |
| `GEO_AI_FORMAT_MODEL` / `GEO_AI_FORMAT_API_KEY` | deepseek/deepseek-v4-flash / — | 标题/配图模型 |
| `GEO_AI_FORMAT_TIMEOUT_SECONDS` | 120 | 格式模型超时 |

### 3.6 MinIO / 飞书

| 变量 | 默认 | 说明 |
|------|------|------|
| `GEO_MINIO_ENDPOINT` / `ACCESS_KEY` / `SECRET_KEY` | localhost:9000 / — / — | 图片库存储 |
| `GEO_MINIO_SECURE` | false | HTTPS 时 true |
| `GEO_FEISHU_WEBHOOK_URL` | — | 任务完成告警（不设则静默跳过） |
| `GEO_FEISHU_APP_ID` / `GEO_FEISHU_APP_SECRET` | — | 选题库从多维表同步（与 webhook 是**不同凭据**） |

---

## 4. 日常运维操作

| 场景 | 命令 |
|------|------|
| 查看状态 | `docker compose ps` / `docker stats` |
| 看日志 | `docker compose logs -f app` / `... worker` |
| 代码更新（保留数据） | `git pull && docker compose up --build -d` |
| 仅重启 | `docker compose restart` |
| 进容器 | `docker compose exec app bash` |
| 跑迁移 | `docker compose exec app alembic upgrade head` |
| 重新种用户 | `docker compose exec app python -m server.scripts.seed_users` |
| 清空重建（危险） | `docker compose down -v && docker compose up --build -d` |

---

## 5. 监控与健康检查

- **应用健康**：`GET /api/system/status`（需 admin），返回 `service`、`directories_ready`、各计数、`worker_online`（30s 心跳）、`browser_ready`、`novnc_runtime_ready`。
- **Worker 在线**：`worker_heartbeats` 表，30s 内有心跳判定在线；前端系统工作区可见。
- **MySQL 健康**：compose `healthcheck`（`mysqladmin ping`）。
- **任务可观测**：任务 SSE 流 `GET /api/tasks/{id}/stream`、`task_logs`（含失败截图 asset）、飞书告警。
- **崩溃自愈**：app 启动跑 `recover_stuck_records()` 复位上次 crash 残留的 `running` 记录；worker 周期性跑 `recover_stuck_records` + `recover_stuck_task_claims` 释放过期租约。

---

## 6. 备份与恢复（要点；详见 `DEPLOYMENT.md §7`）

需要备份**两类**数据，缺一不可：

| 类别 | 内容 | 脚本 |
|------|------|------|
| 数据库 | MySQL `geo_collab` | `scripts/backup_db.sh` / `restore_db.sh` |
| 文件 | `app_data`（assets + browser_states + exports）、`minio_data`（图库） | `scripts/backup_files.sh` / `restore_files.sh` |

推荐 cron（与 `DEPLOYMENT.md` 一致）：

```cron
# 每天 03:00 备份 MySQL（保留 7 天）
0 3 * * * MYSQL_PASSWORD=$(grep MYSQL_PASSWORD ~/geo/.env | cut -d= -f2) bash ~/geo/scripts/backup_db.sh >> ~/geo/backups/backup.log 2>&1
# 每天 03:30 备份文件（默认跳过 chromium profile，只留 storage_state）
30 3 * * * BACKUP_SKIP_BROWSER_PROFILES=1 bash ~/geo/scripts/backup_files.sh >> ~/geo/backups/backup.log 2>&1
# 每天 04:00 异地同步（必须！否则服务器整体故障会连备份一起丢）
0 4 * * * rclone copy ~/geo/backups/ remote:geo-backups/ --max-age 24h
```

恢复脚本会自动：校验完整性 → 备份当前状态为恢复点（`.pre-restore`）→ 停服务 → 灌入 → 重启。**跨版本恢复后补跑 `alembic upgrade head`。**

> 文件数据丢失后果（务必重视）：`assets/` 丢 → 文章封面/内联图全 404；`browser_states/` 丢 → 所有账号要重新登录；`minio_data` 丢 → 图库全失。

---

## 7. 故障排查速查

| 症状 | 排查 |
|------|------|
| app 启动即退出 | 多半 `GEO_JWT_SECRET` 未设 → 看 `docker compose logs app` |
| 登录 403 | 用户 `must_change_password=true`，需先改密；或账号被禁用 |
| 前端 8000 端口空白 | SPA 需先 build（容器已带 `web/dist`；本地裸跑用 Vite 5173） |
| 发布卡 `waiting_user_input` | 正常：需 noVNC 人工处理验证码/登录，再 `resolve-user-input` |
| 发布卡 `running` 不动 | 检查 worker 是否在线（`worker_online`）；过期租约会被 recover 复位 |
| 账号锁不释放 | 检查是否有人在锁获取与 `finally` 之间加了 `return/raise`（代码缺陷）；重启 worker 兜底 |
| AI 生文一直失败 | `GEO_AI_API_KEY` 缺/错（启动不报，调用才报）；看 `generation_sessions.error_message` |
| 图片 404 | MinIO 未起 / 凭据错 / `app_data` 丢失 |
| 飞书无告警 | `GEO_FEISHU_WEBHOOK_URL` 未设（静默跳过） |

通用：`docker compose logs app | tail -50`、`docker compose logs worker | tail -50`、`docker compose ps`、`docker stats`。

---

## 8. 安全加固清单（上线前）

- [ ] `GEO_SECURE_COOKIE=true`（HTTPS）
- [ ] noVNC 不公网裸暴露（nginx basic auth / VPN / SSH 隧道）
- [ ] MinIO 控制台（9001）不对公网开放
- [ ] `.env` 权限收紧（`chmod 600`），不入库
- [ ] `GEO_SEED_USERS` 初始密码登录后立即改（新用户 `must_change_password=true`）
- [ ] 备份异地同步已配置并验证可恢复
- [ ] 发布并发 `GEO_PUBLISH_MAX_CONCURRENT_RECORDS` 按平台风控调低（2–3）

---

> 下一篇：[08 测试文档](./08-testing.md)。
