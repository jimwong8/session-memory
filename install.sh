#!/usr/bin/env bash
set -euo pipefail

# session-memory-agent install.sh — 适配 jimwong8 的 Session Memory 系统

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

REPO_URL="https://github.com/jimwong8/session-memory-backend.git"
INSTALL_DIR="${HOME}/.session-memory-agent"

# === 检测目标主机 ===
if [[ "$(hostname)" == "jimwong-standardpc" ]]; then
    SESSION_MEMORY_HOST="100.77.184.40"
else
    echo -n "Session Memory 主机 IP (默认 100.77.184.40): "
    read -r INPUT_HOST
    SESSION_MEMORY_HOST="${INPUT_HOST:-100.77.184.40}"
fi
SESSION_MEMORY_API="http://${SESSION_MEMORY_HOST}:8000"
CSS_API="http://${SESSION_MEMORY_HOST}:8443"
USER_ID="${USER:-$(whoami)}"
HOST="$(hostname)"

info "目标: ${SESSION_MEMORY_API}"

# === 检查连通性 ===
if ! curl -sf "${SESSION_MEMORY_API}/health" >/dev/null 2>&1; then
    warn "无法连接 ${SESSION_MEMORY_API}，确认主机可达后重试"
    exit 1
fi
ok "Session Memory 可达"

# === 创建目录 ===
mkdir -p "${INSTALL_DIR}"
cd "${INSTALL_DIR}"

# === 配置文件 ===
cat > config.json << CFGEOF
{
    "session_memory_api": "${SESSION_MEMORY_API}",
    "css_api": "${CSS_API}",
    "user_id": "${USER_ID}",
    "hostname": "${HOST}",
    "project_key": "session-memory-agent"
}
CFGEOF
ok "config.json 已创建"

# === memory_hook.py ===
cat > memory_hook.py << 'PYEOF'
#!/usr/bin/env python3
"""session-memory-agent: 会话记忆钩子 — 对接 Session Memory API"""
import sys, os, json, uuid, time, urllib.request, urllib.error
from pathlib import Path

CONFIG = json.load(open(Path(__file__).parent / "config.json"))
API = CONFIG["session_memory_api"]
USER_ID = CONFIG["user_id"]
HOST = CONFIG["hostname"]

def api_get(path):
    try:
        r = urllib.request.urlopen(f"{API}{path}", timeout=10)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def api_post(path, data):
    try:
        req = urllib.request.Request(
            f"{API}{path}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"}
        )
        r = urllib.request.urlopen(req, timeout=10)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        return {"error": str(e)}

def start(resume_words=None):
    """初始化会话"""
    payload = {
        "title": f"{HOST}:{USER_ID}@{time.strftime('%Y%m%d-%H%M')}",
        "user_id": USER_ID,
        "metadata_json": {"host": HOST, "source": "session-memory-agent", "os": os.uname().sysname}
    }
    r = api_post("/api/v1/sessions", payload)
    if "id" in r:
        ok(f"会话已创建: {r['id'][:12]}")
        sid_file = Path(__file__).parent / ".session_id"
        sid_file.write_text(r["id"])
    else:
        warn(f"创建失败: {r}")
    return r

def tick(user_msg, reply_msg):
    """保存对话消息 — 对接 /api/v1/sessions/{id}/messages"""
    sid_file = Path(__file__).parent / ".session_id"
    if not sid_file.exists():
        start()
    sid = sid_file.read_text().strip()

    # 系统: 用户消息
    api_post(f"/api/v1/sessions/{sid}/messages", {
        "role": "user",
        "content": user_msg[:2000]
    })
    # 系统: 助手回复
    r = api_post(f"/api/v1/sessions/{sid}/messages", {
        "role": "assistant",
        "content": reply_msg[:4000]
    })
    if "id" in r:
        ok(f"消息已推送: {r['id'][:12]}")
    else:
        warn(f"推送失败: {r}")
    return r

