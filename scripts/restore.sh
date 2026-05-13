#!/usr/bin/env bash
set -Eeuo pipefail

if [ $# -lt 1 ]; then
  echo "用法: ./restore.sh <备份时间戳前缀>"
  echo "示例: ./restore.sh 20260423_024500"
  exit 1
fi

PROJECT_DIR="/home/jimwong/session-memory"
BACKUP_DIR="$PROJECT_DIR/backups"
DOCKER_BIN="/usr/bin/docker"
POSTGRES_CONTAINER="session_memory_postgres"
REDIS_CONTAINER="session_memory_redis"
DB_NAME="session_memory"
DB_USER="postgres"
PREFIX="$1"
POSTGRES_BACKUP="$BACKUP_DIR/postgres_${PREFIX}.sql.gz"
REDIS_BACKUP="$BACKUP_DIR/redis_${PREFIX}.rdb"

echo "[restore $(date +"%F %T")] 开始恢复"
$DOCKER_BIN ps --format "{{.Names}}" | grep -qx "$POSTGRES_CONTAINER"
$DOCKER_BIN ps --format "{{.Names}}" | grep -qx "$REDIS_CONTAINER"

if [ -f "$POSTGRES_BACKUP" ]; then
  echo "恢复 PostgreSQL: $POSTGRES_BACKUP"
  gunzip -c "$POSTGRES_BACKUP" | $DOCKER_BIN exec -i "$POSTGRES_CONTAINER" psql -U "$DB_USER" "$DB_NAME"
else
  echo "PostgreSQL 备份文件不存在: $POSTGRES_BACKUP" >&2
fi

if [ -f "$REDIS_BACKUP" ]; then
  echo "恢复 Redis: $REDIS_BACKUP"
  $DOCKER_BIN cp "$REDIS_BACKUP" "$REDIS_CONTAINER:/data/dump.rdb"
  $DOCKER_BIN restart "$REDIS_CONTAINER" >/dev/null
else
  echo "Redis 备份文件不存在: $REDIS_BACKUP" >&2
fi

echo "[restore $(date +"%F %T")] 恢复完成"
