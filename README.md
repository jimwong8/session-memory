# Session Memory - 会话记忆系统

集中式会话管理和长期记忆系统，支持多台服务器共享使用。

## 快速开始

### 服务端 (100.77.184.40)

```bash
cd ~/session-memory
docker-compose up -d
```

### 客户端 (100.77.184.40, 10.100.1.18)

SDK和Hook已自动部署到 ~/.config/opencode/

## 核心功能

- ✅ 会话管理 (创建、查询、更新)
- ✅ 消息持久化 (支持向量嵌入)
- ✅ 智能上下文构建 (最近消息 + 向量检索)
- ✅ 知识图谱提取 (AtlasCloud + 本地 GPU)
- ✅ 离线队列 (网络故障时本地缓存)
- ✅ 自动备份 (PostgreSQL + Redis)
- ✅ 监控告警 (Prometheus + Grafana)

## 技术栈

- **后端**: FastAPI + Python 3.13
- **数据库**: PostgreSQL 16 + pgvector
- **缓存**: Redis 7
- **嵌入**: bge-m3 (1024d, SiliconFlow)
- **KG 提取**: AtlasCloud DeepSeek-V3.2-Exp / 本地 Gemma 4 E2B
- **监控**: Prometheus + Grafana
- **容器**: Docker + Docker Compose

## 文档

| 文档 | 说明 |
|------|------|
| [README.md](./README.md) | 项目概览 |
| [DEPLOYMENT.md](./DEPLOYMENT.md) | 部署指南 |
| [KG_DEPLOY_GUIDE.md](./KG_DEPLOY_GUIDE.md) | **KG 知识图谱部署指南** |
| [CHANGELOG.md](./CHANGELOG.md) | **版本变更日志** |
| [RUNBOOK.md](./RUNBOOK.md) | 运维手册 |
| [FRONTEND-ANALYSIS.md](./FRONTEND-ANALYSIS.md) | 前端架构分析 |
| [VERIFIED_STATUS.md](./VERIFIED_STATUS.md) | 已验证状态 |
| [docs/credentials-inventory.md](./docs/credentials-inventory.md) | 凭据清单 |
| [docs/design-report.md](./docs/design-report.md) | 设计报告 |
| [API 文档](http://100.77.184.40:8000/docs) | Swagger UI |

## 服务状态

```bash
# 检查所有服务
docker ps | grep session_memory

# 健康检查
curl http://100.77.184.40:8000/health

# 查看日志
docker logs session_memory_api -f
```

## 运维

### 备份
```bash
~/session-memory/scripts/backup.sh
```

### 恢复
```bash
~/session-memory/scripts/restore.sh /path/to/backup.sql
```

### 监控
```bash
~/session-memory/scripts/monitor.sh
```

### KG 提取监控
```bash
# Worker 数量
ps aux | grep kg_atlas_v14 | grep -v grep | wc -l

# 进度查询
docker exec session_memory_postgres psql -U postgres -d session_memory -c \
  "SELECT count(*) FROM messages WHERE metadata_json->>'kg_extract_status'='done_local';"

# 跳过条数
grep -c SKIP /tmp/atlas_workers/p0.log
```

## 架构设计

```
┌─────────────────────────────────────────────────────────┐
│                    客户端 (多台机器)                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ OpenCode     │  │ OpenCode     │  │ OpenCode     │  │
│  │ + SDK        │  │ + SDK        │  │ + SDK        │  │
│  │ + Hook       │  │ + Hook       │  │ + Hook       │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │
│         │                  │                  │          │
│         └──────────────────┼──────────────────┘          │
│                            │                             │
└────────────────────────────┼─────────────────────────────┘
                             │ HTTP API
                             ▼
┌─────────────────────────────────────────────────────────┐
│              服务端 (100.77.184.40)                       │
│  ┌──────────────────────────────────────────────────┐   │
│  │  FastAPI (8000)                                  │   │
│  │  - 会话管理                                       │   │
│  │  - 消息持久化                                     │   │
│  │  - 上下文构建                                     │   │
│  │  - KG 提取                                       │   │
│  └────┬─────────────────────────────────────┬───────┘   │
│       │                                     │           │
│       ▼                                     ▼           │
│  ┌─────────────┐                    ┌─────────────┐    │
│  │ PostgreSQL  │                    │   Redis     │    │
│  │ + pgvector  │                    │   (缓存)    │    │
│  └─────────────┘                    └─────────────┘    │
│                                                         │
│  ┌──────────────┐                   ┌──────────────┐   │
│  │ Prometheus   │                   │   Grafana    │   │
│  │   (9090)     │                   │   (3000)     │   │
│  └──────────────┘                   └──────────────┘   │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │ KG Workers (32 并行)                             │   │
│  │  - AtlasCloud DeepSeek-V3.2-Exp (主力)           │   │
│  │  - 本地 Gemma 4 E2B (GTX 750 Ti)                 │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

## 许可证

MIT
