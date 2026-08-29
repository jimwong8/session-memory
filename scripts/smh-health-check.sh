#!/bin/bash
# Session Memory 健康检查脚本 — 监控已知风险点，防止 2026-08-18 事故复发
# 用法: bash smh-health-check.sh [--alert]
#
# 检查项:
#   1. 本机 watch 循环数（应 ≤ 3，堆积会触发写入风暴）
#   2. Redis key 总量（应 < 100 万，膨胀会让 SCAN/KEYS 变慢）
#   3. dashboard 响应时间（应 < 10s）
#   4. 13号机 worker CPU（应 < 60%）
#   5. 消息写入速率（应 < 100 条/分，异常高说明有写入风暴）
#   6. 核心接口可用性
#
# 退出码: 0=全部正常, 1=存在告警

set -uo pipefail

SMH_API="${SMH_API:-http://100.77.184.40:8000}"
SMH_DIR="${SMH_DIR:-$HOME/.session-memory-agent}"
ALERT=0
[ "${1:-}" = "--alert" ] && ALERT=1

issues=0
warn() {
    echo "⚠️  $1"
    issues=$((issues + 1))
}
ok() {
    echo "✅ $1"
}

echo "=== Session Memory 健康检查 $(date '+%Y-%m-%d %H:%M:%S') ==="

# 1. watch 循环数（本机）
echo ""
echo "--- 1. watch 循环检查（本机）---"
watch_loops=$(ps -ef 2>/dev/null | grep -cE "sleep (10|60)$" || echo 0)
echo "  sleep 循环: $watch_loops 个"
if [ "$watch_loops" -gt 3 ]; then
    warn "watch 循环堆积 ($watch_loops > 3)，可能再次触发写入风暴！检查: pgrep -af 'memory_hook'"
else
    ok "watch 循环正常 ($watch_loops 个)"
fi

# 2. Redis key 总量（13号机）
echo ""
echo "--- 2. Redis key 总量 ---"
if command -v ssh >/dev/null 2>&1 && ssh -o BatchMode=yes -o ConnectTimeout=6 jimwong@10.100.1.13 'true' 2>/dev/null; then
    redis_keys=$(ssh -o BatchMode=yes -o ConnectTimeout=8 jimwong@10.100.1.13 'docker exec session_memory_redis redis-cli dbsize 2>/dev/null' 2>/dev/null || echo 0)
    echo "  Redis 总 key: $redis_keys"
    if [ "${redis_keys:-0}" -gt 1000000 ]; then
        warn "Redis key 过多 ($redis_keys > 100万)，SCAN/KEYS 会变慢！可运行: bash /home/jimwong/session-memory-backend/scripts/redis-cache-cleanup.sh --apply"
    else
        ok "Redis key 数量正常 ($redis_keys)"
    fi
else
    warn "无法 SSH 到 13号机 检查 Redis"
fi

# 3. dashboard 响应时间
echo ""
echo "--- 3. dashboard 响应 ---"
if curl -s -o /dev/null --max-time 30 "$SMH_API/api/v1/admin/dashboard" 2>/dev/null; then
    dash_time=$(curl -s -o /dev/null -w "%{time_total}" --max-time 30 "$SMH_API/api/v1/admin/dashboard" 2>/dev/null)
    echo "  dashboard: ${dash_time}s"
    if [ "$(echo "$dash_time > 10" | bc 2>/dev/null || echo 0)" = "1" ]; then
        warn "dashboard 响应慢 (${dash_time}s > 10s)"
    else
        ok "dashboard 响应正常 (${dash_time}s)"
    fi
else
    warn "dashboard 不可达或超时"
fi

# 4. 13号机 worker CPU
echo ""
echo "--- 4. 13号机 worker CPU ---"
if ssh -o BatchMode=yes -o ConnectTimeout=8 jimwong@10.100.1.13 'true' 2>/dev/null; then
    cpu=$(ssh -o BatchMode=yes -o ConnectTimeout=8 jimwong@10.100.1.13 'ps aux 2>/dev/null | grep "spawn_main" | grep -v grep | awk "{print int(\$3)}" | sort -rn | head -1' 2>/dev/null | tr -d '\r\n' || echo 0)
    echo "  最高 worker CPU: ${cpu}%"
    if [ "${cpu:-0}" -gt 60 ]; then
        warn "worker CPU 过高 (${cpu}% > 60%)"
    else
        ok "worker CPU 正常 (${cpu}%)"
    fi
else
    warn "无法 SSH 到 13号机 检查 CPU"
fi

# 5. 消息写入速率（1 分钟）
echo ""
echo "--- 5. 消息写入速率 ---"
if ssh -o BatchMode=yes -o ConnectTimeout=8 jimwong@10.100.1.13 'true' 2>/dev/null; then
    writes=$(ssh -o BatchMode=yes -o ConnectTimeout=8 jimwong@10.100.1.13 'docker logs session_memory_api --since 1m 2>/dev/null | grep -c "POST /api/v1/sessions/.*/messages.*201"' 2>/dev/null | tr -d '\r\n' || echo 0)
    echo "  近 1 分钟写入: $writes 条"
    if [ "${writes:-0}" -gt 100 ]; then
        warn "写入速率异常高 ($writes 条/分 > 100)，可能有写入风暴！检查: ps -ef | grep memory_hook"
    else
        ok "写入速率正常 ($writes 条/分)"
    fi
else
    warn "无法 SSH 到 13号机 检查写入速率"
fi

# 6. 核心接口
echo ""
echo "--- 6. 核心接口 ---"
for e in "health" "stats" "sessions/recent"; do
    # health 端点在根路径，其他在 /api/v1 下
    if [ "$e" = "health" ]; then
        url="$SMH_API/health"
    else
        url="$SMH_API/api/v1/$e"
    fi
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$url" 2>/dev/null || echo 000)
    if [ "$code" = "200" ]; then
        ok "$url -> $code"
    else
        warn "$url -> $code"
    fi
done

echo ""
echo "=== 检查完成: $issues 个问题 ==="
if [ "$issues" -gt 0 ]; then
    echo "⚠️  存在告警，请关注上述问题"
    exit 1
else
    echo "✅ 全部正常"
    exit 0
fi