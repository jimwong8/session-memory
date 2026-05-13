#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="/home/jimwong/session-memory"
BACKUP_SCRIPT="$PROJECT_DIR/scripts/backup.sh"
AUDIT_SCRIPT="$PROJECT_DIR/scripts/run_audits.sh"
KG_BACKFILL_SCRIPT="$PROJECT_DIR/scripts/adaptive_kg_jobs_backfill.sh"
BACKLOG_SUPERVISOR="$PROJECT_DIR/scripts/supervise_kg_jobs_backlog.sh"
BACKUP_LOG="$PROJECT_DIR/logs/backup.log"
AUDIT_LOG="$PROJECT_DIR/logs/audits/cron.log"
KG_LOG="$PROJECT_DIR/logs/kg_backfill.log"
SUP_LOG="$PROJECT_DIR/logs/backlog_supervisor.log"
mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/logs/audits"

CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
FILTERED="$(printf "%s\n" "$CURRENT_CRON" | grep -v '/scripts/run_kg_once.sh' | grep -v '/scripts/adaptive_kg_backfill.sh' | grep -v '/scripts/supervise_backlog.sh' | grep -v "$BACKUP_SCRIPT" | grep -v "$AUDIT_SCRIPT" | grep -v "$KG_BACKFILL_SCRIPT" | grep -v "$BACKLOG_SUPERVISOR" || true)"
{
  printf "%s\n" "$FILTERED"
  echo "0 2 * * * cd $PROJECT_DIR && /usr/bin/env bash $BACKUP_SCRIPT >> $BACKUP_LOG 2>&1"
  echo "10 * * * * cd $PROJECT_DIR && /usr/bin/env bash $BACKLOG_SUPERVISOR >> $SUP_LOG 2>&1"
  echo "15 * * * * cd $PROJECT_DIR && OPENCODE_MONITOR_MODE=ssh OPENCODE_MONITOR_HOST=10.100.1.18 OPENCODE_MONITOR_USER=jimwong OPENCODE_MONITOR_PASSWORD=ok0115ok /usr/bin/env bash $AUDIT_SCRIPT >> $AUDIT_LOG 2>&1"
  echo "*/10 * * * * cd $PROJECT_DIR && SUDO_PASSWORD=ok0115ok /usr/bin/env bash $KG_BACKFILL_SCRIPT >> $KG_LOG 2>&1"
} | crontab -

echo "已安装 session-memory 定时任务:"
crontab -l | grep -E "$BACKUP_SCRIPT|$AUDIT_SCRIPT|adaptive_kg_jobs_backfill.sh|supervise_kg_jobs_backlog.sh"
