"""
KG Write Queue — serialized write operations on KG entities/relations.

Resolves C3 conflict (write path): sync_turn, Pyramid Worker, and MCP KG 
atomic ops all write to KG simultaneously. The queue serializes writes
to prevent lost updates and race conditions.

Design: single background thread + asyncio Queue. Writers put ops;
the worker executes them sequentially against PG.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class KGOpType(Enum):
    EXTRACT = "extract"           # Pyramid Worker bulk extract
    UPSERT_ENTITY = "upsert_ent"  # Single entity upsert
    DELETE_ENTITY = "delete_ent"  # Entity delete (cascade)
    MERGE_ENTITY = "merge_ent"    # Merge two entities
    UPDATE_RELATION = "upd_rel"   # Relation update


@dataclass
class KGOp:
    op_type: KGOpType
    payload: dict
    future: Optional[asyncio.Future] = None
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.monotonic()


class KGWriteQueue:
    """Serializes KG write operations through a single async worker."""

    def __init__(self, maxsize: int = 1000):
        self._queue: asyncio.Queue[KGOp] = asyncio.Queue(maxsize=maxsize)
        self._worker_task: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._processed = 0
        self._dropped = 0

    def start(self):
        """Start the background worker."""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.new_event_loop()
        self._worker_task = threading.Thread(target=self._run_loop, daemon=True)
        self._worker_task.start()
        logger.info("KG Write Queue started")

    def _run_loop(self):
        """Run the asyncio event loop in a dedicated thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._worker())

    async def _worker(self):
        """Process ops sequentially."""
        from src.services.knowledge_graph import KnowledgeGraphService
        from src.database import async_session

        while self._running:
            try:
                op = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                async with async_session() as session:
                    svc = KnowledgeGraphService(session)
                    result = await self._execute_op(session, svc, op)
                    await session.commit()
                    if op.future and not op.future.done():
                        self._loop.call_soon_threadsafe(op.future.set_result, result)
                self._processed += 1
            except Exception as e:
                logger.warning(f"KG write queue error ({op.op_type.value}): {e}")
                if op.future and not op.future.done():
                    self._loop.call_soon_threadsafe(op.future.set_exception, e)
            finally:
                self._queue.task_done()

    async def _execute_op(self, session, svc, op: KGOp):
        """Execute a single KG operation."""
        if op.op_type == KGOpType.EXTRACT:
            from src.models import Message
            msg_id = op.payload.get("message_id")
            if not msg_id:
                return {"ok": False, "reason": "missing message_id"}
            msg = await session.get(Message, msg_id)
            if not msg:
                return {"ok": False, "reason": "message_not_found"}
            return await svc.extract_knowledge_result(msg)

        elif op.op_type == KGOpType.UPSERT_ENTITY:
            from src.models import KGEntity
            ent = KGEntity(
                session_id=op.payload["session_id"],
                name=op.payload["name"],
                entity_type=op.payload.get("entity_type", "concept"),
                level=op.payload.get("level", "L1"),
            )
            session.add(ent)
            await session.flush()
            return {"ok": True, "entity_id": str(ent.id)}

        elif op.op_type == KGOpType.DELETE_ENTITY:
            from src.models import KGEntity
            ent = await session.get(KGEntity, op.payload["entity_id"])
            if ent:
                await session.delete(ent)
                return {"ok": True, "deleted": str(ent.id)}
            return {"ok": False, "reason": "entity_not_found"}

        elif op.op_type == KGOpType.MERGE_ENTITY:
            # Merge: move all relations from source to target, then delete source
            from src.models import KGEntity, KGRelation
            src_id = op.payload["source_entity_id"]
            tgt_id = op.payload["target_entity_id"]
            # Update relations pointing to source
            await session.execute(
                KGRelation.__table__.update()
                .where(KGRelation.source_entity_id == src_id)
                .values(source_entity_id=tgt_id)
            )
            await session.execute(
                KGRelation.__table__.update()
                .where(KGRelation.target_entity_id == src_id)
                .values(target_entity_id=tgt_id)
            )
            src = await session.get(KGEntity, src_id)
            if src:
                await session.delete(src)
            return {"ok": True, "merged_into": str(tgt_id)}

        elif op.op_type == KGOpType.UPDATE_RELATION:
            from src.models import KGRelation
            rel = await session.get(KGRelation, op.payload["relation_id"])
            if rel and "relation_type" in op.payload:
                rel.relation_type = op.payload["relation_type"]
                rel.version = (rel.version or 1) + 1
                return {"ok": True}
            return {"ok": False, "reason": "relation_not_found"}

        return {"ok": False, "reason": "unknown_op"}

    def submit(self, op: KGOp, wait: bool = False, timeout: float = 5.0):
        """Submit an op to the queue. If wait=True, block until done."""
        if not self._running:
            self.start()

        if wait:
            import concurrent.futures
            fut = asyncio.Future()
            op.future = fut

        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, op)
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning(f"KG write queue full, dropping {op.op_type.value}")
            return None

        if wait:
            # Convert future to concurrent.future for blocking wait
            import concurrent.futures
            cf = concurrent.futures.Future()
            def _done(f):
                if f.exception():
                    cf.set_exception(f.exception())
                else:
                    cf.set_result(f.result())
            fut.add_done_callback(_done)
            try:
                return cf.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                return None
        return True

    def stop(self):
        """Graceful shutdown."""
        self._running = False
        if self._worker_task:
            self._worker_task.join(timeout=10)

    @property
    def stats(self) -> dict:
        return {
            "processed": self._processed,
            "dropped": self._dropped,
            "pending": self._queue.qsize() if self._running else 0,
        }


# Global singleton
kg_write_queue = KGWriteQueue()
