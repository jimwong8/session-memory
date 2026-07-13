# Collector → CSS 桥接通路验收报告

**日期**: 2026-05-15  
**系统**: 10.100.1.13  
**链路**: session-memory → css_bridge → collector spool → central-session-server → raw_events

## 1. 组件状态

| 组件 | 状态 | 详情 |
|------|------|------|
| SM API | ok | /health 200 |
| collector systemd | active (running) | opencode-central-collector.service |
| CSS server | :8443 alive | localhost 直连 |
| bridge 门控 | 生效 | CSS_BRIDGE_MAX_PENDING=50 |

## 2. 调优参数

```yaml
# collector (local-13.yaml)
loop_interval_seconds: 60
batch_size: 3
max_attempts: 8
# bridge (css_bridge.py)
_MAX_PENDING: 50  # pending 超过此阈值跳过新事件
filter: role in {user, assistant}  # 跳过 system/tool
```

## 3. raw_events 入库统计

| 指标 | 值 |
|------|----|
| raw_events 总数 | 320 |
| bridge 贡献事件数 | 319 |
| bridge 覆盖会话数 | 7 |
| 活跃 terminal | session-memory-bridge (319), sm-bridge (1) |

## 4. 会话序列验证

| session_id | event_seq 序列 | 连续? |
|------------|---------------|-------|
| d2af4029-... | 1,2,3,4 | ✅ |
| 998bf033-... | 1,2,3,4,5,6 | ✅ |
| 558eb1f0-... | 1,2 | ✅ |
| e0370a0c-... | 1,2,3,4,5 | ✅ |
| eb7b77cb-... | 1..36（间隙：7,16） | ⚠️ 旧 bridge 遗留 |
| d64b0956-... | 1..30 + 高 seq | ⚠️ 旧 bridge 遗留 |

## 5. collector 运行摘要

```
runtime.status: active
consecutive_failures: 0
last_server_status: 200
degraded_reason: null
loop_count: 2+
last_success_at: <live>
```

## 6. spool 积压

| 队列 | 数量 | 说明 |
|------|------|------|
| pending | ~69 | 旧 backlog + 门控限定 ~50 |
| retry | 0 | 已排空 |
| deadletter | 0 | 已归档 |
| acked | 0 | 以 DB last_acked_seq 为准 |

## 7. 维护自动化

- **systemd timer**: opencode-collector-maintenance.timer — 每天裁剪确认的 spool 文件
- **维护脚本**: ~/session-memory/scripts/collector-maintenance.sh
- **归档位置**: ~/session-memory/data/collector-spool-archive/

## 8. 结论

✅ 链路已稳定接通。关键证据：
1. raw_events 持续增长（319 条来自 bridge）
2. collector runtime active、last_server_status=200
3. 新会话序列连续（1,2,3,4...）
4. 限流已通过 batch/interval/门控三层收敛
5. 每日自动维护已配置

⚠️ 注意：
- 这是 best-effort 事实层，非全量消息复制
- 当 pending > 50 时 bridge 会跳过后续事件
- 旧 session 的历史间隙不影响新写入