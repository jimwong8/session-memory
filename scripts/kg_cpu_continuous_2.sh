#!/bin/bash
set -u
trap "exit 0" TERM INT
while true; do
  /usr/bin/docker exec -e KG_BATCH=10 -e KG_WORKERS=1 -e VLLM_BASE_URL=http://10.100.1.15:8082/v1 -e VLLM_MODEL=qwen2.5-7b-instruct-q4_k_m.gguf session_memory_api /usr/local/bin/python3 /app/scripts/kg_worker_vllm.py >> /tmp/kg_worker_cpu2_continuous.log 2>&1 || true
  sleep 2
done
