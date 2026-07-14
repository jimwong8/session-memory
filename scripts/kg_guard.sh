#!/bin/bash
# Auto-restart KG workers on 10.100.1.13
log() { echo "[$(date +%H:%M:%S)] $*"; }

cd /home/jimwong/session-memory

while true; do
    # Check and restart V3 worker 1
    pid1=$(docker exec -w /app session_memory_api ps aux 2>/dev/null | grep 'kg_docker_worker.py' | grep -v grep | awk '{print $2}')
    if [ -z "$pid1" ]; then
        log "Starting V3 worker 1"
        docker exec -d -w /app session_memory_api bash -c 'python scripts/kg_docker_worker.py > /tmp/kg_worker.log 2>&1'
    fi
    
    # Check and restart V3 worker 2
    pid2=$(docker exec -w /app session_memory_api ps aux 2>/dev/null | grep 'kg_docker_worker.py' | grep -v grep | awk '{print $2}' | tail -1)
    [ -z "$pid2" ] && log "Starting V3 worker 2" && docker exec -d -w /app session_memory_api bash -c 'python scripts/kg_docker_worker.py > /tmp/kg_worker2.log 2>&1'
    
    # Check and restart R1 worker
    pid3=$(docker exec -w /app session_memory_api ps aux 2>/dev/null | grep 'kg_worker_r1.py' | grep -v grep | awk '{print $2}')
    [ -z "$pid3" ] && log "Starting R1 worker" && docker exec -d -w /app session_memory_api bash -c 'python scripts/kg_worker_r1.py > /tmp/kg_worker_r1.log 2>&1'
    
    sleep 60
done
