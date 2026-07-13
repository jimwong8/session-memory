#!/usr/bin/env python3
"""Parallel embedding backfill v2 — FOR UPDATE SKIP LOCKED, no hashtext."""
import asyncio
import time
import os

os.chdir("/app")

from sqlalchemy import select, func, text as sa_text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sentence_transformers import SentenceTransformer

from src.config import settings
from src.models import Message

BATCH_SIZE = 500
ENCODE_BATCH = 256

NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "10"))

engine = create_async_engine(settings.database_url, pool_size=5)
async_session = async_sessionmaker(engine, class_=AsyncSession)


async def claim_batch(session, n):
    """Atomically claim BATCH_SIZE rows using SKIP LOCKED."""
    stmt = (
        select(Message.id, Message.content)
        .where(
            Message.embedding.is_(None),
            Message.role.in_(["user", "assistant"]),
        )
        .order_by(Message.id)
        .limit(n)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    rows = result.all()
    return rows


async def main():
    worker_id = int(os.environ.get("WORKER_ID", "0"))
    print(f"Worker {worker_id}/{NUM_WORKERS} loading model...")
    model = SentenceTransformer(settings.embedding_model)

    total_processed = 0
    start_time = time.time()
    idle_rounds = 0

    while True:
        async with async_session() as session:
            async with session.begin():
                rows = await claim_batch(session, BATCH_SIZE)

                if not rows:
                    idle_rounds += 1
                    if idle_rounds > 3:
                        print(f"Worker {worker_id}: no more work after {idle_rounds} idle rounds, exiting.")
                        break
                    await asyncio.sleep(2 * idle_rounds)
                    continue

                idle_rounds = 0
                ids = [str(r[0]) for r in rows]
                texts = [(r[1] or "")[:2000] for r in rows]

        print(f"Worker {worker_id}: encoding {len(texts)}...")
        embeddings = model.encode(
            texts, batch_size=ENCODE_BATCH, show_progress_bar=False, convert_to_numpy=True,
        )

        async with async_session() as session:
            async with session.begin():
                emb_arr = [e.tolist() for e in embeddings]
                emb_literals = []
                for e in emb_arr:
                    emb_literals.append("ARRAY[" + ",".join(str(x) for x in e) + "]::vector(384)")
                placeholders = []
                params = {}
                for i, (mid, emb) in enumerate(zip(ids, emb_literals)):
                    placeholders.append(f"(:id{i}, {emb})")
                    params[f"id{i}"] = mid
                sql = sa_text(f"""
                    UPDATE messages AS m
                    SET embedding = d.embedding
                    FROM (VALUES {",".join(placeholders)}) AS d(id, embedding)
                    WHERE m.id = d.id::uuid
                """).bindparams(**params)
                await session.execute(sql)

        total_processed += len(rows)
        elapsed = time.time() - start_time
        rate = total_processed / elapsed if elapsed > 0 else 0
        print(f"Worker {worker_id}: {total_processed} total ({rate:.1f} msg/s, {elapsed:.0f}s)")

    print(f"Worker {worker_id} done! Processed {total_processed} in {time.time()-start_time:.1f}s")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
