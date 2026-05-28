#!/bin/bash
# Geo 协作平台 — MySQL 备份恢复脚本
#
# 用法：
#   bash scripts/restore_db.sh <备份文件路径>
#   bash scripts/restore_db.sh backups/geo_collab_20260528_030000.sql.gz
#
# 行为：
#   1. 校验备份文件存在且 gzip 完整
#   2. 停止 app/worker，避免业务写入与导入打架（mysql 容器保持运行）
#   3. 灌入备份
#   4. 重启 app/worker
#   5. 提示是否需要补跑 alembic upgrade head
#
# 安全措施：
#   - 二次确认 prompt（除非传 --yes）
#   - 恢复前自动做一次"恢复点"备份（命名带 .pre-restore 后缀）
#
# 依赖环境变量：
#   MYSQL_PASSWORD — 必须，从 .env 注入

set -euo pipefail

# ── 参数解析 ──
ASSUME_YES=false
BACKUP_FILE=""
for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=true ;;
        -*) echo "❌ 未知参数：$arg" >&2; exit 2 ;;
        *) BACKUP_FILE="$arg" ;;
    esac
done

if [[ -z "$BACKUP_FILE" ]]; then
    echo "用法：bash scripts/restore_db.sh <备份文件路径> [--yes]" >&2
    exit 2
fi

# ── 路径 ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/docker-compose.yml"
BACKUP_DIR="$REPO_ROOT/backups"
DB_NAME="${GEO_DB_NAME:-geo_collab}"
DB_USER="${GEO_DB_USER:-geo_user}"

# ── 前置检查 ──
if [[ -z "${MYSQL_PASSWORD:-}" ]]; then
    echo "❌ MYSQL_PASSWORD 未设置，无法连接 MySQL" >&2
    exit 1
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
    echo "❌ 找不到备份文件：$BACKUP_FILE" >&2
    exit 1
fi

if ! gzip -t "$BACKUP_FILE" 2>/dev/null; then
    echo "❌ 备份文件损坏（gzip 校验失败）：$BACKUP_FILE" >&2
    exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "❌ 找不到 docker-compose.yml：$COMPOSE_FILE" >&2
    exit 1
fi

# ── 显示信息 ──
SIZE="$(du -h "$BACKUP_FILE" | cut -f1)"
LINES="$(zcat "$BACKUP_FILE" | wc -l)"
echo "──────────────────────────────────────────────"
echo "  即将恢复："
echo "  备份文件：$BACKUP_FILE ($SIZE, $LINES 行 SQL)"
echo "  目标库  ：$DB_NAME @ docker compose mysql 服务"
echo "──────────────────────────────────────────────"
echo "⚠ 警告：恢复会覆盖当前数据库的所有数据。"
echo ""

# ── 二次确认 ──
if [[ "$ASSUME_YES" != "true" ]]; then
    read -r -p "确认继续吗？(yes/no) " REPLY
    if [[ "$REPLY" != "yes" ]]; then
        echo "已取消。"
        exit 0
    fi
fi

# ── 恢复前自动做"安全备份" ──
echo ""
echo "▶ 步骤 1/4：先备份当前状态（恢复点）"
mkdir -p "$BACKUP_DIR"
SAFETY_BACKUP="$BACKUP_DIR/${DB_NAME}_$(date +%Y%m%d_%H%M%S).pre-restore.sql.gz"
docker compose -f "$COMPOSE_FILE" exec -T mysql \
    mysqldump -u "$DB_USER" -p"$MYSQL_PASSWORD" \
    --single-transaction --routines --triggers "$DB_NAME" \
    | gzip > "$SAFETY_BACKUP"
echo "  ✓ 恢复点已存：$SAFETY_BACKUP"

# ── 停止业务服务 ──
echo ""
echo "▶ 步骤 2/4：停止 app 和 worker"
docker compose -f "$COMPOSE_FILE" stop app worker
echo "  ✓ 业务服务已停"

# ── 灌入备份 ──
echo ""
echo "▶ 步骤 3/4：灌入备份数据"
zcat "$BACKUP_FILE" | docker compose -f "$COMPOSE_FILE" exec -T mysql \
    mysql -u "$DB_USER" -p"$MYSQL_PASSWORD" "$DB_NAME"
echo "  ✓ 数据恢复完成"

# ── 重启业务 ──
echo ""
echo "▶ 步骤 4/4：重启 app 和 worker"
docker compose -f "$COMPOSE_FILE" start app worker
echo "  ✓ 业务服务已起"

echo ""
echo "──────────────────────────────────────────────"
echo "  ✅ 恢复完成"
echo "──────────────────────────────────────────────"
echo ""
echo "下一步建议："
echo "  1. 检查应用健康：curl http://127.0.0.1:8000/api/system/status"
echo "  2. 如果备份来自旧版本，跑：docker compose -f $COMPOSE_FILE exec app alembic upgrade head"
echo "  3. 恢复点备份保留在：$SAFETY_BACKUP（如果新恢复出问题可以回滚）"
