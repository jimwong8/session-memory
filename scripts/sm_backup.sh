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
    /home/jimwong/sm_notify.sh "❌ Session Memory 备份失败（pg_dump）" "pg_dump 失败于 $(date '+%F %T')，本地无新备份" 2>/dev/null || true
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

# --- WAL 归档异地推送（PITR 前提：基础备份在本地/异地，WAL 决定可回放的时间点）---
REMOTE_WAL_DIR="/SAS12T/backup/sm-wal"
if timeout 8 ssh -o BatchMode=yes -o ConnectTimeout=5 root@10.100.1.254 true 2>/dev/null; then
    _wal_new=$(docker exec session_memory_postgres sh -c \
        "ls -1 /var/lib/postgresql/data/wal_archive/ 2>/dev/null | grep -c '^0000'" 2>/dev/null | tr -d '\r\n')
    if [ "${_wal_new:-0}" -gt 0 ]; then
        echo "[$(date +%H:%M:%S)] 推送 WAL 归档（${_wal_new} 个文件）到异地"
        docker exec session_memory_postgres sh -c \
            "cd /var/lib/postgresql/data/wal_archive && tar cf - \$(ls -1 | grep '^0000')" 2>/dev/null \
            | ssh -o BatchMode=yes -o ConnectTimeout=10 root@10.100.1.254 \
              "mkdir -p ${REMOTE_WAL_DIR} && tar xf - -C ${REMOTE_WAL_DIR} && ls -t ${REMOTE_WAL_DIR} | tail -n +1000 | xargs -r rm -f" 2>/dev/null \
            && echo "[$(date +%H:%M:%S)] ✅ WAL 归档异地推送完成" \
            || echo "[$(date +%H:%M:%S)] ⚠️ WAL 归档异地推送失败"
    fi
fi

# --- 异地副本：推送到 PVE tw (10.100.1.254) 的 SAS12T/backup/sm-db ---
# 13 号机是 QEMU VM，宿主（疑似 PVE253，当前不可达）故障会连坐；
# 254 是独立物理机 + PBS 宿主，构成真正的异地副本。
REMOTE_HOST="root@10.100.1.254"
REMOTE_DIR="/SAS12T/backup/sm-db"
if command -v scp >/dev/null 2>&1 && timeout 8 ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_HOST" true 2>/dev/null; then
    echo "[$(date +%H:%M:%S)] 推送异地副本到 ${REMOTE_HOST}:${REMOTE_DIR}"
    if scp -q -o ConnectTimeout=10 "$OUT" "${REMOTE_HOST}:${REMOTE_DIR}/" 2>>/tmp/sm_backup_remote.log; then
        echo "[$(date +%H:%M:%S)] ✅ 异地推送成功"
        # 远端保留 7 份（本地也是 7 天）
        ssh -o BatchMode=yes -o ConnectTimeout=8 "$REMOTE_HOST" \
            "ls -t ${REMOTE_DIR}/sm_*.dump 2>/dev/null | tail -n +8 | xargs -r rm -f" 2>/dev/null
    else
        echo "[$(date +%H:%M:%S)] ⚠️ 异地推送失败（本地备份仍有效）"
        /home/jimwong/sm_notify.sh "⚠️ Session Memory 异地备份推送失败" "推送 ${REMOTE_HOST}:${REMOTE_DIR} 失败于 $(date '+%F %T')，本地备份仍有效" 2>/dev/null || true
    fi
else
    echo "[$(date +%H:%M:%S)] ⚠️ 跳过异地推送（${REMOTE_HOST} 不可达）"
fi

# 磁盘水位保护：备份目录 > 45G 或磁盘使用率 >= 85% 时删最旧的
TOTAL=$(du -sm "$BACKUP_DIR" | cut -f1)
DISK_PCT=$(df -P "$BACKUP_DIR" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
echo "[$(date +%H:%M:%S)] 备份目录 ${TOTAL}MB, 磁盘使用 ${DISK_PCT}%"
if [ "${TOTAL:-0}" -gt 45000 ] || [ "${DISK_PCT:-0}" -ge 85 ]; then
    echo "[$(date +%H:%M:%S)] 触发水位保护（目录 ${TOTAL}MB / 磁盘 ${DISK_PCT}%），删除最旧备份"
    ls -t "$BACKUP_DIR"/sm_*.dump 2>/dev/null | tail -n +2 | xargs -r rm -f
fi

exit 0
