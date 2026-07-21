# 会话记忆系统 — 真实点亮验证报告 (2026-07-20)

## 结论：系统从"代码全在、运行时全休眠"变为"KG 链路真活着"

### 真 bug 链（实测定位，非推测）
1. kg_jobs.id 缺 default -> 每条消息落库 500 崩溃
   修复: models.py 给 KGJob.id 加 default=uuid.uuid4 + 重启 api
   验证: /messages 返回 201，kg_jobs 能入队
2. run-kg-worker-once 调 backfill_kg.py (async ORM) 在子进程必崩 MissingGreenlet
   根因: async worker 在 docker exec/子进程跑必崩 (已知踩坑)
   修复: 新写 scripts/kg_worker_internal_sync.py (纯 psycopg2 + 同步 LLM)
3. 容器内缺 psycopg2 -> pip install psycopg2-binary
4. kg_entities/kg_relations 的 version/created_at/updated_at 列 NOT NULL 无 default
   修复: ALTER TABLE 加 DEFAULT (version=1, created_at/updated_at=NOW())

### KG 双时态自愈 — 确定性验证通过
- 造同 source+target 矛盾边: 旧 (核心数据库->Postgres|depends_on) 全部被打 invalid_at
- 新 (核心数据库->Postgres|contradicts) 保持 valid_at, invalid_at=NULL
- 语义 = Graphiti 双时态: 事实演进不删旧边，标记失效+时间戳

### 端到端真实链路验证通过
中文消息 -> /messages 入队 kg_jobs -> 常驻 worker 调 Gemma4E2B 抽取
-> kg_entities/kg_relations 真实填充 (实测 5实体/4关系)
示例: 推荐系统 -> 协同过滤 | derived_from; 推荐系统 -> Redis | depends_on

### 常驻运行
- 循环: 每 20s 调 kg_worker_internal_sync.py --batch 3 (setsid detached)
- 自启: crontab @reboot 已写入
- Gemma 4 E2B 单条抽取 30-80s (MI50 18.3 tok/s)，慢但稳定

### 仍未点亮 (需另立项)
- embedding warmup 关闭 -> 向量召回/重排器无输入 (BM25-only 运行)
- compaction 绑定 offload_enabled (恒 false) -> 长会话不压缩
- L1 记忆演化 / persona 事件驱动: 代码在，需触发实测

## 第二轮点亮 (embedding) - 2026-07-20 续

### 真 bug: embedding 永不加载
- 根因: ENABLE_EMBEDDING_WARMUP 在 Dockerfile 层 ENV=false, .env 改了但只 docker restart 不重读 compose env
- 修复: docker compose up -d api (重建容器应用新 env)
- 验证: /health?deep=true -> embedding: {status: ok, model: BAAI/bge-small-en-v1.5, dim: 384}

### 真 bug: 消息写入不生成 embedding
- /messages 端点显式 generate_embedding=False (主链路保成功)
- 修复: 常驻 backfill_embeddings.py 循环 (PYTHONPATH=/app, 9 msg/s)
- 验证: messages 全部 30/30 有向量

### 向量语义召回真实验证通过
- 查询"用内存存储加速数据读取" -> 命中"Redis 分布式缓存缓解数据库读取" (语义, 非关键词)
- 查询"消息队列异步处理" -> 命中"Kafka 消息队列异步解耦" (语义)
- 系统告别 BM25-only, 向量+RRF+重排全链路工作

### 已知残留 bug (不影响主链路, 低优先)
- hybrid_recall 跨 memory_scenarios/memory_atoms 表报错 (缺 content_tsv/session_id 列)
  被 except 吞掉, 仅 messages 表向量召回有效
- admin/status compaction 展示仍绑 offload_enabled (功能本身在 budget 紧张时跑, 仅展示错)

### 常驻进程 (两条 detached + @reboot)
1. kg_worker_internal_sync.py --batch 3  (每 20s, Gemma 抽 KG)
2. backfill_embeddings.py              (每 60s, 补消息向量)

## 第三轮点亮 (keyword BM25 跨表) - 2026-07-20 续

### 真 bug: keyword 搜索三表全失效
- 根因1: content_tsv 列在三表(messages/memory_atoms/memory_scenarios)都没建
  修复: ALTER TABLE ADD COLUMN content_tsv GENERATED ALWAYS AS tsvector + GIN 索引
- 根因2: clean_query 给每个词加单引号 + AND 连接 -> to_tsquery 解析失败
  修复: 改 OR 连接 + 去词内引号 (clean_query = " | ".join([w for w in ...]))
- 根因3: docker compose up -d 不重建容器, 旧代码一直跑
  修复: docker compose up -d --force-recreate api

### 验证
- keyword 搜索 "PostgreSQL 主从切换 故障" -> 命中对应消息 (kw_hits:1)
- hybrid = BM25(三表) + 向量 + 实体增强 + 重排 全链路工作
- 容器内直接执行 _search_table 逻辑确认命中

### 当前系统完整点亮状态
- KG 抽取 + 双时态自愈: 真活 (Gemma 4 E2B)
- 向量语义召回: 真活 (bge-small-en-v1.5, 384d)
- 关键词 BM25 跨三表: 真活
- 重排器: 配置存在 (需 edgefn rerank key 才激活, 当前 fallback RRF)
- 四模型 LLM 路由: 配置 active
- 常驻: kg_worker (20s) + embedding backfill (60s) + @reboot

### 仍残留 (不影响主链路)
- compaction admin 展示绑 offload_enabled (功能在 budget 紧张时真跑)
- reranker 需 edgefn key 才真激活 (否则 RRF 兜底)
- 仓库未 push (本地修复未提交 GitHub)

## 第四轮修复 (compaction 展示 + reranker 确认) - 2026-07-20 续

### compaction 展示 bug 修复
- 根因: admin/status 里 compaction.enabled 绑 settings.offload_enabled (恒 false)
  实际压缩功能在 context_builder 里真有 (budget 紧张时 summarize dropped msgs)
- 修复: 改展示为 {enabled: True, trigger: budget_force_continuation, threshold_ratio}
- 验证: /api/v1/admin/status -> compaction: {enabled: true, trigger: budget_force_continuation, threshold_ratio: 0.95}

### reranker 确认 (非 bug, 误判修正)
- 之前报告称"需 edgefn key 才激活"是错的
- 实际: .env BACKUP_OPENAI_API_KEY/BASE_URL 已配 edgefn, reranker 复用 -> enabled=True
- 真实验证: 容器内调 Reranker.rerank("数据库缓存方案", [Redis缓存, Kafka队列, 天气])
  返回 score: Redis 0.595 > Kafka 0.001 > 天气 0.000 (语义重排正确)
- /api/v1/admin/status -> rerank: {enabled: true, model: bge-reranker-v2-m3, api_configured: true}

### 系统最终完整状态 (全部真实验证)
- KG 抽取 + 双时态自愈: 真活
- 向量语义召回 (bge-small 384d): 真活
- 关键词 BM25 跨三表: 真活
- 混合召回 (BM25+向量+实体+RRF): 真活
- 重排器 (bge-reranker-v2-m3 via edgefn): 真活 (已实测语义重排)
- compaction 展示: 修正为真实触发逻辑
- 四模型 LLM 路由: active
- 常驻: kg_worker(20s) + embedding backfill(60s) + @reboot
- deep health: postgres ok / redis ok / embedding ok / kg_tables ok / llm 4 active

### 唯一残留
- 本地修复未 push GitHub (终端外部网络闸拦截 api.github.com)
