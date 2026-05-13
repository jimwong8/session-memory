#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="/home/jimwong/session-memory"
BACKUP_DIR="$PROJECT_DIR/backups"
LOG_PREFIX="[backup $(date +"%F %T")]"
DOCKER_BIN="/usr/bin/docker"
POSTGRES_CONTAINER="session_memory_postgres"
REDIS_CONTAINER="session_memory_redis"
DB_NAME="session_memory"
DB_USER="postgres"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
POSTGRES_BACKUP="$BACKUP_DIR/postgres_${TIMESTAMP}.sql.gz"
REDIS_BACKUP="$BACKUP_DIR/redis_${TIMESTAMP}.rdb"

mkdir -p "$BACKUP_DIR"
[ -n "$BACKUP_DIR" ] && [ "$BACKUP_DIR" != "/" ]
$DOCKER_BIN ps --format "{{.Names}}" | grep -qx "$POSTGRES_CONTAINER"
$DOCKER_BIN ps --format "{{.Names}}" | grep -qx "$REDIS_CONTAINER"

echo "$LOG_PREFIX 开始备份到 $BACKUP_DIR"
$DOCKER_BIN exec "$POSTGRES_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$POSTGRES_BACKUP"
echo "$LOG_PREFIX PostgreSQL 备份完成: $POSTGRES_BACKUP"
$DOCKER_BIN exec "$REDIS_CONTAINER" redis-cli SAVE >/dev/null
$DOCKER_BIN cp "$REDIS_CONTAINER:/data/dump.rdb" "$REDIS_BACKUP"
echo "$LOG_PREFIX Redis 备份完成: $REDIS_BACKUP"
find "$BACKUP_DIR" -name postgres_*.sql.gz -mtime +30 -delete
find "$BACKUP_DIR" -name redis_*.rdb -mtime +30 -delete
echo "$LOG_PREFIX 旧备份清理完成"
echo "$LOG_PREFIX 备份完成"
