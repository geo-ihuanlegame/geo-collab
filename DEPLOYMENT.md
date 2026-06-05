# 部署脚本指南

## 1. 彻底清空 + 重新部署

**完整一键脚本：**

```bash
#!/bin/bash
set -e

# 备份 .env
mkdir -p /tmp/geo_backup
cp ~/geo/.env /tmp/geo_backup/.env 2>/dev/null || echo "No previous .env found"

# 清空所有
cd ~
docker compose -f ~/geo/docker-compose.yml down -v 2>/dev/null || true
rm -rf ~/geo

# 重新 clone
git clone https://github.com/44lf/geo-collab.git ~/geo
cd ~/geo

# 恢复 .env（如果有备份）
if [ -f /tmp/geo_backup/.env ]; then
    cp /tmp/geo_backup/.env .env
    echo "✓ 恢复 .env"
else
    echo "⚠ 未找到 .env 备份，请手动创建"
fi

# 初始化数据库 + 启动容器
docker compose up --build -d
echo "✓ 容器启动中..."
sleep 10

# 播种初始用户
docker compose exec -T app python -m server.scripts.seed_users
echo "✓ 初始用户创建完成"

# 验证状态
docker compose ps
echo ""
echo "✓ 部署完成！"
echo "前端: http://localhost"
echo "API: http://localhost:8000/docs"
```

保存为 `~/deploy-fresh.sh`，然后：
```bash
chmod +x ~/deploy-fresh.sh
~/deploy-fresh.sh
```

---

## 2. 优化部署 - 加速构建和启动

### 2.1 多阶段构建优化（Dockerfile）

新增 `.dockerignore`：
```
.git
.gitignore
node_modules
pnpm-store
dist
.env
.env.local
__pycache__
*.pyc
.pytest_cache
.venv
```

修改 Dockerfile 第一阶段（Web 构建）：
```dockerfile
# 分离依赖安装和源码
FROM node:22-bookworm-slim AS web-deps
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY web/package.json web/package.json
RUN corepack enable && corepack prepare pnpm@10.4.0 --activate
RUN npm config set registry https://registry.npmmirror.com
RUN pnpm install --frozen-lockfile

FROM node:22-bookworm-slim AS web-build
COPY --from=web-deps /app /app
WORKDIR /app
COPY web ./web
RUN pnpm --filter @geo/web build
```

Python 部分分离依赖：
```dockerfile
FROM python:3.12-slim AS python-deps
RUN sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc websockify novnc chromium \
    fonts-noto-cjk libnss3 libnspr4 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt

FROM python:3.12-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc websockify novnc chromium \
    fonts-noto-cjk libnss3 libnspr4 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=python-deps /usr/local/bin /usr/local/bin

WORKDIR /app
COPY . .
COPY --from=web-build /app/web/dist ./web/dist

RUN PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    playwright install chromium

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn server.app.main:app --host 0.0.0.0 --port 8000"]

> **多实例部署注意（startup recovery leader）：** `GEO_RUN_STARTUP_RECOVERY`（默认 `true`）控制应用启动时是否执行卡住记录的恢复逻辑。多实例部署时，**只能有一个 web 实例开启此标志**（即保持默认 `true`），其余实例须显式设置 `GEO_RUN_STARTUP_RECOVERY=false`。原因与 `GEO_PIPELINE_SCHEDULER_ENABLED` 相同：若多个实例同时执行恢复，会把其他实例正在执行的 in-flight 记录误标为 `failed`。
>
> **定时智能体的权限语义（admin 跨租户）：** 定时调度（`GEO_PIPELINE_SCHEDULER_ENABLED`）会以**工作流属主**的身份无人值守地创建 run 并执行 distribute 节点。当属主是 **admin** 时，分发会沿用代码库既有的 "admin 全局权限" 语义——即**可分发任意用户的账号 / 分组**，绕过归属校验。这是有意为之的现状（非 bug），但请知悉：一个 admin 拥有的、启用了定时调度的工作流，会按 cron **跨租户**自动分发内容。若不希望如此，请勿用 admin 账号拥有定时分发类工作流，或后续改为按属主限定（详见整改计划 Task 15 方案 A）。
```

### 2.2 docker-compose.yml 优化

添加构建缓存策略：
```yaml
services:
  app:
    build:
      context: .
      cache_from:
        - type=registry,ref=localhost:5000/geo:latest
    image: localhost:5000/geo:latest
    # ...其他配置
```

### 2.3 健康检查优化

app 服务添加：
```yaml
  app:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/bootstrap"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 30s
    # ...
```

