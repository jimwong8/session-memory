#!/usr/bin/env python3
"""修复 OpenCode 本地 sqlite 中悬挂的 assistant 消息。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".local/share/opencode/opencode.db"
DEFAULT_SESSIONS = (
    "ses_2479a63f1ffet3ndPDNT7N3J0I",
    "ses_245759a46ffevVdipknF1qbrs7",
)
REPAIR_ERROR_NAME = "RecoveredStuckMessageError"
REPAIR_SCRIPT = "repair_stuck_opencode_sessions.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将卡住的 assistant 消息收尾为显式错误终态。",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"sqlite 数据库路径，默认 {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--session",
        dest="sessions",
        action="append",
        help="要修复的 session_id；可重复传入。默认修复已定位的两个卡死会话。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写入数据库；默认仅 dry-run。",
    )
    return parser.parse_args()


def now_ms() -> int:
    return int(time.time() * 1000)


def load_candidates(conn: sqlite3.Connection, sessions: tuple[str, ...]) -> list[sqlite3.Row]:
    placeholders = ", ".join("?" for _ in sessions)
    query = f"""
        SELECT id, session_id, time_created, time_updated, data
        FROM message
        WHERE session_id IN ({placeholders})
        ORDER BY session_id, time_created ASC
    """
    conn.row_factory = sqlite3.Row
    return list(conn.execute(query, sessions))


def is_dangling_assistant(payload: dict[str, Any]) -> bool:
    if payload.get("role") != "assistant":
        return False
    error = payload.get("error")
    finish = payload.get("finish")
    completed = ((payload.get("time") or {}).get("completed"))
    return error is None and finish is None and completed is None


def repair_payload(
    payload: dict[str, Any],
    *,
    repaired_at_ms: int,
    row_time_created: int,
    row_time_updated: int,
) -> dict[str, Any]:
    updated = json.loads(json.dumps(payload))
    time_info = updated.get("time")
    if not isinstance(time_info, dict):
        time_info = {}
        updated["time"] = time_info
    time_info["completed"] = repaired_at_ms
    updated["finish"] = "error"
    updated["error"] = {
        "name": REPAIR_ERROR_NAME,
        "data": {
            "message": "Assistant message was left without completed/error/finish and was finalized by repair script.",
            "repair_script": REPAIR_SCRIPT,
            "repaired_at_ms": repaired_at_ms,
            "original_time_created": row_time_created,
            "original_time_updated": row_time_updated,
        },
    }
    updated["repair"] = {
        "applied_by": REPAIR_SCRIPT,
        "applied_at_ms": repaired_at_ms,
        "reason": "Finalize dangling assistant message",
    }
    return updated


def summarize(rows: list[sqlite3.Row]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        grouped[str(row["session_id"])].append(str(row["id"]))
    return dict(grouped)


def main() -> int:
    args = parse_args()
    db_path = args.db_path.expanduser().resolve()
    sessions = tuple(args.sessions or DEFAULT_SESSIONS)

    if not db_path.exists():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        rows = load_candidates(conn, sessions)
        repairs: list[tuple[str, int, str]] = []
        repaired_at = now_ms()

        for row in rows:
            try:
                payload = json.loads(row["data"])
            except json.JSONDecodeError as exc:
                print(f"跳过损坏 JSON: {row['id']} ({exc})", file=sys.stderr)
                continue

            if not is_dangling_assistant(payload):
                continue

            updated_payload = repair_payload(
                payload,
                repaired_at_ms=repaired_at,
                row_time_created=int(row["time_created"]),
                row_time_updated=int(row["time_updated"]),
            )
            repairs.append((str(row["id"]), repaired_at, json.dumps(updated_payload, ensure_ascii=False, separators=(",", ":"))))

        if not repairs:
            print("没有发现需要修复的悬挂 assistant 消息。")
            return 0

        by_session = summarize([row for row in rows if str(row["id"]) in {item[0] for item in repairs}])
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] 将修复 {len(repairs)} 条消息")
        for session_id, message_ids in by_session.items():
            print(f"  - {session_id}: {len(message_ids)} 条")
            for message_id in message_ids:
                print(f"    * {message_id}")

        if not args.apply:
            return 0

        conn.execute("BEGIN")
        conn.executemany(
            "UPDATE message SET data = ?, time_updated = ? WHERE id = ?",
            [(payload, repaired_at_ms, message_id) for message_id, repaired_at_ms, payload in repairs],
        )
        conn.commit()
        print("修复已写入数据库。")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
