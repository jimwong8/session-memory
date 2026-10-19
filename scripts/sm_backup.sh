#!/bin/bash
# Session Memory PostgreSQL 每日备份（压缩格式，保留 14 天）
set -uo pipefail

BACKUP_DIR=/home/jimwong/sm-backup
PG_CONTAINER=session_memory_postgres
DB_NAME=session_memory
PG_USER=postgres
STAMP=$(date +%Y%m%d_%H%M%S)
OUT="${BACKUP_DIR}/sm_${STAMP}.dump"

mkdir -p "$BACKUP_DIR"

echo "[$(date +%H:%M:%S)] 开始备份 ${DB_NAME} → ${OUT}"

# 容器内 dump（避免 pg_dump 版本不匹配：必须用容器内自带的 pg_dump）
if ! docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$DB_NAME" -Fc -f /tmp/sm_backup.dump; then
    echo "[$(date +%H:%M:%S)] ❌ pg_dump 失败"
    docker exec "$PG_CONTAINER" rm -f /tmp/sm_backup.dump
    exit 1
fi

# 拷出并命名
if ! docker cp "$PG_CONTAINER":/tmp/sm_backup.dump "$OUT"; then
    echo "[$(date +%H:%M:%S)] ❌ docker cp 失败"
    docker exec "$PG_CONTAINER" rm -f /tmp/sm_backup.dump
    exit 1
fi

docker exec "$PG_CONTAINER" rm -f /tmp/sm_backup.dump

SIZE=$(du -h "$OUT" | cut -f1)

# 校验：pg_restore --list 能读出目录才算有效
if docker exec -i "$PG_CONTAINER" pg_restore --list < "$OUT" > /dev/null 2>&1; then
    echo "[$(date +%H:%M:%S)] ✅ 备份成功 ${OUT} (${SIZE})，校验通过"
else
    echo "[$(date +%H:%M:%S)] ⚠️ 备份已生成但校验失败 ${OUT} (${SIZE})"
fi

# 保留 7 天（单份 dump 约 4.2G；2026-10-19 扩容后根分区 98G -> 197G，可用 123G，
# 7 份约 30G，安全）。扩容前是 3 天。
find "$BACKUP_DIR" -name 'sm_*.dump' -mtime +7 -delete 2>/dev/null

# 磁盘水位保护：备份目录 > 45G 或磁盘使用率 >= 85% 时删最旧的
TOTAL=$(du -sm "$BACKUP_DIR" | cut -f1)
DISK_PCT=$(df -P "$BACKUP_DIR" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
echo "[$(date +%H:%M:%S)] 备份目录 ${TOTAL}MB, 磁盘使用 ${DISK_PCT}%"
if [ "${TOTAL:-0}" -gt 45000 ] || [ "${DISK_PCT:-0}" -ge 85 ]; then
    echo "[$(date +%H:%M:%S)] 触发水位保护（目录 ${TOTAL}MB / 磁盘 ${DISK_PCT}%），删除最旧备份"
    ls -t "$BACKUP_DIR"/sm_*.dump 2>/dev/null | tail -n +2 | xargs -r rm -f
fi

exit 0