def close(summary=""):
    return {"status": "closed", "summary": summary, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ")}

def status():
    h = api_get("/health")
    s = api_get("/api/v1/sessions/recent?limit=1")
    return {"health": h, "latest_session": s}

def ok(m): print(f" [OK] {m}")
def warn(m): print(f" [WARN] {m}")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Session Memory Agent Hook")
    p.add_argument("command", choices=["start", "tick", "close", "status"])
    p.add_argument("--resume", help="恢复关键词")
    p.add_argument("--user", help="用户输入")
    p.add_argument("--reply", help="助手回复")
    p.add_argument("--summary", help="会话摘要")
    args = p.parse_args()

    if args.command == "start":
        start(args.resume)
    elif args.command == "tick":
        if not args.user or not args.reply:
            print(" --user 和 --reply 必填")
            sys.exit(1)
        tick(args.user, args.reply)
    elif args.command == "close":
        print(close(args.summary))
    elif args.command == "status":
        print(status())
PYEOF
chmod +x memory_hook.py

# === bash_memory_hook.sh ===
cat > bash_memory_hook.sh << 'SHEOF'
# session-memory-agent bash 钩子
# 用法: source ~/.session-memory-agent/bash_memory_hook.sh
export SMA_DIR="${HOME}/.session-memory-agent"
export SMA_SESSION_FILE="${SMA_DIR}/.session_id"

_sma_tick() {
    local cmd="$(history 1 | sed 's/^ *[0-9]* *//')"
    [ -z "$cmd" ] && return
    case "$cmd" in ls*|cd*|pwd*|clear*|exit*|cat*) return;; esac
    echo "$(date -Iseconds) ${HOSTNAME:-$(hostname)} ${cmd}" >> "${SMA_DIR}/history_sync.log"
}

if [ -z "${SMA_INIT:-}" ]; then
    export SMA_INIT=1
    nohup python3 "${SMA_DIR}/memory_hook.py" start --resume "bash-$(date +%Y%m%d)" >/dev/null 2>&1 &
fi

PROMPT_COMMAND="_sma_tick;${PROMPT_COMMAND:-}"
SHEOF
chmod +x bash_memory_hook.sh

# === shell_history_sync.py ===
cat > shell_history_sync.py << 'PYEOF'
#!/usr/bin/env python3
"""跨终端历史同步: 本机历史 → 远程 Session Memory (作为 messages)"""
import sys, os, json, time, urllib.request, urllib.error
from pathlib import Path

INSTALL_DIR = Path(__file__).parent
CONFIG = json.load(open(INSTALL_DIR / "config.json"))
API = CONFIG["session_memory_api"]
HISTORY_FILE = INSTALL_DIR / "history_sync.log"

def push_local():
    if not HISTORY_FILE.exists():
        print("无本地历史")
        return
    lines = HISTORY_FILE.read_text().strip().split("\n")
    if not lines or lines == [""]:
        print("无本地历史")
        return
    pushed = 0
    for line in lines:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        ts, host, cmd = parts
        payload = {
            "role": "user",
            "content": f"[{ts}] {host}: {cmd[:500]}"
        }
        try:
            req = urllib.request.Request(
                f"{API}/api/v1/sessions/ingest",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10)
            pushed += 1
        except Exception as e:
            print(f"推送失败: {e}")
            break
    HISTORY_FILE.write_text("")
    print(f"推送 {pushed} 条命令")

if __name__ == "__main__":
    push_local()
PYEOF
chmod +x shell_history_sync.py

# === 验证 ===
echo ""
echo "=== 验证安装 ==="
python3 "${INSTALL_DIR}/memory_hook.py" status >/dev/null 2>&1 && ok "API 连通" || err "API 不可达"
ok "脚本安装完成"
echo ""
echo "=== 使用指南 ==="
echo ""
echo "1. 初始化会话:"
echo "   python3 ${INSTALL_DIR}/memory_hook.py start"
echo ""
echo "2. 对话 tick (AI 对话后):"
echo "   python3 ${INSTALL_DIR}/memory_hook.py tick --user \"问题\" --reply \"回答\""
echo ""
echo "3. Bash 自动对接 (加到 ~/.bashrc):"
echo "   echo 'source ${INSTALL_DIR}/bash_memory_hook.sh' >> ~/.bashrc"
echo "   source ~/.bashrc"
echo ""
echo "4. 查看状态:"
echo "   python3 ${INSTALL_DIR}/memory_hook.py status"
echo ""
info "配置完成！"

# 清理测试会话
PYTEST=$(python3 -c "
import json, urllib.request
r = urllib.request.urlopen('http://100.77.184.40:8000/api/v1/sessions/recent?limit=10').read()
sessions = json.loads(r)
for s in sessions:
    if s.get('title','').startswith('test'):
        req = urllib.request.request(f\"http://100.77.184.40:8000/api/v1/sessions/{s['id']}\", method='DELETE')
        urllib.request.urlopen(req)
        print(f\"  cleaned: {s['id'][:12]}\")
" 2>/dev/null)
[ -n "$PYTEST" ] && echo "$PYTEST"
