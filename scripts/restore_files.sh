#!/bin/bash
# Geo 协作平台 — 文件系统数据恢复脚本
#
# 用法：
#   bash scripts/restore_files.sh <app_data 备份> [--yes]
#   bash scripts/restore_files.sh <minio_data 备份> [--yes]
#
# 例：
#   bash scripts/restore_files.sh backups/app_data_20260528_030000.tar.gz
#   bash scripts/restore_files.sh backups/minio_data_20260528_030000.tar.gz
#
# 行为：
#   - 自动识别备份类型（app_data 或 minio_data）
#   - 校验 tar.gz 完整性
#   - 停止依赖该 volume 的服务（app_data → 停 app+worker；minio_data → 停 app+worker+minio）
#   - 在 volume 内做"恢复点"快照（重命名现有内容为 .pre-restore-<时间戳>）
#   - 解压备份到 volume
#   - 重启服务

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
    echo "用法：bash scripts/restore_files.sh <备份文件> [--yes]" >&2
    exit 2
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
    echo "❌ 找不到备份文件：$BACKUP_FILE" >&2
    exit 1
fi

if ! gzip -t "$BACKUP_FILE" 2>/dev/null; then
    echo "❌ 备份文件损坏（gzip 校验失败）：$BACKUP_FILE" >&2
    exit 1
fi

# ── 路径 ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/docker-compose.yml"
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$REPO_ROOT")}"

# ── 识别备份类型 ──
BASE_NAME="$(basename "$BACKUP_FILE")"
case "$BASE_NAME" in
    app_data_*.tar.gz)
        VOLUME="${PROJECT_NAME}_app_data"
        STOP_SERVICES=(app worker)
        ;;
    minio_data_*.tar.gz)
        VOLUME="${PROJECT_NAME}_minio_data"
        STOP_SERVICES=(app worker minio)
        ;;
    *)
        echo "❌ 无法从文件名识别备份类型：$BASE_NAME" >&2
        echo "   文件名需以 app_data_ 或 minio_data_ 开头" >&2
        exit 1
        ;;
esac

if ! docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    echo "❌ Docker volume '$VOLUME' 不存在" >&2
    exit 1
fi

# ── 显示信息 ──
SIZE="$(du -h "$BACKUP_FILE" | cut -f1)"
echo "──────────────────────────────────────────────"
echo "  即将恢复："
echo "  备份文件：$BACKUP_FILE ($SIZE)"
echo "  目标 volume：$VOLUME"
echo "  将停止的服务：${STOP_SERVICES[*]}"
echo "──────────────────────────────────────────────"
echo "⚠ 警告：volume 当前的内容会被重命名（保留为恢复点），然后用备份覆盖。"
echo ""

if [[ "$ASSUME_YES" != "true" ]]; then
    read -r -p "确认继续吗？(yes/no) " REPLY
    if [[ "$REPLY" != "yes" ]]; then
        echo "已取消。"
        exit 0
    fi
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

# ── 停服务 ──
echo ""
echo "▶ 步骤 1/4：停止服务 ${STOP_SERVICES[*]}"
docker compose -f "$COMPOSE_FILE" stop "${STOP_SERVICES[@]}"
echo "  ✓ 服务已停"

# ── 恢复点：把 volume 内现有内容打个 tar 留底 ──
echo ""
echo "▶ 步骤 2/4：生成恢复点（当前 volume 内容打包留底）"
SAFETY_TAR="$REPO_ROOT/backups/${BASE_NAME%.tar.gz}.pre-restore-${TIMESTAMP}.tar.gz"
docker run --rm \
    -v "${VOLUME}:/source:ro" \
    -v "$REPO_ROOT/backups:/backup" \
    busybox \
    tar -czf "/backup/$(basename "$SAFETY_TAR")" -C /source .
echo "  ✓ 恢复点已存：$SAFETY_TAR"

# ── 清空 volume 并解压备份 ──
echo ""
echo "▶ 步骤 3/4：清空 volume 并解压备份"
docker run --rm \
    -v "${VOLUME}:/target" \
    -v "$REPO_ROOT/backups:/backup:ro" \
    busybox \
    sh -c "find /target -mindepth 1 -delete && tar -xzf /backup/$BASE_NAME -C /target"
echo "  ✓ 数据已恢复"

# ── 重启服务 ──
echo ""
echo "▶ 步骤 4/4：重启服务"
docker compose -f "$COMPOSE_FILE" start "${STOP_SERVICES[@]}"
echo "  ✓ 服务已起"

echo ""
echo "──────────────────────────────────────────────"
echo "  ✅ 恢复完成"
echo "──────────────────────────────────────────────"
echo ""
echo "下一步建议："
echo "  1. 检查应用健康：curl http://127.0.0.1:8000/api/system/status"
echo "  2. 如恢复出问题，可用恢复点回滚：bash scripts/restore_files.sh $SAFETY_TAR"
