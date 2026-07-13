"""SM → CSS bridge - 把 session-memory 的 message 写入 fire-and-forget 到 CSS spool

放在 SM 这边 (src/services/css_bridge.py)，因为 collector spool 由 SM 主动写，
collector 负责异步消费。
"""
from __future__ import annotations

import asyncio
import hashlib
import fcntl
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 配置：SM 写入 collector spool 目录，与 collector 配置共用
_SPOOL_DIR = Path(os.environ.get("CSS_BRIDGE_SPOOL_DIR", "/app/data/collector-spool"))
_TERMINAL_ID = os.environ.get("CSS_BRIDGE_TERMINAL_ID", "session-memory-bridge")
_HOSTNAME = os.environ.get("CSS_BRIDGE_HOSTNAME", "10.100.1.13")
_BRIDGE_ENABLED = os.environ.get("CSS_BRIDGE_ENABLED", "true").lower() == "true"
_MAX_PENDING = int(os.environ.get("CSS_BRIDGE_MAX_PENDING", "50"))
_SEQ_STATE_DIR = Path(os.environ.get("CSS_BRIDGE_STATE_DIR", "/app/data/collector-state"))
_SEQ_MAP_FILE = _SEQ_STATE_DIR / "seq-map.json"


def _next_seq(session_id: str) -> int:
    """Per-session monotonic seq allocator with file lock."""
    _SEQ_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _SEQ_MAP_FILE.touch(exist_ok=True)
    with open(_SEQ_MAP_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = f.read().strip()
            state = json.loads(raw) if raw else {}
            cur = int(state.get(session_id, 0))
            nxt = cur + 1
            state[session_id] = nxt
            f.seek(0)
            f.truncate(0)
            f.write(json.dumps(state))
            f.flush()
            return nxt
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)



def is_enabled() -> bool:
    return _BRIDGE_ENABLED


async def enqueue_message(
    *,
    message_id: str,
    session_id: str,
    role: str,
    content: str,
    event_seq: int,
    user_id: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> None:
    """把一条 message 异步写到 collector spool。

    注意：fire-and-forget，失败只 log，不影响 SM 主链路。
    """
    if not _BRIDGE_ENABLED:
        return

    # 降噪与限流保护：仅桥接用户/助手消息，跳过系统/tool 事件
    if role not in {"user", "assistant"}:
        return

    try:
        await asyncio.to_thread(
            _write_spool_sync,
            message_id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            event_seq=_next_seq(session_id),
            user_id=user_id,
            metadata_json=metadata_json or {},
        )
    except Exception as exc:
        logger.warning("css_bridge enqueue failed (non-blocking): %s", exc)


def _write_spool_sync(
    *,
    message_id: str,
    session_id: str,
    role: str,
    content: str,
    event_seq: int,
    user_id: str | None,
    metadata_json: dict[str, Any],
) -> None:
    """同步写 spool（被 to_thread 调用，不阻塞事件循环）"""
    pending = _SPOOL_DIR / "pending"
    pending.mkdir(parents=True, exist_ok=True)

    # 限流门控：pending 堆积超过阈值时主动丢弃
    pcount = len(list(pending.glob("*.json")))
    if pcount > _MAX_PENDING:
        logger.warning("bridge throttle: pending=%d > max=%d, drop session=%s seq=%d",
                       pcount, _MAX_PENDING, session_id, event_seq)
        return

    # 推导 event_type
    event_type = "message"
    if metadata_json.get("event") == "tool.execute.before":
        event_type = "tool_start"
    elif metadata_json.get("event") == "tool.execute.after":
        event_type = "tool_end"
    elif content.startswith("[tool:start]"):
        event_type = "tool_start"
    elif content.startswith("[tool:end]"):
        event_type = "tool_end"

    content_hash = hashlib.sha256(
        f"{message_id}|{role}|{content}".encode("utf-8")
    ).hexdigest()[:32]

    project_key = (
        metadata_json.get("project_key")
        or metadata_json.get("opencode_project_id")
        or user_id
        or "default"
    )

    event = {
        "id": str(message_id),
        "session_id": str(session_id),
        "event_seq": int(event_seq),
        "terminal_id": _TERMINAL_ID,
        "project_key": str(project_key)[:255],
        "event_type": event_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash,
        "hostname": _HOSTNAME,
        "role": role,
        "content_text": content[:10000],
        "content_json": _safe_json(metadata_json),
        "tool_name": metadata_json.get("tool_name") if event_type.startswith("tool") else None,
        "tool_status": metadata_json.get("tool_status") if event_type.startswith("tool") else None,
        "sensitivity_level": metadata_json.get("sensitivity_level", "normal"),
        "tags": metadata_json.get("tags", []) if isinstance(metadata_json.get("tags"), list) else [],
    }

    envelope = {
        "state": "pending",
        "attempt_count": 0,
        "next_retry_at": None,
        "event": event,
    }

    fname = f"{session_id}_{event_seq:08d}_{str(message_id)[:8]}.json"
    target = pending / fname
    # 原子写入：写到 .tmp 再 rename
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=False, default=str))
    tmp.rename(target)


def _safe_json(d: Any) -> dict:
    """确保 JSON-serializable"""
    if not isinstance(d, dict):
        return {}
    try:
        json.dumps(d, default=str)
        return d
    except Exception:
        return {}
