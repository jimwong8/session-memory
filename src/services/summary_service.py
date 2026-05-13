"""摘要服务 - 自动压缩会话历史"""

import logging
import time
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.cache import cache
from src.config import settings
from src.llm_client import generate_summary
from src.metrics import SUMMARY_DURATION, SUMMARY_FAIL_COUNT, SUMMARY_SUCCESS_COUNT
from src.models import Message, Summary
from src.tokenizer import count_tokens

logger = logging.getLogger(__name__)


class SummaryService:
    """管理会话摘要的生成和检索"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_latest_summary(self, session_id: uuid.UUID) -> Summary | None:
        cached_text = await cache.get_summary(session_id)
        if cached_text:
            return Summary(
                session_id=session_id,
                content=cached_text,
                message_range_start=0,
                message_range_end=0,
                tokens=count_tokens(cached_text),
            )

        stmt = (
            select(Summary)
            .where(Summary.session_id == session_id)
            .order_by(Summary.created_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        summary = result.scalar_one_or_none()

        if summary:
            await cache.set_summary(session_id, summary.content)

        return summary

    async def should_generate_summary(self, session_id: uuid.UUID) -> bool:
        latest = await self.get_latest_summary(session_id)
        last_end = latest.message_range_end if latest else 0

        stmt = (
            select(func.count())
            .select_from(Message)
            .where(Message.session_id == session_id)
        )
        result = await self.db.execute(stmt)
        total_count = result.scalar() or 0

        new_messages = total_count - last_end
        return new_messages >= settings.summary_trigger_count

    async def get_summary_status(self, session_id: uuid.UUID) -> dict:
        latest = await self.get_latest_summary(session_id)
        return {
            "summary_enabled": settings.enable_auto_summary,
            "latest_summary_exists": latest is not None,
            "latest_summary_time": latest.created_at.isoformat() if latest and getattr(latest, "created_at", None) else None,
            "latest_summary_tokens": latest.tokens if latest else 0,
            "latest_summary_range_start": latest.message_range_start if latest else None,
            "latest_summary_range_end": latest.message_range_end if latest else None,
        }

    async def generate_and_save_summary(self, session_id: uuid.UUID) -> Summary | None:
        lock_key = f"summary:{session_id}"
        if not await cache.acquire_lock(lock_key, ttl=60):
            logger.info("摘要生成已在进行中: %s", session_id)
            return None

        start_time = time.time()
        try:
            latest = await self.get_latest_summary(session_id)
            start_from = latest.message_range_end if latest else 0

            stmt = (
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.created_at)
                .offset(start_from)
            )
            result = await self.db.execute(stmt)
            messages = list(result.scalars().all())

            if not messages:
                return None

            # 分块生成摘要（避免超过 LLM context limit，每批最多 50 条消息）
            MAX_MSGS_PER_CHUNK = 20
            chunks = [messages[i:i+MAX_MSGS_PER_CHUNK] for i in range(0, len(messages), MAX_MSGS_PER_CHUNK)]

            chunk_summaries: list[str] = []
            if latest:
                chunk_summaries.append(f"[之前的摘要]: {latest.content}")

            for chunk in chunks:
                parts: list[str] = []
                if chunk_summaries:
                    parts.append(f"[已有摘要]: {chunk_summaries[-1]}")
                for msg in chunk:
                    parts.append(f"[{msg.role}]: {msg.content}")
                chunk_text = "\n".join(parts)
                chunk_summary = await generate_summary(chunk_text)
                chunk_summaries.append(chunk_summary)

            # 多分块时再做合并摘要
            if len(chunk_summaries) > 1:
                final_parts = ["以下是各段落的摘要，请综合生成最终摘要："]
                for i, cs in enumerate(chunk_summaries):
                    final_parts.append(f"[第{i+1}段]: {cs}")
                summary_text = await generate_summary("\n".join(final_parts))
            else:
                summary_text = chunk_summaries[0] if chunk_summaries else ""
            summary_tokens = count_tokens(summary_text)

            total_end = start_from + len(messages)
            summary = Summary(
                session_id=session_id,
                content=summary_text,
                message_range_start=start_from,
                message_range_end=total_end,
                tokens=summary_tokens,
            )
            self.db.add(summary)
            await self.db.commit()
            await self.db.refresh(summary)

            await cache.set_summary(session_id, summary_text)

            duration = time.time() - start_time
            SUMMARY_DURATION.observe(duration)
            SUMMARY_SUCCESS_COUNT.inc()
            logger.info(
                "摘要已生成: session=%s, range=[%d, %d], tokens=%d, duration=%.2fs",
                session_id,
                start_from,
                total_end,
                summary_tokens,
                duration,
            )
            return summary

        except Exception:
            duration = time.time() - start_time
            SUMMARY_DURATION.observe(duration)
            SUMMARY_FAIL_COUNT.inc()
            logger.exception("摘要生成失败: %s", session_id)
            return None
        finally:
            await cache.release_lock(lock_key)
