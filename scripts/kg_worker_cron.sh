#!/bin/bash
# Kg Worker Cron 脚本 - 等待容器就绪后执行

# 等待容器就绪（最多等待 120 秒）
for i in {1..24}; do
    if docker exec session_memory_api curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        break
    fi
    sleep 5
done

# 执行 kg_worker
docker exec session_memory_api python3 /app/scripts/kg_worker.py >> /tmp/kg_worker.log 2>&1
