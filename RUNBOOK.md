# Session Memory 运维 Runbook

## 目标

这份 runbook 用于排查 `session-memory-auto` 的自动消息同步、跨主机幂等去重，以及相关监控面板。

## 服务入口

- API: `http://100.77.184.40:8000`
- Prometheus: `http://100.77.184.40:9090`
- Grafana: `http://100.77.184.40:3000`
- Grafana 默认账号: `admin / admin`

## 关键能力现状

- 工具调用自动审计已启用
- user / assistant 自动消息同步已启用
- 跨主机全局幂等去重已启用
- 幂等相关指标已暴露到 `/metrics`
- Prometheus 最小告警规则已启用

## 最关键的指标

### 1. 幂等预查重命中

Prometheus 指标：

```promql
auto_message_dedupe_hits_total{path="precheck"}
```

### 2. 幂等冲突回退命中

Prometheus 指标：

```promql
auto_message_dedupe_conflicts_total{path="integrity_error"}
```

### 3. 消息写入接口延迟

Prometheus 查询：

```promql
histogram_quantile(0.95, sum by (le, method, endpoint) (
  rate(http_request_duration_seconds_bucket{endpoint="/api/v1/sessions/{session_id}/messages"}[5m])
))
```

## 快速排查命令

### 检查 API 健康

```bash
curl http://127.0.0.1:8000/health
```

### 查看幂等相关指标

```bash
curl -s http://127.0.0.1:8000/metrics | grep auto_message_dedupe
```

### 查看 Prometheus 告警状态

```bash
curl -s http://127.0.0.1:9090/api/v1/rules
```

### 查看 API 最近日志

```bash
docker logs session_memory_api --tail 200
```

## 推荐日常巡检

1. `curl /health`
2. `curl /metrics | grep auto_message_dedupe`
3. `curl http://127.0.0.1:9090/api/v1/rules`
4. `docker logs session_memory_api --tail 100`
5. Grafana 面板是否能看到幂等趋势和消息接口延迟

## 自动巡检脚本

当前已提供以下巡检入口：

### 1. 全量健康巡检

```bash
python3 ~/session-memory/scripts/audit_health.py
```

检查内容：
- `/health` 状态
- 核心表计数
- 重复 `opencode_session_id` 组数量
- `continuation/latest` 是否可读
- 迁移/归并备份工件是否存在

### 2. 重复会话巡检

```bash
python3 ~/session-memory/scripts/audit_duplicates.py
```

检查内容：
- 当前是否还存在重复 `opencode_session_id` 会话组
- 输出重复组明细 JSON

### 3. Continuation 质量巡检

```bash
python3 ~/session-memory/scripts/audit_continuation.py
```

检查内容：
- `last_user_message` 是否被 directive 噪声污染
- `recent_messages` 是否仍混入 TODO continuation 系统指令


### 4. 知识图谱质量巡检

```bash
python3 ~/session-memory/scripts/audit_kg.py
```

检查内容：
- 最近窗口内知识图谱抽取成功次数
- JSON 提取失败次数
- 429 限流次数
- 502 上游错误次数
- `<think>` 污染次数
- `429` 冷却跳过次数 (`kg_skipped_cooldown`)

判断建议：
- 如果 `kg_fail_429` 很高，说明仍在直接撞上游限流
- 如果 `kg_fail_429` 下降但 `kg_skipped_cooldown` 上升，说明冷却机制在生效，系统正以“跳过换稳定”
- 如果长期出现较高 `kg_skipped_cooldown`，则应把知识图谱抽取从主消息写入链路中拆出来，进入异步队列/延后执行模式

说明：
- 该脚本检查的是 **latest continuation 的当前内容**。
- 如果 latest 仍是历史旧 payload，即使代码已经修复，脚本也可能继续报 `degraded`。
- 只有在下一次真实 continuation 重新生成后，这个检查才会反映修复后的效果。

## 巡检结果落盘

统一入口：

