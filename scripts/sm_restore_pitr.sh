#!/bin/bash
# Session Memory 物理恢复（PITR）
#
# 用法:
#   sm_restore_pitr.sh                     # 恢复到最新可用 WAL
#   sm_restore_pitr.sh "2026-10-19 15:00:00"   # 恢复到指定时间点
#   sm_restore_pitr.sh --list              # 列出可用基础备份
#
# 原理: pg_basebackup 物理基础备份 + WAL 归档重放（PITR）
#   - 物理备份直接带已建好的索引（HNSW 等），无需重建 —— 这是相对逻辑备份的最大优势
#     实测：逻辑恢复 35 分钟（索引重建占 30 分钟），物理恢复 35 秒
#   - WAL 归档让可恢复的时间点精确到分钟
#
# ⚠️ 关键要点（都是实测踩过的坑）:
#   1. pg_basebackup 的 tar 【不包含】postgresql.auto.conf（本地配置，不算数据）
#      → 恢复时必须显式提供与主库一致的参数，否则报
#        "recovery aborted because of insufficient parameter settings
#         max_connections = 100 is a lower setting than on the primary server, where its value was 200"
#   2. docker run 的 -c 是 --cpu-shares，不是 PG 参数！
#      PG 参数必须放在【镜像名之后】作为 command
#   3. PGDATA 权限必须是 700 且属主为 postgres(UID 999)
#   4. 恢复用的参数（主库当前值，改动后需同步更新本脚本）:
#      max_connections=200 max_worker_processes=16 max_wal_senders=10 shared_buffers=2GB
set -uo pipefail

BASE_DIR=/home/jimwong/sm-basebackup
WAL_HOST_DIR=/home/jimwong/session-memory-backend/data/postgres/wal_archive
CONTAINER=pitr_restore
IMAGE=pgvector/pgvector:pg16
PGDATA_TMP=/tmp/pitr_restore_data
LOG=/home/jimwong/sm-restore.log

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# --- 参数（必须 >= 主库值，否则恢复被拒）---
PG_ARGS=(-c max_connections=200 -c max_worker_processes=16 -c max_wal_senders=10
         -c shared_buffers=2GB -c wal_level=replica)

if [ "${1:-}" = "--list" ]; then
    echo "可用基础备份:"
    ls -1dt "$BASE_DIR"/base_* 2>/dev/null | while read d; do
        printf "  %-50s %s  起始WAL=%s\n" "$(basename "$d")" \
            "$(du -sh "$d" 2>/dev/null | cut -f1)" "$(cat "$d/START_WAL" 2>/dev/null)"
    done
    exit 0
fi

# --- 选基础备份（默认最新）---
BASE=$(ls -1dt "$BASE_DIR"/base_* 2>/dev/null | head -1)
[ -z "$BASE" ] && { log "❌ 没有可用基础备份"; exit 1; }
log "使用基础备份: $BASE"

# --- 清理旧恢复环境 ---
docker rm -f "$CONTAINER" >/dev/null 2>&1
rm -rf "$PGDATA_TMP"; mkdir -p "$PGDATA_TMP"

log "解包中（$(du -sh "$BASE"/base.tar.gz 2>/dev/null | cut -f1)）..."
tar xzf "$BASE/base.tar.gz" -C "$PGDATA_TMP" 2>/dev/null
[ -f "$PGDATA_TMP/PG_VERSION" ] || { log "❌ 解包失败"; exit 1; }

# --- 写恢复配置 ---
{
    echo "restore_command = 'cp /wal_archive/%f %p'"
    echo "recovery_target_action = 'promote'"
    echo "recovery_target_timeline = 'latest'"
    [ -n "${1:-}" ] && echo "recovery_target_time = '$1'"
} > "$PGDATA_TMP/postgresql.auto.conf"
touch "$PGDATA_TMP/recovery.signal"
chown -R 999:999 "$PGDATA_TMP" 2>/dev/null
chmod 700 "$PGDATA_TMP"

log "启动恢复容器..."
docker run -d --name "$CONTAINER" -e POSTGRES_PASSWORD=postgres \
    -v "$PGDATA_TMP":/var/lib/postgresql/data \
    -v "$WAL_HOST_DIR":/wal_archive:ro \
    "$IMAGE" "${PG_ARGS[@]}" >/dev/null 2>&1

# --- 等待恢复完成 ---
for i in $(seq 1 60); do
    if docker logs "$CONTAINER" 2>&1 | grep -q "database system is ready to accept connections"; then
        log "✅ 恢复完成（$(docker logs "$CONTAINER" 2>&1 | grep 'redo done' | tail -1 | sed 's/.*system usage: //')）"
        break
    fi
    if docker logs "$CONTAINER" 2>&1 | grep -q "FATAL"; then
        log "❌ 恢复失败:"
        docker logs "$CONTAINER" 2>&1 | grep -A3 FATAL | tail -6
        exit 1
    fi
    sleep 5
done

# --- 校验 ---
log "数据校验："
docker exec "$CONTAINER" psql -U postgres -d session_memory -t -A -F'|' -c \
    "SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM sessions),
            (SELECT count(*) FROM kg_entities), (SELECT count(*) FROM kg_relations)" 2>/dev/null
log "索引有效性："
docker exec "$CONTAINER" psql -U postgres -d session_memory -t -A -c \
    "SELECT count(*) FILTER (WHERE i.indisvalid) || '/' || count(*) || ' 有效'
     FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE c.relname LIKE '%hnsw%'" 2>/dev/null
log "恢复容器 $CONTAINER 保持运行（连接端口 5432）；用完请 docker rm -f $CONTAINER"
exit 0
