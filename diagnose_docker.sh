#!/bin/bash
# 诊断 Docker 上传问题

echo "=== Docker 容器状态 ==="
docker-compose ps

echo -e "\n=== App 容器磁盘空间 ==="
docker-compose exec app df -h /app/data

echo -e "\n=== App 容器日志（最后30行）==="
docker-compose logs --tail=30 app

echo -e "\n=== 数据目录文件数 ==="
docker-compose exec app sh -c "find /app/data -type f | wc -l"

echo -e "\n=== 临时文件（可能是卡住的上传）==="
docker-compose exec app ls -lah /app/data/.upload_tmp_* 2>/dev/null || echo "无临时文件"

echo -e "\n=== 容器资源使用情况 ==="
docker stats --no-stream --format "table {{.Container}}\t{{.MemUsage}}\t{{.CPUPerc}}"
