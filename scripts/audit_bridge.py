#!/usr/bin/env python3
"""collector/bridge status snapshot for admin dashboard"""
from pathlib import Path
import json, os, subprocess

log_dir = Path(os.environ.get("LOG_DIR", "/home/jimwong/session-memory/logs/audits"))
spool_dir = Path(os.environ.get("SPOOL_DIR", "/home/jimwong/session-memory/data/collector-spool"))
health_file = Path(os.environ.get("HEALTH_FILE", "/home/jimwong/session-memory/logs/collector/collector-health.json"))
log_dir.mkdir(parents=True, exist_ok=True)

def count_files(d):
    return len(list(d.glob("*.json"))) if d.exists() else 0

health = {}
if health_file.exists():
    try: health = json.loads(health_file.read_text())
    except: pass

rt = health.get("runtime", {})
sessions = health.get("sessions", {})

spool_counts = {
    "pending": count_files(spool_dir / "pending"),
    "retry": count_files(spool_dir / "retry"),
    "acked": count_files(spool_dir / "acked"),
    "deadletter": count_files(spool_dir / "deadletter"),
}

ts = subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True).stdout.strip()

data = {
    "status": "ok",
    "generated_at": ts,
    "collector": {
        "status": rt.get("status"),
        "consecutive_failures": rt.get("consecutive_failures"),
        "last_server_status": rt.get("last_server_status"),
        "degraded_reason": rt.get("degraded_reason"),
        "loop_count": rt.get("loop_count"),
        "config_mode": rt.get("run_mode"),
    },
    "spool": spool_counts,
    "sessions": {sid[:16]: {"last_acked_seq": v.get("last_acked_seq"), "status": v.get("status")} for sid, v in sessions.items()},
    "issues": [],
}
if spool_counts["retry"] > 50:
    data["issues"].append("retry=%d > 50" % spool_counts["retry"])
if spool_counts["deadletter"] > 100:
    data["issues"].append("deadletter=%d > 100" % spool_counts["deadletter"])
if rt.get("status") != "active":
    data["issues"].append("collector status=%s" % rt.get("status"))
    data["status"] = "warning"
if rt.get("degraded_reason"):
    data["issues"].append("collector degraded: %s" % json.dumps(rt.get("degraded_reason")))
    data["status"] = "degraded"

latest = log_dir / "audit_bridge_latest.json"
latest.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print(json.dumps(data, ensure_ascii=False))
