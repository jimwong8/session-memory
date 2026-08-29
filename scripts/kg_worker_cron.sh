#!/bin/bash
# KG Worker Cron - 双模型并行（智谱 + vLLM），各自 flock 防重叠

# 等待容器就绪（最多 60 秒）
for i in {1..12}; do
    if docker exec session_memory_api curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        break
    fi
    sleep 5
done

# 智谱 glm-4-flash worker
(
    flock -n 201 || exit 0
    docker exec -e KG_BATCH=100 -e KG_WORKERS=16 session_memory_api python3 /app/scripts/kg_worker_zhipu.py >> /tmp/kg_worker_zhipu.log 2>&1
) 201>/tmp/kg_worker_zhipu.lock &

# 本地 vLLM Qwen2.5-7B worker（10.100.1.15:8000）
(
    flock -n 203 || exit 0
    docker exec -e KG_BATCH=200 -e KG_WORKERS=48 -e VLLM_BASE_URL=http://10.100.1.15:8000/v1 -e VLLM_MODEL=/model session_memory_api python3 /app/scripts/kg_worker_vllm.py >> /tmp/kg_worker_vllm.log 2>&1
) 203>/tmp/kg_worker_vllm.lock &

wait
