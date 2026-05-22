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
from server.app.models import User
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
    --single-transaction --routines --triggers geo_collab \
    | gzip > "$BACKUP_FILE"
echo "✓ 备份完成：$BACKUP_FILE"
```

> **说明：**
> - `--single-transaction`：对 InnoDB 表做一致性快照，不锁表。
> - `| gzip`：压缩输出，典型压缩率 80–90%。
> - 备份文件统一存放在 `~/geo/backups/`，建议定期同步到云存储或异机。

---

### 7.2 定期自动备份（cron）

创建备份脚本 `~/geo/scripts/backup_db.sh`：

```bash
#!/bin/bash
set -e

BACKUP_DIR=~/geo/backups
LOG_FILE=~/geo/backups/backup.log
COMPOSE_FILE=~/geo/docker-compose.yml
DB_NAME=geo_collab
DB_USER=geo_user
KEEP_DAYS=7

mkdir -p "$BACKUP_DIR"

BACKUP_FILE="$BACKUP_DIR/${DB_NAME}_$(date +%Y%m%d_%H%M%S).sql.gz"

# 执行备份
docker compose -f "$COMPOSE_FILE" exec -T mysql \
    mysqldump -u "$DB_USER" -p"${MYSQL_PASSWORD}" \
    --single-transaction --routines --triggers "$DB_NAME" \
    | gzip > "$BACKUP_FILE"

echo "$(date '+%Y-%m-%d %H:%M:%S') ✓ 备份完成：$BACKUP_FILE" >> "$LOG_FILE"

# 清理超过 KEEP_DAYS 天的旧备份
find "$BACKUP_DIR" -name "${DB_NAME}_*.sql.gz" -mtime +${KEEP_DAYS} -delete
echo "$(date '+%Y-%m-%d %H:%M:%S') ✓ 已清理 ${KEEP_DAYS} 天前的旧备份" >> "$LOG_FILE"
```

赋予执行权限：

```bash
chmod +x ~/geo/scripts/backup_db.sh
```

注册 cron 任务（每天凌晨 3:00 执行）：

```bash
# 编辑当前用户的 crontab
crontab -e
```

在 crontab 中添加以下行：

```
# 每天 03:00 备份 geo_collab 数据库，保留最近 7 天
0 3 * * * MYSQL_PASSWORD=$(grep MYSQL_PASSWORD ~/geo/.env | cut -d= -f2) ~/geo/scripts/backup_db.sh >> ~/geo/backups/backup.log 2>&1
```

验证 cron 任务已注册：

```bash
crontab -l
```

---

### 7.3 从备份恢复

> **警告：** 恢复操作会覆盖数据库中的现有数据，请务必先确认备份文件完整性（见 7.4）。

**完整恢复流程：**

```bash
# 1. 停止应用服务（保留 mysql 容器运行）
cd ~/geo
docker compose stop app worker

# 2. 确认要恢复的备份文件
ls -lh ~/geo/backups/*.sql.gz

# 3. 执行恢复（替换 <备份文件名> 为实际文件路径）
BACKUP_FILE=~/geo/backups/<备份文件名>.sql.gz
zcat "$BACKUP_FILE" | docker compose exec -T mysql \
    mysql -u geo_user -p"${MYSQL_PASSWORD}" geo_collab

echo "✓ 数据库恢复完成"

# 4. 重新启动应用服务
docker compose start app worker
docker compose ps
```

**注意事项：**
- 恢复前确认目标数据库 `geo_collab` 已存在（docker compose up 会自动创建）。
- 如果备份来自不同版本，恢复后需运行 `docker compose exec app alembic upgrade head` 补跑迁移。
- 生产环境恢复建议在维护窗口内操作，提前通知用户。

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
