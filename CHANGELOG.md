# Changelog

本文档记录 Session Memory 系统的重大变更和修复。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

## [Unreleased] - 2026-08-02

### 修复

#### AtlasCloud Worker 稳定性
- **SSL 连接修复** — Python `requests` 验证 SSL 导致 `ConnectionResetError`，通过 `session.verify = False` 解决（AtlasCloud 使用 Cloudflare 代理，部分路径下证书验证异常）
- **Hermes Artifact 清洗** — 消息内容中的 `[SILENT]`、`[IMPORTANT:]`、`DELIVERY:` 等 Hermes 指令会导致模型返回非 JSON 内容，新增 `clean()` 函数预处理
- **死循环跳过机制** — 单条消息连续失败 10 次后自动标记为 `failed` 跳过，防止 worker 无限卡在一条坏消息上
- **进程清理** — 旧版 multiprocessing fork 进程（PID 962738/962739）占 97% CPU，通过 `docker restart session_memory_api` 彻底清理

#### 系统维护
- **磁盘清理** — `/tmp` 被 13GB 备份文件占满（100%），清理后恢复到 22%
- **API 容器重启** — 停止后重新启动，消除僵尸进程

### 变更

#### KG 提取加速
- **Worker 数量** — 从 16 提升到 32 个并行 worker（partition 0-31）
- **模型主力** — AtlasCloud DeepSeek-V3.2-Exp（5.6s/条，无 RPM 限制）
- **理论产能** — ~20,000 条/小时（32 workers × 5.6s）
- **实际产出** — 受 DB 查询和 API 并发限制，约 300 条/分钟

#### LXC1001 (GTX 750 Ti) 模型切换
- **Qwen 2.5 3B → Gemma 4 E2B** — 3B 模型指令遵循能力太差，JSON 解析失败率 >90%，GPU 97% 满载但 0 有效产出。切换为 Gemma 4 E2B Q4_K_M（2.9GB），指令遵循显著改善

#### 代码同步
- 最新源码已同步到 GitHub `jimwong8/session-memory` main 分支

### 数据

| 指标 | 值 |
|------|-----|
| DB 总消息 | 1,860,000+ |
| 已完成 KG 提取 | ~34,400 条 |
| 待处理 | ~1,590,000 条 |
| KG 实体 | ~152,000 |
| KG 关系 | ~130,000 |
| 系统负载 | 1.77 → 4.13（32 workers 运行中） |

---

## [0.9.0] - 2026-08-01

### 新增
- AtlasCloud API 集成（DeepSeek-V3.2-Exp、v4-flash 等模型）
- 多模型 worker 支持（AtlasCloud + SiliconFlow + edgefn）
- Dashboard v4 前端（暗色主题，7 标签页）
- 嵌入模型统一为 bge-m3 (1024d)

### 修复
- PostgreSQL 认证从 scram-sha-256 改为 trust（修复容器内 psycopg2 超时）
- `abs(hashtext(id::text)) % 8` 导致全表扫描（25s），改为覆盖索引后 <1ms
- SSL 连接错误（openai 库 + verify=False）
- SiliconFlow 429 限流（切换到 AtlasCloud 无限流）

---

## [0.8.0] - 2026-07-31

### 新增
- LXC1001 (GTX 750 Ti) GPU Worker 部署
- Qwen 2.5 3B Q4_K_M 模型

### 修复
- kg_jobs 表 id 列缺失默认值

---

## [0.7.0] - 2026-07-24

### 新增
- Session Memory Terminal API（注册、心跳、共享会话）
- Dashboard 终端管理面板
- 多源 AI 客户端同步（Hermes + OpenCode）

---

## [0.6.0] - 2026-07-15

### 新增
- 初始版本发布
- 基础会话管理、KG 提取框架
- Docker Compose 部署