```bash
bash ~/session-memory/scripts/run_audits.sh
```

输出目录：

- `~/session-memory/logs/audits/audit_health_latest.json`
- `~/session-memory/logs/audits/audit_duplicates_latest.json`
- `~/session-memory/logs/audits/audit_continuation_latest.json`
- `~/session-memory/logs/audits/audit_kg_latest.json`
- `~/session-memory/logs/audits/audit_summary_latest.json`

同时会按 UTC 时间戳归档单次执行结果，例如：

- `audit_health_YYYYMMDDTHHMMSSZ.json`
- `audit_duplicates_YYYYMMDDTHHMMSSZ.json`
- `audit_continuation_YYYYMMDDTHHMMSSZ.json`
- `audit_kg_YYYYMMDDTHHMMSSZ.json`

## 定时任务

`setup_cron.sh` 现已安装两条任务：

1. 每天 02:00 执行数据库备份
2. 每小时 15 分执行一次自动巡检

当前 cron 计划：

```cron
0 2 * * * cd /home/jimwong/session-memory && /usr/bin/env bash /home/jimwong/session-memory/scripts/backup.sh >> /home/jimwong/session-memory/logs/backup.log 2>&1
15 * * * * cd /home/jimwong/session-memory && /usr/bin/env bash /home/jimwong/session-memory/scripts/run_audits.sh >> /home/jimwong/session-memory/logs/audits/cron.log 2>&1
```

## 真实 continuation 重验说明

当前 `audit_continuation.py` 仍可能报 `degraded`，原因不是新过滤逻辑未部署，而是 `continuation/latest` 仍可能指向旧历史 payload。

只有当下一次新的真实 continuation 重新生成并写入 latest 后，巡检结果才会反映修复后的效果。

## 真实 Continuation 重验流程

### 1. 采集当前 latest 基线

```bash
python3 ~/session-memory/scripts/capture_continuation_baseline.py
```

输出：
- `~/session-memory/logs/audits/continuation_baseline_latest.json`

### 2. 等待新的 latest 出现并自动重跑巡检

```bash
bash ~/session-memory/scripts/recheck_continuation.sh 600 15
```

参数含义：
- 第一个参数：最长等待秒数，默认 `600`
- 第二个参数：轮询间隔秒数，默认 `15`

行为：
- 先采集当前 latest 的内容哈希
- 轮询 `continuation/latest` 是否变化
- 一旦变化，自动执行 `run_audits.sh`
- 将结果写入 `~/session-memory/logs/audits/continuation_recheck_latest.json`

### 3. 如何判断修复已生效

重点检查：
- `audit_continuation_latest.json`
- `continuation_recheck_latest.json`

如果新的 latest 已生成，且 `last_user_message` 不再包含 TODO directive，`recent_messages` 也不再混入系统指令，则说明 continuation 提取修复已经在真实链路生效。

## KG 异步回填模式

当前知识图谱抽取已从主消息写入链路中拆出，改为：

1. 消息落库时仅写入：
   - `kg_extract_pending=true`
   - `kg_extract_status=pending`
2. 后台回填脚本按批次慢速处理
3. 成功后写回：
   - `kg_extract_pending=false`
   - `kg_extract_status=done`
4. 失败后写回：
   - `kg_extract_pending=false`
   - `kg_extract_status=failed`
   - `kg_extract_error=...`

### 手动执行一次 KG 回填

```bash
/home/jimwong/session-memory/scripts/run_kg_jobs_once.sh --apply --batch-size 20 --sleep-seconds 0.2
```

### 直接运行 Python 回填脚本

```bash
python3 ~/session-memory/scripts/backfill_kg_jobs.py --apply --batch-size 20 --sleep-seconds 0.2
```

### 当前效果判断

在 backlog 被实际消费后，最近 3 分钟 `audit_kg.py` 已出现：

- `kg_fail_json_extract = 0`
- `kg_fail_429 = 0`
- `kg_raw_think = 0`
- `kg_skipped_cooldown = 0`
- `status = ok`

