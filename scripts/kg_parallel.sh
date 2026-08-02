#!/bin/bash
DB_URL="postgresql://postgres:postgres@postgres:5432/session_memory"
EF_URL="https://api.edgefn.net/v1"
K1="sk-syEFrinxzFq78jolC7Fa7eEa605b4dC8B9F4F717FdBa28Bf"
K2="sk-udSXzOGbI06G7UdkA684D21671Ac4dB29207753813276aC1"
BATCH=1 TOTAL=4

run() {
    docker exec -w /app \
        -e KG_DB_URL="$DB_URL" -e KG_LLM_URL="$EF_URL" \
        -e KG_LLM_KEY="$1" -e KG_LLM_MODEL="DeepSeek-V3.2-EXP" \
        -e KG_PARTITION="$2" -e KG_PARTITIONS="$TOTAL" \
        session_memory_api python3 /app/scripts/kg_worker_internal_sync.py --batch $BATCH 2>&1
}

run "$K1" 0 &
sleep 0.5
run "$K2" 1 &
sleep 0.5
run "$K1" 2 &
wait
