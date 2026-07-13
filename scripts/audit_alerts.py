#!/usr/bin/env python3
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BASE = Path('/home/jimwong/session-memory/logs/audits')
summary_path = BASE / 'audit_summary_latest.json'
summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
checks = summary.get('generated_files', [])
recheck = summary.get('continuation_recheck') or {}
continuation_latest_path = BASE / 'audit_continuation_latest.json'
continuation_latest = {}
if continuation_latest_path.exists():
    try:
        continuation_latest = json.loads(continuation_latest_path.read_text(encoding='utf-8'))
    except Exception:
        continuation_latest = {}

severity = 'ok'
issues = []

# Check audit health
for item in checks:
    status = item.get('status')
    name = item.get('name')
    check_issues = item.get('issues', [])
    if name == 'audit_health' and status != 'ok':
        severity = 'critical'
        issues.append({'level': 'critical', 'check': name, 'issues': check_issues or ['health check degraded']})
    elif name in {'audit_duplicates', 'audit_continuation', 'audit_kg'} and status not in {'ok', 'idle'}:
        if severity != 'critical':
            severity = 'warning'
        issues.append({'level': 'warning', 'check': name, 'issues': check_issues})

# Check continuation recheck
continuation_has_latest = bool((continuation_latest or {}).get('latest'))
if continuation_has_latest and recheck.get('exists') and recheck.get('detected_change') is False:
    # idle state: no new continuation events, not an error
    issues.append({'level': 'info', 'check': 'continuation_recheck', 'issues': ['no new continuation events (idle)']})

# === 新增告警阈值检查 ===

def check_disk_usage():
    """检查磁盘使用率"""
    import shutil
    usage = shutil.disk_usage("/")
    percent = usage.used / usage.total * 100
    if percent > 80:
        return {"severity": "warning", "message": f"磁盘使用率 {percent:.1f}% > 80%"}
    return {"severity": "ok", "message": f"磁盘使用率 {percent:.1f}%"}

def check_db_connections():
    """检查 DB 连接数"""
    result = subprocess.run(
        ["docker", "exec", "session_memory_postgres", "psql", "-U", "postgres", "-d", "session_memory", "-At", "-c", "SELECT count(*) FROM pg_stat_activity"],
        capture_output=True, text=True, timeout=10
    )
    count = int(result.stdout.strip() or 0)
    # 连接池最大 60（pool_size=20 + max_overflow=40）
    if count > 48:  # 80% of 60
        return {"severity": "warning", "message": f"DB 连接数 {count} > 48 (80% of max)"}
    return {"severity": "ok", "message": f"DB 连接数 {count}"}

# 执行新增检查
disk_result = check_disk_usage()
if disk_result["severity"] != "ok":
    if severity != "critical":
        severity = "warning"
    issues.append({"level": "warning", "check": "disk_usage", "issues": [disk_result["message"]]})

db_result = check_db_connections()
if db_result["severity"] != "ok":
    if severity != "critical":
        severity = "warning"
    issues.append({"level": "warning", "check": "db_connections", "issues": [db_result["message"]]})

# === 原有输出逻辑保持不变 ===

report = {
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'severity': severity,
    'checks_evaluated': len(checks) + 2,  # +2 是新增的检查项
    'issues': issues,
}
print(json.dumps(report, ensure_ascii=False, indent=2))
