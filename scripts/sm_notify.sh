#!/bin/bash
# Session Memory 告警通知 → Telegram
#
# 用法: sm_notify.sh "<标题>" "<正文>"
# 凭据来自 /home/jimwong/.sm-alert/telegram.env（权限 600，永不回显）
#
# 设计要点:
#   - 失败不阻塞调用方（退出码永远 0），告警通道本身不能成为故障源
#   - 相同标题 1 小时内去重，避免告警风暴
#   - 静默模式下（SM_NOTIFY_SILENT=1）只记日志不发消息（用于演练/压测）

ENV_FILE=/home/jimwong/.sm-alert/telegram.env
DEDUP_DIR=/home/jimwong/.sm-alert/dedup
LOG=/home/jimwong/.sm-alert/notify.log

TITLE="${1:-Session Memory 告警}"
BODY="${2:-}"

mkdir -p "$DEDUP_DIR"
touch "$LOG"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 去重：同标题 1 小时内只发一次
KEY=$(echo "$TITLE" | md5sum | cut -c1-16)
MARK="$DEDUP_DIR/$KEY"
if [ -f "$MARK" ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y "$MARK") ))
    if [ "$AGE" -lt 3600 ]; then
        log "重复告警已抑制: $TITLE (${AGE}s 前已发)"
        exit 0
    fi
fi
touch "$MARK"

if [ "${SM_NOTIFY_SILENT:-0}" = "1" ]; then
    log "[SILENT] $TITLE | $BODY"
    exit 0
fi

if [ ! -r "$ENV_FILE" ]; then
    log "凭据文件不可读: $ENV_FILE"
    exit 0
fi
# shellcheck disable=SC1090
. "$ENV_FILE"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    log "凭据缺失（token 或 chat_id 为空）"
    exit 0
fi

# Telegram 单条消息上限 4096 字符
MSG="$TITLE"
[ -n "$BODY" ] && MSG="$TITLE

$BODY"
MSG=$(printf '%s' "$MSG" | head -c 3900)

RESP=$(curl -s --max-time 20 -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H 'Content-Type: application/json' \
    -d "$(printf '%s' "$MSG" | python3 -c 'import json,sys; print(json.dumps({"chat_id": sys.argv[1], "text": sys.stdin.read(), "disable_notification": True}))' "$TELEGRAM_CHAT_ID")" 2>&1)

if echo "$RESP" | grep -q '"ok":true'; then
    log "已发送: $TITLE"
else
    # 只记录状态，不记录 token
    log "发送失败: $TITLE | $(printf '%s' "$RESP" | head -c 200)"
    rm -f "$MARK"     # 失败不抑制，下次仍可重试
fi
exit 0