这说明异步回填 + 解析增强 + 冷却保护的组合，已经显著改善了 KG 抽取稳定性。

## 总览状态判断

统一总览文件：

- `~/session-memory/logs/audits/audit_summary_latest.json`

当前总览会同时汇总：

- `audit_health`
- `audit_duplicates`
- `audit_continuation`
- `audit_kg`
- `continuation_recheck`

其中 `continuation_recheck` 用于说明：
- 是否已经检测到新的 latest continuation 变化
- 如果没有变化，会明确写出 `latest continuation did not change within wait window`

## KG 异步回填定时化

当前除了备份和统一巡检，还增加了一条后台 KG 回填任务：

```cron
*/10 * * * * cd /home/jimwong/session-memory && /usr/bin/env bash /home/jimwong/session-memory/scripts/adaptive_kg_jobs_backfill.sh >> /home/jimwong/session-memory/logs/kg_backfill.log 2>&1
```

作用：
- 周期性消费 `kg_extract_pending=true` 的消息
- 将 KG 抽取从主消息写入链路中解耦
- 让知识图谱逐步回填，而不是同步阻塞

## 受控 Continuation 样本触发

当需要验证 continuation 过滤与巡检逻辑，而又暂时没有新的真实坏会话样本时，可以使用受控验证脚本：

```bash
python3 ~/session-memory/scripts/seed_clean_continuation.py
bash ~/session-memory/scripts/run_audits.sh
```

用途：
- 人工写入一条干净的 `continuation/latest` payload
- 验证 `audit_continuation.py` 是否能转为 `status=ok`
- 验证统一总览是否同步转绿

注意：
- 这是“受控验证”，用于闭环测试过滤修复与巡检链路
- 它不替代真实坏会话监控
- 真实线上样本仍应由 `capture_continuation_baseline.py + recheck_continuation.sh` 跟踪

## 告警与趋势脚本

### 1. 告警判断

```bash
python3 ~/session-memory/scripts/audit_alerts.py
```

作用：
- 读取 `audit_summary_latest.json`
- 输出统一 `severity`
- 汇总当前仍需关注的问题列表

说明：
- 当前实现里，`continuation_recheck` 的“未观测到变化”会作为信息性问题保留
- 但不会把总告警级别强制拉成 `degraded`

### 2. 趋势汇总

```bash
python3 ~/session-memory/scripts/audit_trends.py
```

作用：
- 汇总最近若干次审计结果
- 显示 `audit_health / audit_duplicates / audit_continuation / audit_kg` 的历史状态轨迹
- 用于判断问题是正在改善、恶化，还是已经稳定

### 3. 统一巡检现已包含的产物

每次 `run_audits.sh` 现在会同时产出：

- 原始检查结果：`audit_health / audit_duplicates / audit_continuation / audit_kg`
- 总览：`audit_summary_latest.json`
- 告警：`audit_alerts_latest.json`
- 趋势：`audit_trends_latest.json`

## 三级告警模型

`audit_alerts.py` 现在输出三级告警：

- `ok`
- `warning`
- `critical`

### 1. critical

用于系统核心健康异常，例如：
- `audit_health != ok`
- 健康接口不可用
- 数据库/核心工件读取失败

### 2. warning

用于能力退化或闭环未完成，例如：
- `audit_duplicates != ok`
- `audit_continuation != ok`
- `audit_kg != ok`
- `continuation_recheck` 尚未观察到新的 latest 变化

说明：
- `continuation_recheck.detected_change=false` 当前被视为 `warning`，因为它表示“监听窗口内没有新样本”，而不是系统已经损坏。

### 3. ok

用于所有核心检查均通过，仅无关紧要信息项为空或不存在时。

## 外部告警摘要输出

当前系统已经提供标准化告警摘要生成脚本：

```bash
python3 ~/session-memory/scripts/build_alert_payload.py
```

