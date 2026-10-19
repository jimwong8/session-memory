#!/bin/bash
# Session Memory 物理基础备份（pg_basebackup）+ WAL 归档管理
#
# 为什么需要物理备份：
#   逻辑备份（pg_dump）恢复时必须重建所有索引，实测两个 HNSW 索引
#   （messages 上 555MB + 1778MB）构建耗时 20-30 分钟，是恢复的绝对瓶颈。
#   物理备份直接复制已建好的数据目录，恢复只需复制文件 + 重放 WAL。
#
# 配套：postgresql.auto.conf 已开
#   archive_mode=on
#   archive_command='test ! -f <datadir>/wal_archive/%f && cp %p <datadir>/wal_archive/%f'
#   archive_timeout=300
#
# 产出：
#   /home/jimwong/sm-basebackup/<TS>/base.tar.gz   物理基础备份
#   <datadir>/wal_archive/                        WAL 归档（持续累积）
#
# 保留：物理备份 3 份，WAL 归档 7 天

set -uo pipefail

PG_CONTAINER=session_memory_postgres
BKP_DIR=/home/jimwong/sm-basebackup
WAL_DIR_IN_CONTAINER=/var/lib/postgresql/data/wal_archive
KEEP_BASE=3
KEEP_WAL_DAYS=7
LOG=/home/jimwong/sm-basebackup.log

mkdir -p "$BKP_DIR"
TS=$(date +%Y%m%d_%H%M%S)
OUT="$BKP_DIR/base_${TS}"
mkdir -p "$OUT"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

log "=== 物理基础备份开始 → $OUT ==="

# 1) 强制一次 WAL 切换，确保 base backup 自包含一个干净的检查点
docker exec "$PG_CONTAINER" psql -U postgres -d postgres -c "SELECT pg_switch_wal()" >/dev/null 2>&1
sleep 3

# 2) pg_basebackup（tar 格式，便于直接解包恢复）
if docker exec "$PG_CONTAINER" pg_basebackup \
      -U postgres -D /tmp/base_backup -Ft -z -X stream -c fast >/dev/null 2>&1; then
    log "  pg_basebackup 完成"
else
    log "  ❌ pg_basebackup 失败"
    /home/jimwong/sm_notify.sh "❌ Session Memory 物理备份失败" "pg_basebackup 失败于 $(date '+%F %T')" 2>/dev/null || true
    rm -rf "$OUT"
    exit 1
fi

# 3) 拷出
if docker cp "$PG_CONTAINER":/tmp/base_backup/base.tar.gz "$OUT/base.tar.gz" 2>/dev/null; then
    SIZE=$(du -h "$OUT/base.tar.gz" | cut -f1)
    log "  ✅ 已拷出 base.tar.gz ($SIZE)"
else
    log "  ❌ 拷出失败"
    /home/jimwong/sm_notify.sh "❌ Session Memory 物理备份拷出失败" "docker cp 失败于 $(date '+%F %T')" 2>/dev/null || true
    rm -rf "$OUT"
    exit 1
fi
docker exec "$PG_CONTAINER" rm -rf /tmp/base_backup

# 4) 记录 base backup 起始 WAL（恢复时需要从这里开始重放）
START_WAL=$(docker exec "$PG_CONTAINER" psql -U postgres -d postgres -t -A -c \
    "SELECT pg_walfile_name(pg_current_wal_lsn())" 2>/dev/null | tr -d '\r\n')
echo "$START_WAL" > "$OUT/START_WAL"
log "  起始 WAL: $START_WAL"

# 5) WAL 归档保留策略（超过 7 天的删掉）
DELETED=$(docker exec "$PG_CONTAINER" sh -c \
    "find $WAL_DIR_IN_CONTAINER -name '0000*' -type f -mtime +$KEEP_WAL_DAYS -delete 2>/dev/null | wc -l" 2>/dev/null | tr -d '\r\n')
WAL_N=$(docker exec "$PG_CONTAINER" sh -c "ls -1 $WAL_DIR_IN_CONTAINER 2>/dev/null | wc -l" 2>/dev/null | tr -d '\r\n')
WAL_SIZE=$(docker exec "$PG_CONTAINER" du -sh $WAL_DIR_IN_CONTAINER 2>/dev/null | awk '{print $1}')
log "  WAL 归档: ${WAL_N} 个文件, ${WAL_SIZE}, 清理 ${DELETED} 个过期"

# 6) 保留最近 3 份物理备份
ls -1dt "$BKP_DIR"/base_* 2>/dev/null | tail -n +$((KEEP_BASE + 1)) | xargs -r rm -rf
log "  物理备份保留 $KEEP_BASE 份"

# 7) 异地推送（与逻辑备份同一目的地）
REMOTE_HOST="root@10.100.1.254"
REMOTE_DIR="/SAS12T/backup/sm-base"
if timeout 8 ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_HOST" true 2>/dev/null; then
    log "  推送异地副本..."
    if scp -q -o ConnectTimeout=10 "$OUT/base.tar.gz" "${REMOTE_HOST}:${REMOTE_DIR}/" 2>>/tmp/sm_base_remote.log; then
        ssh -o BatchMode=yes -o ConnectTimeout=8 "$REMOTE_HOST" \
            "echo '$START_WAL' > ${REMOTE_DIR}/START_WAL.latest; ls -t ${REMOTE_DIR}/base_*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f" 2>/dev/null
        log "  ✅ 异地推送成功"
    else
        log "  ⚠️ 异地推送失败（本地仍有效）"
        /home/jimwong/sm_notify.sh "⚠️ Session Memory 物理备份异地推送失败" "推送 ${REMOTE_HOST} 失败于 $(date '+%F %T')" 2>/dev/null || true
    fi
else
    log "  ⚠️ 跳过异地推送（${REMOTE_HOST} 不可达）"
fi

log "=== 物理备份完成 ==="
exit 0