### 2.4 启动优化 - 并行初始化

创建 `entrypoint.sh`：
```bash
#!/bin/sh
set -e

echo "🔄 运行数据库迁移..."
alembic upgrade head

echo "🔄 创建初始用户（如设置了 GEO_SEED_USERS）..."
python -m server.scripts.seed_users || true

docker compose exec app python -m server.scripts.seed_users   # 服务器用这个


echo "✓ 启动 API 服务..."
exec uvicorn server.app.main:app --host 0.0.0.0 --port 8000
```

Dockerfile CMD 改为：
```dockerfile
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
CMD ["/app/entrypoint.sh"]
```

---

## 3. 开发快速部署（热重载）

### 仅重建 + 重启（保留数据）

```bash
# 方案 A：保留卷，只更新代码
cd ~/geo
docker compose down          # 不加 -v，保留数据
git pull
docker compose up --build -d

# 方案 B：仅后端热重载（开发模式）
docker compose down
docker compose -f docker-compose.dev.yml up -d

# 方案 C：部分重建（只 app，不重建 worker）
docker compose up --build -d app
```

### 3.1 docker-compose.dev.yml（开发专用）

```yaml
version: '3.8'

services:
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: GeoRoot20260513A1
      MYSQL_DATABASE: geo_collab
      MYSQL_USER: geo_user
      MYSQL_PASSWORD: GeoUser20260513A1
    ports:
      - "3306:3306"
    volumes:
      - mysql_data:/var/lib/mysql

  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      GEO_DB_HOST: mysql
      GEO_DB_PORT: 3306
      GEO_DB_USER: geo_user
      GEO_DB_PASS: GeoUser20260513A1
      GEO_DB_NAME: geo_collab
      GEO_DATA_DIR: /app/data
      GEO_JWT_SECRET: dev-secret-key
    volumes:
      - .:/app
      - app_data:/app/data
    command: >
      sh -c "alembic upgrade head &&
             python -m server.scripts.seed_users &&
             uvicorn server.app.main:app --host 0.0.0.0 --port 8000 --reload"
    depends_on:
      - mysql

volumes:
  mysql_data:
  app_data:
```

使用：
```bash
docker compose -f docker-compose.dev.yml up
```

---

## 4. 完整部署决策树

| 场景 | 命令 |
|------|------|
| **首次部署** | `~/deploy-fresh.sh` |
| **清空所有重建** | `docker compose down -v && docker compose up --build -d` |
| **代码更新（保留数据）** | `git pull && docker compose up --build -d` |
| **只重启（不重建）** | `docker compose restart` |
| **查看日志** | `docker compose logs -f app` |
| **进入容器** | `docker compose exec app bash` |
| **清理无用镜像** | `docker image prune -a` |
| **开发模式（热重载）** | `docker compose -f docker-compose.dev.yml up` |

---

## 5. 关键环境变量检查清单

```bash
# 检查 .env 是否完整
echo "检查必填项..."
grep -E "MYSQL_ROOT_PASSWORD|MYSQL_PASSWORD|GEO_JWT_SECRET|GEO_SEED_USERS" .env || echo "❌ 缺少必填变量"

# 验证数据库连接
docker compose exec -T app python -c "
from server.app.db.session import SessionLocal
try:
    db = SessionLocal()
    db.execute('SELECT 1')
    print('✓ 数据库连接成功')
except Exception as e:
    print(f'❌ 数据库连接失败: {e}')
finally:
    db.close()
"

# 检查初始用户
docker compose exec -T app python -c "
from server.app.db.session import SessionLocal
from server.app.modules.system.models import User
db = SessionLocal()
users = db.query(User).all()
print(f'✓ 用户数: {len(users)}')
for u in users:
    print(f'  - {u.username} ({u.role})')
db.close()
"
```

---

## 6. 故障排查

```bash
# 容器状态
docker compose ps

# 查看错误日志
docker compose logs app | tail -50

# 检查资源占用
docker stats

# 重建单个服务
docker compose up --build -d app

# 完全重置（核选项）
docker compose down -v
docker system prune -a --volumes
```

---

## 7. 数据库备份与恢复

### 7.1 手动备份（mysqldump）

创建备份目录（首次执行一次即可）：

```bash
mkdir -p ~/geo/backups
```

执行完整备份：

```bash
# 读取密码并备份（时间戳格式：YYYYMMDD_HHMMSS）
BACKUP_FILE=~/geo/backups/geo_collab_$(date +%Y%m%d_%H%M%S).sql.gz
docker compose -f ~/geo/docker-compose.yml exec -T mysql \
    mysqldump -u geo_user -p"${MYSQL_PASSWORD}" \
    --single-transaction --routines --triggers --no-tablespaces geo_collab \
    | gzip > "$BACKUP_FILE"
echo "✓ 备份完成：$BACKUP_FILE"
```

