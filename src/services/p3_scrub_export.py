"""P3 收口：脱敏 + portable export"""

import fnmatch
import hashlib
import json
import logging
import os
import re
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import IO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models import MemoryAtom, Persona

logger = logging.getLogger(__name__)

# ── P3-2: 脱敏规则 ──

_SENSITIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*\S+'), r'\1=<REDACTED:password>'),
    (re.compile(r'(?i)(api[_-]?key|apikey)\s*[=:]\s*\S+'), r'\1=<REDACTED:api_key>'),
    (re.compile(r'(?i)(token|auth_token)\s*[=:]\s*\S+'), r'\1=<REDACTED:token>'),
    (re.compile(r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----'), '<REDACTED:private_key>'),
    (re.compile(r'(?i)(secret|client_secret)\s*[=:]\s*\S+'), r'\1=<REDACTED:secret>'),
    (re.compile(r'(?i)(cookie|session_id)\s*[=:]\s*\S+'), r'\1=<REDACTED:cookie>'),
]


def scrub_text(text: str) -> tuple[str, bool]:
    """脱敏文本，返回(clean_text, was_scrubbed)"""
    if not text:
        return text, False
    scrubbed = False
    result = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        new_result = pattern.sub(replacement, result)
        if new_result != result:
            scrubbed = True
            result = new_result
    return result, scrubbed


async def scrub_atom_content(db: AsyncSession) -> int:
    """扫描已有 atom 并脱敏"""
    stmt = select(MemoryAtom).where(MemoryAtom.sensitivity_level == "normal")
    result = await db.execute(stmt)
    atoms = result.scalars().all()

    count = 0
    for atom in atoms:
        clean, was_scrubbed = scrub_text(atom.content)
        if was_scrubbed:
            atom.content = clean
            atom.sensitivity_level = "high"
            count += 1

    if count > 0:
        await db.commit()
        logger.info("scrubbed %d atoms", count)
    return count


# ── P3-1: capture exclude ──

def should_exclude_agent(agent_name: str | None) -> bool:
    """检查 agent 是否在排除列表中"""
    if not agent_name or not settings.capture_exclude_agents:
        return False
    patterns = [p.strip() for p in settings.capture_exclude_agents.split(",")]
    return any(fnmatch.fnmatch(agent_name, p) for p in patterns)


# ── P3-3: portable export ──

async def export_user_memory(
    db: AsyncSession,
    user_id: str,
    output: IO[bytes],
) -> int:
    """导出用户记忆到 tar.gz"""
    from src.services.markdown_sync import sync_user_memory
    await sync_user_memory(db, user_id)

    root = Path(settings.markdown_sync_root) / f"user={user_id}"
    if not root.exists():
        return 0

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(str(root), arcname=f"user={user_id}")

    with open(tmp_path, "rb") as f:
        output.write(f.read())
    os.unlink(tmp_path)

    file_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
    logger.info("exported %s memory to tar.gz (%d bytes)", user_id, file_size)
    return file_size


async def import_user_memory(
    db: AsyncSession,
    user_id: str,
    data: bytes,
) -> int:
    """导入用户记忆（恢复文件 + 重建索引待后续）"""
    root = Path(settings.markdown_sync_root) / f"user={user_id}"
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(data)

    with tarfile.open(tmp_path, "r:gz") as tar:
        tar.extractall(path=root.parent)

    os.unlink(tmp_path)

    # 读回去（全量重建 DB 索引在这里暂不实现）
    files = len(list(root.rglob("*")))
    logger.info("imported %s memory: %d files", user_id, files)
    return files