输出目录：

- `~/session-memory/logs/alerts/alert_payload_latest.json`
- `~/session-memory/logs/alerts/alert_payload_<timestamp>.json`

字段包含：

- `headline`
- `severity`
- `checks_evaluated`
- `issues`
- `continuation_recheck`

用途：
- 作为后续 webhook / 企业微信 / 飞书 / 邮件通知的统一输入
- 避免直接消费底层多个 audit JSON 文件
- 让外部告警系统只依赖一个稳定的数据结构

## 容量观测脚本

### 1. 容量与吞吐检查

```bash
python3 ~/session-memory/scripts/audit_capacity.py
```

当前输出字段：
- `total_messages`
- `messages_last_1h`
- `messages_last_24h`
- `kg_pending_backlog`
- `active_sessions`
- `archived_sessions`
- `alert_severity`

### 2. 当前阈值解释

- 当 `kg_pending_backlog > 200` 时，`audit_capacity` 会进入 `warning`
- 这表示 KG 异步回填吞吐当前落后于消息流入速度
- 系统未必故障，但已经进入“需要关注与调参”的阶段

### 3. backlog warning 的处理建议

如果 `kg_pending_backlog` 长时间维持高位：

1. 临时增大 `run_kg_jobs_once.sh` 的 `--batch-size`
2. 缩短 KG 定时回填周期
3. 继续观察 `audit_kg.py` 是否仍保持 `ok`
4. 如果 backlog 持续堆积，再考虑引入真正的任务队列/worker 模式

## KG 自适应回填

当前 KG 回填任务已从固定参数切换为自适应入口：

```bash
/home/jimwong/session-memory/scripts/adaptive_kg_backfill.sh
```

### 当前调参规则

- `pending <= 50` → `batch-size=5`, `sleep=0.5`
- `pending <= 200` → `batch-size=10`, `sleep=0.2`
- `pending <= 500` → `batch-size=25`, `sleep=0.1`
- `pending > 500` → `batch-size=50`, `sleep=0.05`

### 当前 cron 任务

```cron
*/10 * * * * cd /home/jimwong/session-memory && /usr/bin/env bash /home/jimwong/session-memory/scripts/adaptive_kg_backfill.sh >> /home/jimwong/session-memory/logs/kg_backfill.log 2>&1
```

### 实际观测

在最近一次手动运行中：

- `pending=320`
- 自动选择：`batch-size=25`
- 自动选择：`sleep-seconds=0.1`

这说明系统已经能够根据 backlog 大小自动提高 KG 回填强度，而不是固定速率处理。

## backlog 自调节补偿

在固定的 KG 自适应回填之外，系统现在还增加了一层 backlog supervisor：

```bash
/home/jimwong/session-memory/scripts/supervise_backlog.sh
```

### 规则

- `pending <= 200`：不额外补偿
- `pending > 200`：额外触发 1 次自适应 KG 回填
- `pending > 500`：额外触发 2 次自适应 KG 回填

### 当前 cron 顺序

```cron
10 * * * * cd /home/jimwong/session-memory && /usr/bin/env bash /home/jimwong/session-memory/scripts/supervise_backlog.sh >> /home/jimwong/session-memory/logs/backlog_supervisor.log 2>&1
15 * * * * cd /home/jimwong/session-memory && /usr/bin/env bash /home/jimwong/session-memory/scripts/run_audits.sh >> /home/jimwong/session-memory/logs/audits/cron.log 2>&1
*/10 * * * * cd /home/jimwong/session-memory && /usr/bin/env bash /home/jimwong/session-memory/scripts/adaptive_kg_backfill.sh >> /home/jimwong/session-memory/logs/kg_backfill.log 2>&1
```

### 实际观察与限制

最近一次补偿运行中：

- `pending=391`
- 触发 `extra_runs=1`
- 实际回填批次：`batch-size=25`
- 结果：`3` 条成功，`22` 条失败/跳过
- 日志中出现 `知识图谱提取触发 429 冷却`

