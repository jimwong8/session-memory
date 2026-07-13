#!/usr/bin/env python3
"""Parallel embedding backfill worker using partition by id hash."""
import asyncio
import time
import os
import sys

os.chdir("/app")

from sqlalchemy import select, update, func, text as sa_text, String
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sentence_transformers import SentenceTransformer

from src.config import settings
from src.models import Message

BATCH_SIZE = 500
ENCODE_BATCH = 256

WORKER_ID = int(os.environ.get("WORKER_ID", "0"))
NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "4"))
MAX_MESSAGES = int(os.environ.get("BACKFILL_MAX", "220000"))

engine = create_async_engine(settings.database_url, pool_size=3)
async_session = async_sessionmaker(engine, class_=AsyncSession)


async def main():
    print(f"Worker {WORKER_ID}/{NUM_WORKERS} loading model...")
    model = SentenceTransformer(settings.embedding_model)

    total_processed = 0
    start_time = time.time()

    while total_processed < MAX_MESSAGES // NUM_WORKERS:
        async with async_session() as session:
            stmt = (
                select(Message.id, Message.content)
                .where(
                    Message.embedding.is_(None),
                    Message.role.in_(["user", "assistant"]),
                    # hashtextmod for partitioning
                    func.abs(func.hashtext(func.cast(Message.id, String))) % NUM_WORKERS == WORKER_ID,
                )
                .order_by(Message.created_at.desc())
                .limit(BATCH_SIZE)
            )
            result = await session.execute(stmt)
            rows = result.all()

            if not rows:
                print(f"Worker {WORKER_ID}: no more messages.")
                break

            ids = [str(r[0]) for r in rows]
            texts = [(r[1] or "")[:2000] for r in rows]

        print(f"Worker {WORKER_ID}: encoding batch of {len(texts)}...")
        embeddings = model.encode(
            texts, batch_size=ENCODE_BATCH, show_progress_bar=False, convert_to_numpy=True,
        )

        async with async_session() as session:
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
            await session.commit()

        total_processed += len(rows)
        elapsed = time.time() - start_time
        rate = total_processed / elapsed if elapsed > 0 else 0
        print(f"Worker {WORKER_ID}: {total_processed} ({rate:.1f} msg/s, {elapsed:.0f}s)")

    print(f"Worker {WORKER_ID} done! Processed {total_processed} in {time.time()-start_time:.1f}s")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
