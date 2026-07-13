"""Context Offload - 大消息外挂到文件系统"""

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.config import settings
from src.tokenizer import count_tokens

logger = logging.getLogger(__name__)


def should_offload(content: str) -> bool:
    """判断是否应该卸载"""
    if not settings.offload_enabled:
        return False
    if not content:
        return False
    return count_tokens(content) > settings.offload_threshold_tokens


def offload_message(
    content: str,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    user_id: str = "unknown",
) -> str:
    """将大消息写入文件系统，返回包含 ref 的摘要字符串

    Returns:
        str: 替换后的 content（摘要 + 文件路径）
    """
    root = Path(settings.markdown_sync_root) / f"user={user_id}" / "refs" / str(session_id)
    root.mkdir(parents=True, exist_ok=True)

    # 写全文到文件
    ref_path = root / f"{message_id}.md"
    ref_path.write_text(content)

    # 生成摘要（取前 200 token 的内容）
    summary_tokens = count_tokens(content[:600])
    summary = content[:600] if summary_tokens > 0 else content[:200]

    # 基本信息
    total_tokens = count_tokens(content)

    replacement = (
        f"[OFFLOADED]\n"
        f"摘要: {summary[:200]}\n"
        f"原文: refs/{session_id}/{message_id}.md\n"
        f"result_ref: {message_id}\n"
        f"tokens: {total_tokens}\n"
    )

    logger.info(
        "offloaded msg=%s session=%s tokens=%d -> %s",
        message_id, session_id, total_tokens, ref_path,
    )
    return replacement


def read_offloaded_content(
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    user_id: str = "unknown",
) -> str | None:
    """从文件系统读取已卸载的原文"""
    path = (
        Path(settings.markdown_sync_root)
        / f"user={user_id}"
        / "refs"
        / str(session_id)
        / f"{message_id}.md"
    )
    if path.exists():
        return path.read_text()
    return None


def get_offload_stats(user_id: str = "unknown") -> dict:
    """获取卸载统计"""
    root = Path(settings.markdown_sync_root) / f"user={user_id}" / "refs"
    if not root.exists():
        return {"files": 0, "size_bytes": 0}

    total_size = 0
    total_files = 0
    for f in root.rglob("*.md"):
        total_size += f.stat().st_size
        total_files += 1

    return {"files": total_files, "size_bytes": total_size}
