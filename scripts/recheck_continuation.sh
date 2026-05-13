#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="/home/jimwong/session-memory"
LOG_DIR="$BASE_DIR/logs/audits"
BASELINE="$LOG_DIR/continuation_baseline_latest.json"
MAX_WAIT_SECONDS="${1:-600}"
SLEEP_SECONDS="${2:-15}"
mkdir -p "$LOG_DIR"

python3 "$BASE_DIR/scripts/capture_continuation_baseline.py" >/dev/null
BASE_HASH="$(python3 - <<'PY'
import json
from pathlib import Path
p = Path('/home/jimwong/session-memory/logs/audits/continuation_baseline_latest.json')
print(json.loads(p.read_text(encoding='utf-8'))['content_hash'])
PY
)"

START_TS="$(date -u +%Y%m%dT%H%M%SZ)"
END_TIME=$(( $(date +%s) + MAX_WAIT_SECONDS ))

while [ "$(date +%s)" -lt "$END_TIME" ]; do
  python3 "$BASE_DIR/scripts/capture_continuation_baseline.py" >/dev/null
  CUR_HASH="$(python3 - <<'PY'
import json
from pathlib import Path
p = Path('/home/jimwong/session-memory/logs/audits/continuation_baseline_latest.json')
print(json.loads(p.read_text(encoding='utf-8'))['content_hash'])
PY
)"
  if [ "$CUR_HASH" != "$BASE_HASH" ]; then
    bash "$BASE_DIR/scripts/run_audits.sh" >/dev/null 2>&1 || true
    python3 - <<'PY'
import json
from pathlib import Path
base = Path('/home/jimwong/session-memory/logs/audits')
out = {
  'detected_change': True,
  'baseline': json.loads((base / 'continuation_baseline_latest.json').read_text(encoding='utf-8')),
  'audit_continuation': json.loads((base / 'audit_continuation_latest.json').read_text(encoding='utf-8')) if (base / 'audit_continuation_latest.json').exists() else None,
}
(base / 'continuation_recheck_latest.json').write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(out, ensure_ascii=False, indent=2))
PY
    exit 0
  fi
  sleep "$SLEEP_SECONDS"
done

python3 - <<'PY'
import json
from pathlib import Path
base = Path('/home/jimwong/session-memory/logs/audits')
out = {'detected_change': False, 'message': 'latest continuation did not change within wait window'}
(base / 'continuation_recheck_latest.json').write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(out, ensure_ascii=False, indent=2))
PY
exit 1