> **说明：**
> - `--single-transaction`：对 InnoDB 表做一致性快照，不锁表。
> - `--no-tablespaces`：跳过表空间元数据。MySQL 8.0+ 默认会 dump 表空间，需要全局 `PROCESS` 权限；业务账号 `geo_user` 一般没这个权限。对 InnoDB 恢复无影响。
> - `| gzip`：压缩输出，典型压缩率 80–90%。
> - 备份文件统一存放在 `~/geo/backups/`，建议定期同步到云存储或异机。

---

### 7.2 定期自动备份（cron）

仓库内已提供脚本 `scripts/backup_db.sh`（基于 7.1 的逻辑 + 旧备份自动清理 + gzip 完整性校验）。

赋予执行权限（首次部署一次即可）：

```bash
chmod +x ~/geo/scripts/backup_db.sh ~/geo/scripts/restore_db.sh
```

注册 cron 任务（每天凌晨 3:00 执行）：

```bash
crontab -e
```

在 crontab 中添加以下行：

```
# 每天 03:00 备份 geo_collab 数据库，保留最近 7 天
0 3 * * * MYSQL_PASSWORD=$(grep MYSQL_PASSWORD ~/geo/.env | cut -d= -f2) bash ~/geo/scripts/backup_db.sh >> ~/geo/backups/backup.log 2>&1
```

验证 cron 任务已注册：

```bash
crontab -l
```

> `>> backup.log 2>&1` 把 mysqldump/docker 的 stderr 也接进日志，否则连 MySQL 失败时只能去 `/var/mail/$USER` 找错误。
> 默认保留 7 天，可在调用前覆盖：`BACKUP_KEEP_DAYS=30 bash ~/geo/scripts/backup_db.sh`

---

### 7.3 从备份恢复

> **警告：** 恢复操作会覆盖数据库中的现有数据。`scripts/restore_db.sh` 会在恢复前自动做一份"恢复点"备份（`.pre-restore.sql.gz`）作为安全网。

**推荐流程（用仓库内的脚本）：**

```bash
cd ~/geo

# 1. 先看一眼有哪些备份
ls -lh backups/*.sql.gz

# 2. 选一份备份，跑恢复脚本（会要求二次确认）
MYSQL_PASSWORD=$(grep MYSQL_PASSWORD .env | cut -d= -f2) \
    bash scripts/restore_db.sh backups/geo_collab_<时间戳>.sql.gz

# 3. 如果备份来自旧版本，补跑迁移
docker compose exec app alembic upgrade head

# 4. 验证服务
curl http://127.0.0.1:8000/api/system/status
```

脚本会依次：(1) 校验 gzip 完整性 → (2) 备份当前状态作为恢复点 → (3) 停 app+worker → (4) 灌入备份 → (5) 重启 app+worker。

`--yes` 参数可以跳过交互确认（不推荐生产环境用）。

**注意事项：**
- 恢复前确认目标数据库 `geo_collab` 已存在（docker compose up 会自动创建）。
- 如果备份来自不同版本，恢复后需运行 `docker compose exec app alembic upgrade head` 补跑迁移。
- 生产环境恢复建议在维护窗口内操作，提前通知用户。
- 如果新恢复出问题，可以用脚本生成的 `.pre-restore.sql.gz` 回滚到恢复前的状态。

---

### 7.4 备份验证

**检查备份文件是否损坏：**

```bash
# 方法 A：验证 gzip 完整性（快速，推荐日常使用）
gzip -t ~/geo/backups/geo_collab_<时间戳>.sql.gz && echo "✓ 文件完整" || echo "❌ 文件损坏"

# 方法 B：检查 SQL 内容头尾（确认有效 SQL 结构）
zcat ~/geo/backups/geo_collab_<时间戳>.sql.gz | head -5
zcat ~/geo/backups/geo_collab_<时间戳>.sql.gz | tail -5

# 方法 C：统计备份文件行数（正常备份通常数千行以上）
zcat ~/geo/backups/geo_collab_<时间戳>.sql.gz | wc -l
```

**一次性验证所有备份文件：**

```bash
for f in ~/geo/backups/*.sql.gz; do
    gzip -t "$f" && echo "✓ OK: $f" || echo "❌ 损坏: $f"
done
```

**查看备份历史：**

