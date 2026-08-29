"""记忆原子（L1）构建服务 - 从会话消息中抽取原子事实"""

import asyncio
import hashlib
import json
import json
import json_repair  # robust LLM-JSON repair (nested unescaped quotes)

def repair_llm_json(s: str) -> str:
    """Repair common LLM-JSON defects (unescaped inner quotes, trailing commas,
    unclosed strings) via a small state machine so json.loads can parse it."""
    out = []
    i = 0
    n = len(s)
    in_string = False
    while i < n:
        c = s[i]
        if not in_string:
            out.append(c)
            if c == '"':
                in_string = True
            i += 1
            continue
        # inside a string
        if c == '\\':
            out.append(c)
            if i + 1 < n:
                out.append(s[i + 1])
                i += 2
            else:
                i += 1
            continue
        if c == '"':
            # peek ahead skipping whitespace to find the structural closer
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            nxt = s[j] if j < n else ''
            if nxt in (',', ':', '}', ']', ''):
                out.append('"')
                in_string = False
                i += 1
                continue
            else:
                # unescaped inner quote -> escape it
                out.append('\\"')
                i += 1
                continue
        out.append(c)
        i += 1
    if in_string:
        out.append('"')
    return ''.join(out)
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import select, func, delete as sa_delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.embedding_service import create_embedding
from src.llm_client import chat_completion
from src.models import MemoryAtom, Message, Session
from src.services.pyramid.extractor_prompt import build_messages
from src.services.pyramid.triggers import TriggerEvaluator
from src.tokenizer import count_tokens, truncate_to_tokens

logger = logging.getLogger(__name__)

_ATOM_KINDS = {
    "preference", "decision", "fact", "constraint",
    "goal", "error_pattern", "task_state", "blocker",
}
_MAX_MESSAGES_PER_PASS = 50  # 单次最多送 LLM 的消息数


