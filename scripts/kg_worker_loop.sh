#!/usr/bin/env bash
# 常驻循环：消费 session_memory 的 kg_jobs 队列（调 API 自带端点，不碰 DB 直写）
# 每 30s 调一次 /api/v1/admin/ops/run-kg-worker-once；失败不退出，持续重试
while true; do
  curl -s -m 25 -X POST http://localhost:8000/api/v1/admin/ops/run-kg-worker-once >/dev/null 2>&1
  sleep 30
done