```bash
ls -lh ~/geo/backups/*.sql.gz
cat ~/geo/backups/backup.log | tail -20
```

---

### 7.5 文件系统数据备份（assets + browser_states + MinIO）

MySQL 备份不包含三类同样重要的文件数据：

| 数据 | 位置 | 丢失后果 |
|---|---|---|
| 上传图片 | `app_data` volume → `/app/data/assets/` | 文章封面、内联图全部 404 |
| 浏览器登录态 | `app_data` volume → `/app/data/browser_states/` | 所有平台账号要重新登录 |
| 素材图库 | `minio_data` volume | Stock image gallery 全部丢失 |

仓库提供 `scripts/backup_files.sh` 备份这些 volume：

```bash
bash scripts/backup_files.sh
# 产物：
#   backups/app_data_20260528_030500.tar.gz   (assets + browser_states + exports)
#   backups/minio_data_20260528_030500.tar.gz (素材图库)
```

**实现方式**：通过 `docker run --rm busybox` 临时容器挂载 named volume，tar 后写到 `backups/`。不依赖宿主机能直接看到数据，也不需要停服务（用 `:ro` 挂载）。

**可调环境变量：**

| 变量 | 默认 | 作用 |
|---|---|---|
| `BACKUP_SKIP_MINIO=1` | 不跳过 | 跳过 minio，只备份 app_data |
| `BACKUP_SKIP_BROWSER_PROFILES=1` | 不跳过 | 跳过 `browser_states/*/*/profile/`，只保留登录态 `storage_state.json`。当 chromium profile 太大或发布期间可能被持有时建议开启 |
| `BACKUP_KEEP_DAYS=N` | 7 | 自动清理超过 N 天的旧备份 |
| `COMPOSE_PROJECT_NAME=xxx` | 仓库目录名 | 如果 compose 项目名和目录名不一致，必须设置 |

**完整的每日自动备份 cron（MySQL + 文件）**

```
# 每天 03:00 备份 MySQL
0 3 * * * MYSQL_PASSWORD=$(grep MYSQL_PASSWORD ~/geo/.env | cut -d= -f2) bash ~/geo/scripts/backup_db.sh >> ~/geo/backups/backup.log 2>&1

# 每天 03:30 备份文件数据（错开 30 分钟，避免 IO 高峰叠加）
30 3 * * * BACKUP_SKIP_BROWSER_PROFILES=1 bash ~/geo/scripts/backup_files.sh >> ~/geo/backups/backup.log 2>&1
```

> 建议生产环境**默认开启** `BACKUP_SKIP_BROWSER_PROFILES=1`：
> - chromium profile 占用大（每个账号 ~100-500MB）
> - 发布期间 Playwright 持有 profile 文件，tar 可能拿到不一致的快照
> - `storage_state.json` 是真正的"登录态"载体，profile 丢了大不了缓存重建
> 真要存 profile 时再手动跑一次无参数的脚本即可。

---

### 7.6 文件备份恢复

用 `scripts/restore_files.sh` 恢复：

```bash
cd ~/geo

# 看有哪些备份
ls -lh backups/*.tar.gz

# 恢复 app_data（脚本会自动停 app+worker）
bash scripts/restore_files.sh backups/app_data_20260528_030500.tar.gz

# 恢复 minio_data（脚本会自动停 app+worker+minio）
bash scripts/restore_files.sh backups/minio_data_20260528_030500.tar.gz
```

脚本会：
1. 校验 tar.gz 完整性
2. 把 volume 当前内容打包为 `.pre-restore-<时间戳>.tar.gz` 留底
3. 清空 volume 后解压备份
4. 重启依赖该 volume 的服务

恢复后如果出问题，用恢复点回滚：

```bash
bash scripts/restore_files.sh backups/app_data_*.pre-restore-*.tar.gz
```

---

### 7.7 备份的异地同步

**关键提醒**：以上备份都落在**同一台服务器**上。服务器整体挂掉（磁盘损坏、机房问题、勒索病毒）= 备份也丢。生产环境**必须**把 `backups/` 同步到异地存储。

常见方案：

```bash
# 用 rclone 同步到云存储（S3/OSS/B2 等）
# 安装：curl https://rclone.org/install.sh | sudo bash
# 配置：rclone config

# cron 加一行：每天 04:00 同步到云端（错开备份脚本结束时间）
0 4 * * * rclone copy ~/geo/backups/ remote:geo-backups/ --max-age 24h
```

或者直接 `scp` 到另一台机器：

```bash
0 4 * * * rsync -az --delete ~/geo/backups/ backup-server:/backups/geo/
```
