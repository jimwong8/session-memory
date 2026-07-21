#!/usr/bin/env python3
"""Final embedding backfill - ORM-based, memory-safe."""
import asyncio
import gc
import os
import sys
import time

os.chdir("/app")
sys.path.insert(0, "/app")

from sqlalchemy import func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sentence_transformers import SentenceTransformer

from src.config import settings
from src.models import Message

MODEL_PATH = os.environ.get("EMBEDDING_MODEL", settings.embedding_model)
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "30"))
ENCODE_BATCH = int(os.environ.get("ENCODE_BATCH", "16"))
MAX_MESSAGES = int(os.environ.get("BACKFILL_MAX", "100000"))

engine = create_async_engine(settings.database_url, pool_size=2)
async_session = async_sessionmaker(engine, class_=AsyncSession)


async def main():
    print(f"Loading model: {MODEL_PATH}")
    model = SentenceTransformer(MODEL_PATH)
    dim = model.get_sentence_embedding_dimension()
    print(f"Model dim: {dim}")

    total_processed = 0
    start_time = time.time()

    while total_processed < MAX_MESSAGES:
        async with async_session() as session:
            stmt = (
                select(Message.id, Message.content)
                .where(
                    Message.embedding.is_(None),
                    Message.role.in_(["user", "assistant"]),
                )
                .order_by(Message.created_at.desc())
                .limit(BATCH_SIZE)
            )
            result = await session.execute(stmt)
            rows = result.all()

            if not rows:
                print("No more messages without embeddings.")
                break

            ids = [str(r[0]) for r in rows]
            texts = [(r[1] or "")[:2000] for r in rows]

        print(f"  Encoding batch of {len(texts)} texts...")
        embeddings = model.encode(
            texts, batch_size=ENCODE_BATCH, show_progress_bar=False, convert_to_numpy=True,
        )

        async with async_session() as session:
            for msg_id, emb in zip(ids, embeddings):
                await session.execute(
                    sa_update(Message)
                    .where(Message.id == msg_id)
                    .values(embedding=emb.tolist())
                )
            await session.commit()

        total_processed += len(rows)
        elapsed = time.time() - start_time
        rate = total_processed / max(elapsed, 0.1)
        remaining = MAX_MESSAGES - total_processed
        eta = remaining / max(rate, 0.1)
        print(f"  Progress: {total_processed}/{MAX_MESSAGES} ({rate:.0f} msg/s, ETA {eta/60:.0f}m)")

        del embeddings, texts, ids, rows
        gc.collect()

    elapsed = time.time() - start_time
    print(f"\nDone! Processed {total_processed} messages in {elapsed:.0f}s ({elapsed/60:.1f}m)")

    async with async_session() as session:
        total = (await session.execute(select(func.count()).select_from(Message))).scalar()
        with_emb = (await session.execute(
            select(func.count()).where(Message.embedding.isnot(None))
        )).scalar()
        pct = 100 * with_emb / max(total, 1)
        print(f"Total: {total}, with embedding: {with_emb} ({pct:.1f}%)")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
