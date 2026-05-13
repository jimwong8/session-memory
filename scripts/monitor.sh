#!/bin/bash

check_service() {
    local service=$1
    local url=$2
    
    if curl -sf "" > /dev/null 2>&1; then
        echo "[CST 2026 оны Дөрөвдүгээр сар 22, Лх 00:43:11] ✓  正常"
        return 0
    else
        echo "[CST 2026 оны Дөрөвдүгээр сар 22, Лх 00:43:11] ✗  异常，尝试重启..."
        docker restart "session_memory_"
        return 1
    fi
}

# 检查 API
check_service "api" "http://localhost:8000/health"

# 检查 PostgreSQL
if docker exec session_memory_postgres pg_isready -U postgres > /dev/null 2>&1; then
    echo "[CST 2026 оны Дөрөвдүгээр сар 22, Лх 00:43:11] ✓ PostgreSQL 正常"
else
    echo "[CST 2026 оны Дөрөвдүгээр сар 22, Лх 00:43:11] ✗ PostgreSQL 异常，尝试重启..."
    docker restart session_memory_postgres
fi

# 检查 Redis
if docker exec session_memory_redis redis-cli ping > /dev/null 2>&1; then
    echo "[CST 2026 оны Дөрөвдүгээр сар 22, Лх 00:43:11] ✓ Redis 正常"
else
    echo "[CST 2026 оны Дөрөвдүгээр сар 22, Лх 00:43:11] ✗ Redis 异常，尝试重启..."
    docker restart session_memory_redis
fi