这说明：
- backlog supervisor 已经能主动补偿
- 但在 backlog 高位时，单靠增加批量仍可能被 429 限制
- 若长期处于这种状态，应进一步升级为真正的任务队列 / worker 模式，而不是继续简单放大 batch-size

## KG worker 状态机

当前 KG 后台回填已经从简单 pending 标记，升级为最小任务状态机：

### 消息级状态字段

写入在 `messages.metadata_json` 中：

- `kg_extract_pending`
- `kg_extract_status`
- `kg_extract_attempts`
- `kg_extract_error`
- `kg_extract_next_attempt_at`
- `kg_extract_last_attempt_at`

### 状态含义

- `pending`：新入队，尚未处理
- `retry_wait`：处理失败，但还允许后续重试
- `done`：处理成功
- `deadletter`：重试上限后仍失败，停止自动处理，等待人工治理
- `failed`：早期状态，后续应逐步收敛到 `retry_wait/deadletter`

### 当前 worker 策略

- 新消息入队时：`pending + attempts=0`
- 失败时：
  - 未达阈值 → `retry_wait`
  - 达到阈值 → `deadletter`
- 成功时：`done`

### 容量巡检已新增字段

`audit_capacity.py` 现已观测：

- `kg_pending_backlog`
- `kg_failed`
- `kg_retry_wait`
- `kg_deadletter`
- `oldest_pending_age_seconds`

### 当前结论

如果 backlog 长期高位，且单次批量补偿仍会触发大规模失败/冷却，则说明系统已到达“应升级为真正任务队列/worker 模式”的阶段。此时继续单纯放大 batch-size 的收益会下降。

## kg_jobs 并行账本阶段

当前系统已引入独立任务表 `kg_jobs`，用于作为知识图谱抽取的并行任务账本。

### 当前阶段定位

这不是一次性硬切换，而是“metadata 镜像 + 任务表账本并行期”：

- `messages.metadata_json` 仍保留旧状态字段，兼容现有回填与巡检链路
- 新写入消息会额外向 `kg_jobs` 幂等写入一条 `pending` 任务记录
- 当前 worker 还主要消费 metadata 视角，尚未完全切到 `kg_jobs`

### kg_jobs 最小表结构

核心字段：
- `message_id`
- `session_id`
- `job_type`
- `status`
- `attempts`
- `last_error`
- `available_at`
- `locked_at`
- `locked_by`
- `created_at`
- `updated_at`

### 当前容量双视角

`audit_capacity.py` 现已同时输出：

#### metadata 视角
- `kg_pending_backlog`
- `kg_retry_wait`
- `kg_deadletter`
- `oldest_pending_age_seconds`

#### kg_jobs 视角
- `kg_jobs_pending`
- `kg_jobs_running`
- `kg_jobs_succeeded`
- `kg_jobs_failed`
- `kg_jobs_deadletter`

### 当前结论

如果你看到：
- metadata backlog 很大
- 但 `kg_jobs_pending` 很小

这表示系统正处于“新消息已双写到任务表，但 worker 尚未切到新表消费”的中间阶段，这是预期现象。

## kg_jobs 主视角切换

当前系统已经完成一个关键切换：

- `adaptive_kg_backfill.sh` 现在优先读取 `kg_jobs where status='pending'`
- `supervise_backlog.sh` 现在优先读取 `kg_jobs pending`
- `audit_capacity.py` 的 backlog warning 判断现在以 `kg_jobs_pending` 为主

### 当前验证结果

最近一次 dry-run 结果：

- `adaptive_kg_backfill.sh --dry-run` → `pending=44`, `batch-size=5`, `sleep=0.5`
- `supervise_backlog.sh --dry-run` → `pending=44`, `extra_runs=0`

这说明：
- 调度层已经不再被旧 metadata backlog 主导
- 新任务表 `kg_jobs` 已经成为回填调度的主视角

