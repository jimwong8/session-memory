import json, os, shutil

spool_dir = "/app/data/collector-spool"
pending_dir = os.path.join(spool_dir, "pending")
deadletter_dir = os.path.join(spool_dir, "deadletter")

os.makedirs(pending_dir, exist_ok=True)
os.makedirs(deadletter_dir, exist_ok=True)

requeued = 0
for fname in os.listdir(deadletter_dir):
    if not fname.endswith(".json"):
        continue
    src = os.path.join(deadletter_dir, fname)
    try:
        with open(src) as f:
            data = json.load(f)
        data["state"] = "pending"
        data["attempt_count"] = 0
        data["next_retry_at"] = None
        dst = os.path.join(pending_dir, fname)
        with open(dst, "w") as f:
            json.dump(data, f)
        os.remove(src)
        requeued += 1
    except Exception as e:
        print(f"Failed {fname}: {e}")

# Also reset pending files that might be stale
pending_count = len([f for f in os.listdir(pending_dir) if f.endswith(".json")])
deadletter_count = len([f for f in os.listdir(deadletter_dir) if f.endswith(".json")])

print(f"Requeued {requeued} files from deadletter to pending")
print(f"Current pending: {pending_count}, deadletter: {deadletter_count}")
