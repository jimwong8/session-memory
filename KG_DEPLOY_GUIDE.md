# KG 部署指南

本文档描述如何在生产环境部署和调优 KG 知识图谱提取系统。

## 架构概述

```
┌─────────────────────────────────────────────────────────────────┐
│ 13号机 (10.100.1.13 / 100.77.184.40)                           │
│                                                                  │
│  ┌─────────────────┐    ┌─────────────────┐    ┌──────────────┐ │
│  │ AtlasCloud      │    │ Local GPU       │    │ SiliconFlow  │ │
│  │ V3.2-Exp        │    │ Gemma 4 E2B     │    │ GLM-4.5-Air  │ │
│  │ 5.6s/条 无限流  │    │ 5.6s/条 无限流  │    │ 3s/条 有RPM  │ │
│  └────────┬────────┘    └────────┬────────┘    └──────┬───────┘ │
│           │                      │                     │         │
│           └──────────────────────┼─────────────────────┘         │
│                                  ▼                               │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ PostgreSQL (session_memory DB)                               ││
│  │ messages / kg_entities / kg_relations / kg_jobs             ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

## 组件

### 1. AtlasCloud Worker (主力)

**位置**: 13号机 `/tmp/atlas_workers/kg_atlas_v14.py`  
**API**: `https://api.atlascloud.ai/v1`  
**Model**: `deepseek-ai/DeepSeek-V3.2-Exp`  
**Key**: `apikey-03a16d10aec040208ac3597f0e529d8e`

**启动方式**:
```bash
cd /tmp/atlas_workers
for i in $(seq 0 31); do
  nohup python3 -u kg_atlas_v14.py --partition $i --total 32 > p${i}.log 2>&1 &
done
```

**监控**:
```bash
ps aux | grep kg_atlas_v14 | grep -v grep | wc -l  # Worker 数量
tail -5 p0.log                                       # 查看最新日志
grep -c SKIP p0.log                                  # 跳过条数
```

**检查进度**:
```bash
docker exec session_memory_postgres psql -U postgres -d session_memory -c \
  "SELECT count(*) FROM messages WHERE metadata_json->>'kg_extract_status'='done_local';"
```

### 2. GPU Worker (辅助)

**位置**: LXC1001 (PVE 10.100.1.252, CT 1001)  
**Model**: Gemma-4-E2B-Q4_K_M (2.9GB)  
**LLM Server**: llama-server 端口 18881

**启动 llama-server**:
```bash
/opt/llama.cpp/build.bak/bin/llama-server \
  -m /models/Gemma-4-E2B-Q4_K_M.gguf \
  -ngl 999 --ctx-size 16384 -np 4 \
  --flash-attn on -ctk q4_0 -ctv q4_0 \
  --batch-size 256 --ubatch-size 256 \
  --host 0.0.0.0 --port 18881 --mlock -t 8 -tb 4
```

**启动 worker**:
```bash
cd /opt/models
nohup python3 -u kg_gpu_v3.py --partition 0 --total 1 > kg_gpu.log 2>&1 &
```

**监控**:
```bash
nvidia-smi  # GPU 利用率
cat kg_gpu.log | tail -5
```

## 配置

### PostgreSQL

**认证**: trust（无密码）  
**连接**: `host=127.0.0.1 port=5432 dbname=session_memory user=postgres`  
**索引**: `idx_msgs_kg_process ON messages(id) WHERE kg_extract_pending='true' AND length(content)>=50`

### Worker 关键参数

| 参数 | 值 | 说明 |
|------|-----|------|
| FOR UPDATE SKIP LOCKED | 是 | 并发安全 |
| ORDER BY id | 升序 | 保证处理顺序 |
| max_tokens | 1000 | LLM 输出上限 |
| temperature | 0 | 确定性输出 |
| session.verify | False | AtlasCloud SSL |
| skip threshold | 10 次 | 连续失败跳过 |

## 故障排查

### Worker 卡死

**症状**: Worker 运行但 DB 进度不动

1. 检查 API 连通性:
   ```bash
   curl -s -o /dev/null -w "%{http_code}" https://api.atlascloud.ai/v1/models \
     -H "Authorization: Bearer apikey-03a16d10aec040208ac3597f0e529d8e"
   ```

2. 检查 SSL 错误:
   ```bash
   grep -c "ConnectionResetError" /tmp/atlas_workers/p0.log
   ```

3. 检查死循环:
   ```bash
   grep -c SKIP /tmp/atlas_workers/p0.log
   ```

4. 检查 DB 索引是否生效:
   ```sql
   EXPLAIN ANALYZE SELECT id FROM messages 
   WHERE metadata_json->>'kg_extract_pending'='true' AND length(content)>=50 
   ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED;
   ```

### 系统负载过高

**症状**: load > 10

1. 检查僵尸进程:
   ```bash
   ps aux --sort=-%cpu | head -5
   ```

2. 检查 API 容器是否占 CPU:
   ```bash
   docker stats --no-stream
   ```

3. 必要时重启 API 容器:
   ```bash
   docker update --restart=no session_memory_api
   docker stop session_memory_api
   ```

### 磁盘满

**症状**: `/tmp` 使用率 100%

```bash
du -sh /tmp/* | sort -rh | head -10
rm -f /tmp/backup_*.tar
rm -rf /tmp/atlas_workers/*.log
```

### GPU 空转

**症状**: GPU 97%+ 但 0 有效产出

**原因**: 小模型（3B）指令遵循能力不足，JSON 解析失败率 >90%

**方案**: 切换到更大模型（Gemma 4 E2B）或放弃 GPU worker

## 性能调优

### 增加并行度

当前 32 workers，可根据系统负载调整:
- load < 5: 可增至 48-64 workers
- load 5-10: 保持 32
- load > 10: 减少到 16

### 批量处理

单条 API 调用效率低。AtlasCloud 支持 batch 时，可改为 batch=4-8 条消息一起处理。

### 模型选择

| 模型 | 速度 | RPM | JSON 质量 | 推荐 |
|------|------|-----|-----------|------|
| DeepSeek-V3.2-Exp | 5.6s | 无 | 高 | 主力 |
| GLM-4.5-Air | 3s | 有限 | 高 | 辅助 |
| Gemma 4 E2B (本地) | 5.6s | 无 | 中 | 辅助 |
| Qwen 2.5 3B (本地) | 4s | 无 | 极差 | 不推荐 |

## 安全

- API Key 不写入日志或 GitHub
- PostgreSQL trust 认证仅限本地
- AtlasCloud SSL 关闭验证（因为 Cloudflare 代理兼容问题）
- Worker 进程运行在非 root 用户（jimwong）

## 备份

```bash
# 数据库备份
docker exec session_memory_postgres pg_dump -U postgres session_memory > backup_$(date +%Y%m%d).sql

# 配置备份
tar czf config_backup.tar.gz \
  /tmp/atlas_workers/kg_atlas_v14.py \
  /home/jimwong/session-memory-backend/docker-compose.yml \
  /home/jimwong/session-memory-backend/.env.example
```
