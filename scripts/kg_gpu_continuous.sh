#!/bin/bash
set -u
trap 'exit 0' TERM INT
while true; do
  /usr/bin/docker exec -e KG_BATCH=4 -e KG_WORKERS=4 -e VLLM_BASE_URL=http://10.100.1.15:8000/v1 -e VLLM_MODEL=/model session_memory_api /usr/local/bin/python3 /app/scripts/kg_worker_vllm.py >> /tmp/kg_worker_gpu_continuous.log 2>&1 || true
  sleep 2
done