class AtomBuilder:
    """从会话消息中抽取记忆原子"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def pick_session(self) -> Session | None:
        """选一个需要处理的 session（有未处理消息的最长等待 session）"""
        stmt = (
            select(Session)
            .where(
                Session.message_count >= settings.pipeline_every_n_conversations,
            )
            .order_by(
                Session.pyramid_processed_at.asc().nulls_first(),
                Session.updated_at.asc(),
            )
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def process_session(self, session: Session) -> int:
        """处理一个 session：检测→抽取→去重→写入

        Returns:
            int: 实际写入的 atom 数量
        """
        # 统计已有 atom
        count_stmt = select(func.count(MemoryAtom.id)).where(
            MemoryAtom.session_id == session.id,
            MemoryAtom.superseded_by.is_(None),
        )
        count_result = await self.db.execute(count_stmt)
        existing_count: int = count_result.scalar() or 0

        # 触发判定
        decision = TriggerEvaluator(
            session_message_count=session.message_count,
            existing_atom_count=existing_count,
        ).evaluate()
        if not decision.should_run:
            logger.debug("[%s] skip: %s", session.id, decision.reason)
            session.pyramid_processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            return 0

        logger.info("[%s] %s", session.id, decision.reason)

        # 获取未处理消息
        skip = existing_count * settings.pipeline_every_n_conversations
        messages = await self._fetch_messages(
            session.id, skip=skip, limit=_MAX_MESSAGES_PER_PASS
        )
        if not messages:
            session.pyramid_processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            return 0

        # LLM 抽取
        try:
            atom_drafts = await self._extract_atoms(messages, session)
        except Exception as exc:
            logger.error("[%s] extract_atoms failed: %s", session.id, exc)
            session.pyramid_processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            return 0
        if not atom_drafts:
            session.pyramid_processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            return 0

        # 去重
        atom_drafts = await self._dedup(atom_drafts, session)

        if not atom_drafts:
            session.pyramid_processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            return 0

        # 写入
        await self._persist(atom_drafts, session)
        await self.db.commit()

        count = len(atom_drafts)
        logger.info("atom count=%d", count)
        logger.info("[%s] wrote %d atoms", session.id, count)
        return count

    async def _fetch_messages(
        self, session_id: uuid.UUID, skip: int = 0, limit: int = 50
    ) -> Sequence[Message]:
        """获取待处理的消息窗口"""
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.asc())
            .offset(skip)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def _extract_atoms(
        self, messages: Sequence[Message], session: Session
    ) -> list[dict]:
        """调用 LLM 从消息中提取 atom 草稿"""
        # 过滤掉工具调用类消息
        filtered = []
        for m in messages:
            content = (m.content or "").strip()
            if not content:
                continue
            # 跳过纯工具消息
            if content.startswith("toolstart ") or content.startswith("toolend "):
                continue
            filtered.append(m)

        if not filtered:
            return []

        # 构建消息文本
        lines = []
        for i, m in enumerate(filtered):
            role_label = "USER" if m.role == "user" else "ASSISTANT"
            content = m.content[:2000]  # 单条截断
            lines.append(f"[{i}] [{role_label}]\n{content}")
        messages_text = "\n---\n".join(lines)

        # The live pyramid LLM route (local MI50 llama.cpp :8081) clamps to
        # n_ctx=2048. Keep a hard budget well under that so extraction never
        # returns HTTP 400 (exceeds context) -> which previously forced every
        # pyramid call to fall through to the slow/overloaded GLM route.
        _MAX_INPUT_TOKENS = 1700
        _in_tokens = count_tokens(messages_text)
        if _in_tokens > _MAX_INPUT_TOKENS:
            messages_text = truncate_to_tokens(messages_text, _MAX_INPUT_TOKENS)
            logger.debug("atom input truncated %d->%d tokens", _in_tokens, _MAX_INPUT_TOKENS)

        # 构建 LLM 调用
        llm_messages = build_messages(messages_text, len(filtered))

        try:
            logger.debug("LLM call for atom extraction")
            reply, _ = await chat_completion(
                messages=llm_messages,
                temperature=0.3,
                max_tokens=4096,
                task_type="atom_build",
            )
        except Exception as exc:
            logger.warning("atom extraction failed")
            logger.error("[%s] LLM extraction failed: %s", session.id, exc)
            return []

        # 解析 JSON 响应
        return self._parse_response(reply, filtered)

    def _parse_response(
        self, reply: str, messages: Sequence[Message]
    ) -> list[dict]:
        """解析 LLM 返回的 JSON"""
        # 尝试提取 JSON 数组
        reply = reply.strip()
        # 去掉可能的 markdown 代码块标记
        if reply.startswith("```"):
            lines = reply.split("\n")
            # 去掉第一行 ``` 和最后一行 ```
            if len(lines) >= 3:
                reply = "\n".join(lines[1:-1])
            reply = reply.strip()

        try:
            items = json.loads(reply)
        except json.JSONDecodeError:
            # The LLM (even GLM) sometimes emits structurally-broken JSON:
            #   - unescaped inner double-quotes inside a string value
            #   - truncated stream (max_tokens cut mid-JSON)
            # Repair the most common defects before giving up.
            start = reply.find("[")
            if start < 0:
                logger.warning("atom extraction failed")
                logger.error("No JSON array found in response: %s...", reply[:200])
                return []
            end = reply.rfind("]")
            # Try the full (or interior-sliced) text, first as-is, then repaired.
            candidates = []
            if end > start:
                candidates.append(reply[start : end + 1])
            candidates.append(repair_llm_json(reply))
            if end > start:
                candidates.append(repair_llm_json(reply[start : end + 1]))
            # Truncated-array fallback: wrap the last complete {...} object.
            last_obj = reply.rfind("}")
            if last_obj > start:
                candidates.append(reply[start : last_obj + 1].rstrip(",") + "]")
                candidates.append(repair_llm_json(reply[start : last_obj + 1].rstrip(",") + "]"))
            for cand in candidates:
                try:
                    items = json.loads(cand)
                    break
                except (json.JSONDecodeError, ValueError):
                    continue
            else:
                # FINAL recovery: json_repair handles nested unescaped inner
                # quotes (e.g. content containing os.environ.get("X", "y"))
                # which the hand-rolled repair above cannot fix. This was the
                # root cause of 0-atom extractions in production.
                try:
                    items = json_repair.repair_json(reply, return_objects=True)
                except Exception as exc:  # pragma: no cover
                    logger.error("json_repair failed: %s", exc)
                    items = None
                if not isinstance(items, list):
                    logger.warning("atom extraction failed")
                    logger.error("JSON parse failed (recovery exhausted): %s...", reply[:200])
                    return []

        if not isinstance(items, list):
            return []

        drafts = []
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type") or item.get("kind") or ""
            if kind not in _ATOM_KINDS:
                continue
            content = (item.get("content") or "").strip()
            if len(content) < 5:
                continue
            title = (item.get("title") or "")[:500]
            tags = item.get("tags") or []
            if isinstance(tags, list):
                tags = [str(t)[:100] for t in tags]
            else:
                tags = []

            # 找到对应的 source message
            source_idx = item.get("source_message_index")
            source_ids = []
            if source_idx is not None:
                try:
                    idx = int(source_idx)
                    if 0 <= idx < len(messages):
                        source_ids.append(str(messages[idx].id))
                except (ValueError, IndexError):
                    pass

            drafts.append({
                "kind": kind,
                "title": title,
                "content": content,
                "tags": tags,
                "source_message_ids": source_ids,
            })

        return drafts

    async def _dedup(
        self, drafts: list[dict], session: Session
    ) -> list[dict]:
        """对 draft 做两层去重：精确签名 + 向量相似度"""
        if not settings.extraction_enable_dedup or not drafts:
            return drafts

        # 计算 dedup signature
        for d in drafts:
            normalized = (d["content"] or "").strip().lower()
            d["dedup_signature"] = hashlib.sha256(
                normalized.encode("utf-8")
            ).hexdigest()[:12]

        # 查询 session 内已有 atom 的 signature
        stmt = select(MemoryAtom.dedup_signature).where(
            MemoryAtom.session_id == session.id,
            MemoryAtom.superseded_by.is_(None),
            MemoryAtom.dedup_signature.isnot(None),
        )
        result = await self.db.execute(stmt)
        existing_signatures = {row[0] for row in result if row[0]}

        # layer-1: 精确去重
        filtered = []
        for d in drafts:
            sig = d.get("dedup_signature", "")
            if sig in existing_signatures:
                
                logger.debug("dedup sig hit: %s", sig)
                continue
            filtered.append(d)

        if not filtered:
            return []

        # layer-2: 向量去重（cosine > threshold 视为重复）
        if len(filtered) > 1:
            threshold = settings.extraction_dedup_cosine_threshold
            texts = [d["content"] for d in filtered]
            try:
                embeddings = await asyncio.gather(
                    *[create_embedding(t) for t in texts]
                )
            except Exception as exc:
                logger.warning("vector dedup failed, fallback to exact only: %s", exc)
                return filtered

            # 也要与已有 atom 向量比对
            exist_stmt = select(MemoryAtom.embedding, MemoryAtom.content).where(
                MemoryAtom.session_id == session.id,
                MemoryAtom.superseded_by.is_(None),
                MemoryAtom.embedding.isnot(None),
            )
            exist_result = await self.db.execute(exist_stmt)
            existing_embeddings = [
                (row[0], row[1]) for row in exist_result if row[0] is not None
            ]

            keep = []
            for i, (draft, emb) in enumerate(zip(filtered, embeddings)):
                if emb is None:
                    keep.append(draft)
                    continue
                try:
                    emb_arr = np.array(emb, dtype=np.float32).flatten()
                except Exception:
                    continue

                # 跟新草稿的其他候选比较
                is_dup = False
                for j in range(i):
                    if j >= len(embeddings) or embeddings[j] is None:
                        continue
                    other = np.array(embeddings[j], dtype=np.float32)
                    cos = float(np.dot(emb_arr, other) / (
                        np.linalg.norm(emb_arr) * np.linalg.norm(other) + 1e-10
                    ))
                    if cos > threshold:
                        
                        logger.debug(
                            "dedup vector hit (new): %.4f <=> %s", cos, draft["content"][:50]
                        )
                        is_dup = True
                        break

                if is_dup:
                    continue

                # 跟已有 DB atom 比较
                for exist_emb, exist_content in existing_embeddings:
                    if exist_emb is None:
                        continue
                    other = np.array(exist_emb, dtype=np.float32)
                    cos = float(np.dot(emb_arr, other) / (
                        np.linalg.norm(emb_arr) * np.linalg.norm(other) + 1e-10
                    ))
                    if cos > threshold:
                        
                        logger.debug(
                            "dedup vector hit (existing): %.4f <=> %s",
                            cos, exist_content[:50] if exist_content else "",
                        )
                        is_dup = True
                        break

                if not is_dup:
                    keep.append(draft)

            return keep

        return filtered

    async def _persist(self, drafts: list[dict], session: Session) -> None:
        """批量写入 atom"""
        # 批量获取嵌入
        texts = [d["content"] for d in drafts]
        try:
            embeddings = await asyncio.gather(
                *[create_embedding(t) for t in texts]
            )
        except Exception as exc:
            logger.warning("embedding failed during persist, writing without vector: %s", exc)
            embeddings = [None] * len(drafts)

        atoms = []
        for draft, emb in zip(drafts, embeddings):
            source_ids = draft.get("source_message_ids", [])
            uuid_ids = []
            for sid in source_ids:
                try:
                    uuid_ids.append(uuid.UUID(sid))
                except (ValueError, AttributeError):
                    pass

            atom = MemoryAtom(
                id=uuid.uuid4(),
                session_id=session.id,
                user_id=session.user_id,
                kind=draft["kind"],
                title=draft.get("title"),
                content=draft["content"],
                tags=draft.get("tags", []),
                source_message_ids=uuid_ids,
                embedding=emb,
                dedup_signature=draft.get("dedup_signature"),
            )
            atoms.append(atom)

        self.db.add_all(atoms)
