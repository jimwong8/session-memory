#!/bin/bash
# Redis 会话缓存清理脚本 — 减少 key 总量，防止 SCAN/KEYS 变慢
# 用法: bash redis-cache-cleanup.sh [--apply]
#
# 背景: session_memory_redis 会积累大量 session:* / session:msgs:* 缓存
# （系统处理 153 万会话，TTL 几小时~一天），key 总量可达 90 万，
# 导致 dashboard 的 SCAN/KEYS 遍历全库变慢（10-30s）。
#
# 策略:
#   1. 只清理 session:* / session:msgs:* 前缀（会话热缓存，可重建，非持久数据）
#   2. 保留 llm:route:*、plugin-state:*、continuation:* 等关键前缀
#   3. 分批删除（每批 500），避免阻塞 Redis
#   4. 默认 dry-run，加 --apply 才真正执行
#
# 注意: 本脚本必须在 13号机 上运行（Redis 容器在 13号机）。

set -uo pipefail

REDIS_C="docker exec session_memory_redis redis-cli"
DRY_RUN=1
if [ "${1:-}" = "--apply" ]; then
    DRY_RUN=0
fi

echo "=== Redis 清理前状态 ==="
$REDIS_C dbsize 2>/dev/null | awk '{print "总 key 数: " $1}'
$REDIS_C info memory 2>/dev/null | grep -E "used_memory_human" || true

echo ""
echo "=== 各前缀分布 ==="
$REDIS_C --scan 2>/dev/null | sed -E 's/^([a-z:_]+).*/\1/' | sort | uniq -c | sort -rn | head -8 || true

# 分批清理指定前缀
# 原理: redis-cli --scan 每次完整遍历并返回匹配 key；删除后下次遍历不再返回它们。
# 每批取 500 个删除，循环直到某次遍历不足 500 个（说明接近清空）。
cleanup_prefix() {
    local prefix="$1"
    echo ""
    echo "=== 清理 $prefix ==="
    local total=0 round=0
    while true; do
        round=$((round + 1))
        local keys
        keys=$($REDIS_C --scan --pattern "${prefix}*" --count 500 2>/dev/null | head -500 || true)
        local batch_count
        batch_count=$(echo "$keys" | grep -c . || echo 0)
        if [ "$batch_count" -eq 0 ]; then
            echo "  无更多 key，完成"
            break
        fi
        total=$((total + batch_count))
        if [ "$DRY_RUN" = "1" ]; then
            echo "  [dry-run] 将删除 ${batch_count} 个 key（累计 ${total}）"
        else
            echo "$keys" | tr '\n' ' ' | xargs -r $REDIS_C del >/dev/null 2>&1 || true
            echo "  已删除 ${batch_count} 个 key（累计 ${total}）"
        fi
        # 本批不足 500 个 = 已接近清空（或 key 已删完）
        if [ "$batch_count" -lt 500 ]; then
            break
        fi
        if [ "$round" -gt 3000 ]; then
            echo "  ⚠️ 超过 3000 轮，中止（疑似死循环）"
            break
        fi
    done
    echo "  $prefix 合计: $total 个"
}

cleanup_prefix "session:msgs:"
cleanup_prefix "session:"

echo ""
echo "=== Redis 清理后状态 ==="
$REDIS_C dbsize 2>/dev/null | awk '{print "总 key 数: " $1}'
$REDIS_C info memory 2>/dev/null | grep -E "used_memory_human" || true

if [ "$DRY_RUN" = "1" ]; then
    echo ""
    echo "⚠️  这是 dry-run，未实际删除。确认无误后加 --apply 执行。"
fi