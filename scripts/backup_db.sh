#!/bin/bash
# Geo 协作平台 — MySQL 每日备份脚本
#
# 用法：
#   bash scripts/backup_db.sh                  # 手动执行
#   crontab 中调度：见仓库根 DEPLOYMENT.md §7.2
#
# 行为：
#   1. 用 mysqldump 把 geo_collab 库一致性快照导出（不锁表）
#   2. gzip 压缩，文件名带时间戳
#   3. 写日志到 backups/backup.log
#   4. 删除超过 KEEP_DAYS 天的旧备份
#
# 依赖环境变量：
#   MYSQL_PASSWORD — 必须，从 .env 注入（cron 行里用 grep 读 .env）

set -euo pipefail

# ── 路径：相对于脚本自身位置 ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BACKUP_DIR="$REPO_ROOT/backups"
LOG_FILE="$BACKUP_DIR/backup.log"
COMPOSE_FILE="$REPO_ROOT/docker-compose.yml"
DB_NAME="${GEO_DB_NAME:-geo_collab}"
DB_USER="${GEO_DB_USER:-geo_user}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"

# ── 前置检查 ──
if [[ -z "${MYSQL_PASSWORD:-}" ]]; then
    echo "❌ MYSQL_PASSWORD 未设置，无法连接 MySQL" >&2
    exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "❌ 找不到 docker-compose.yml：$COMPOSE_FILE" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.sql.gz"

# ── 执行备份 ──
# --single-transaction: InnoDB 一致性快照，不锁表
# --routines --triggers: 顺带导出存储过程和触发器
# --no-tablespaces:      跳过表空间元数据（业务账号没有 PROCESS 权限；
#                        对 InnoDB 恢复无影响，MySQL 自动重建）
docker compose -f "$COMPOSE_FILE" exec -T mysql \
    mysqldump -u "$DB_USER" -p"$MYSQL_PASSWORD" \
    --single-transaction --routines --triggers --no-tablespaces "$DB_NAME" \
    | gzip > "$BACKUP_FILE"

# ── 验证产物 ──
if ! gzip -t "$BACKUP_FILE" 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ❌ 备份文件损坏：$BACKUP_FILE" >> "$LOG_FILE"
    rm -f "$BACKUP_FILE"
    exit 1
fi

SIZE="$(du -h "$BACKUP_FILE" | cut -f1)"
echo "$(date '+%Y-%m-%d %H:%M:%S') ✓ 备份完成：$BACKUP_FILE ($SIZE)" >> "$LOG_FILE"

# ── 清理过期备份 ──
DELETED=$(find "$BACKUP_DIR" -name "${DB_NAME}_*.sql.gz" -mtime "+${KEEP_DAYS}" -print -delete | wc -l)
echo "$(date '+%Y-%m-%d %H:%M:%S') ✓ 清理 ${DELETED} 个超过 ${KEEP_DAYS} 天的旧备份" >> "$LOG_FILE"