### 当前 capacity warning 含义变化

切换之后，如果仍出现 `warning`，更应优先关注：

- `kg_deadletter`
- `oldest_pending_age_seconds`

而不是单纯看旧 metadata backlog 数字。

旧 metadata 指标仍保留，主要用于迁移过渡期对账，不再作为主调度依据。

## kg_jobs 主消费调优结果

当前系统已经进入：

- `kg_jobs` 作为主消费调度视角
- legacy metadata worker 退为手动补洞/回退工具

### 当前调优后规则

#### `adaptive_kg_jobs_backfill.sh`
- `pending <= 20` → `batch-size=10`, `sleep=0.3`
- `pending <= 80` → `batch-size=10`, `sleep=0.2`
- `pending <= 200` → `batch-size=20`, `sleep=0.1`
- `pending > 200` → `batch-size=40`, `sleep=0.05`

#### `supervise_kg_jobs_backlog.sh`
- `pending <= 80`：不补偿
- `pending > 80`：补偿 1 次
- `pending > 400`：补偿 2 次

### 当前实际观测

最近一次 dry-run 结果：
- `kg_jobs pending = 138`
- 自适应回填选择：`batch-size=20`, `sleep=0.1`
- backlog supervisor 选择：`extra_runs=1`

### legacy 工具当前定位

以下脚本保留，但不再作为主消费器：
- `backfill_kg.py`
- `run_kg_once.sh`
- 旧 metadata 视角的 backlog 只保留用于对账和补洞

用途：
- 回滚时快速恢复旧链路
- 对历史 metadata backlog 做一次性补洞
- 与 `kg_jobs` 新链路做迁移期对账

## kg_jobs 正式主消费接管

当前正式主消费器：`run_kg_jobs_once.sh` / `adaptive_kg_jobs_backfill.sh`

legacy 工具：`run_kg_once.sh` / `backfill_kg.py` 仅保留为手动补洞与回滚工具，不再作为常态调度路径。

## 项目级跨会话共享（第一批）

当前系统已接入第一批跨会话共享能力，基于 `sessions.metadata_json.project_id` 实现：

### 已落地能力

- `SessionCreate` 支持 `project_id`
- `ContextWindow` 支持：
  - `shared_summary`
  - `shared_graph_context`
- 新增项目级接口：
  - `GET /projects/{project_id}/search/summaries`
  - `GET /projects/{project_id}/search/kg`
- `ContextBuilder` 在当前会话带 `project_id` 时，会尝试注入：
  - 同项目其他会话的最近摘要
  - 同项目其他会话的最近 KG 实体命中

### 当前限制

这批能力已经“接线完成”，但完整 E2E 效果依赖两个前置条件：

1. 源会话必须已经生成 summary
2. 源会话消息必须已被 KG worker 消费完成

因此在 KG backlog 较高时，项目级共享接口可能暂时返回空结果，不表示接口坏了，而是共享源数据尚未完成沉淀。

### 当前阶段结论

- 共享基础设施已到位
- 项目级接口已存在
- ContextBuilder 已具备共享挂钩
- 下一步若要做稳定 E2E，应在 backlog 较低窗口下进行，或用预先沉淀好的 project 样本做验证

## 共享巡检脚本

当前已新增：

```bash
python3 ~/session-memory/scripts/audit_shared_memory.py
```

### 检查内容

- 当前带 `project_id` 的会话数量
- 不同 `project_id` 数量
- 项目级 summary 共享源数量
- 项目级 KG 共享源数量
- 采样 project 的共享可用性

### 当前实际状态解释

如果出现：
- `summary_sources > 0`
- 但 `kg_entity_sources = 0`

这表示：
- 第一批项目级摘要共享已经开始可用
- 但项目级 KG 共享仍依赖 KG backlog 被进一步消化或更多样本沉淀

这类 warning 不代表共享接口损坏，而表示“共享基础设施已接好，但共享数据沉淀尚未完全到位”。

