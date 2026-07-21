# Session Memory 部署文档

## 系统架构

- **服务端**: 100.77.184.40:8000 (FastAPI + PostgreSQL + Redis)
- **客户端**: 100.77.184.40, 10.100.1.18 (SDK + Hook)
- **监控**: Prometheus (9090) + Grafana (3000)

## 已部署组件

### 1. 服务端 (100.77.184.40)

**Docker服务**:
- API: session_memory_api (端口8000)
- PostgreSQL: session_memory_postgres (端口5432)
- Redis: session_memory_redis (内部6379)
- Prometheus: session_memory_prometheus (端口9090)
- Grafana: session_memory_grafana (端口3000)

## API端点

基础URL: http://100.77.184.40:8000/api/v1

- POST /sessions - 创建会话
- GET /sessions?user_id={user_id} - 列出会话
- GET /sessions/{session_id} - 获取会话详情
- POST /sessions/{session_id}/messages - 添加消息
- GET /sessions/{session_id}/context - 获取上下文

## 测试验证

### 健康检查
```bash
curl http://100.77.184.40:8000/health
```

### 创建会话
```bash
curl -X POST http://100.77.184.40:8000/api/v1/sessions   -H 'Content-Type: application/json'   -d '{"user_id":"test_user","title":"测试会话"}'
```

### 添加消息
```bash
curl -X POST http://100.77.184.40:8000/api/v1/sessions/{session_id}/messages   -H 'Content-Type: application/json'   -d '{"role":"user","content":"测试消息"}'
```

## 监控

- Prometheus: http://100.77.184.40:9090
- Grafana: http://100.77.184.40:3000 (admin/admin)

## 运维命令

```bash
cd ~/session-memory
docker-compose up -d
```

## 迁移说明

- 2026-04-26 已完成旧服务端原始会话数据与资料向当前服务端的合并迁移
- 当前生产基准服务端为 100.77.184.40
- 迁移报告位于 backups/migration-20260426/MIGRATION-REPORT.md
