#!/usr/bin/env python3
"""Memory-optimized backfill embeddings."""
import asyncio
import time
import os
import gc
os.chdir("/app")

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sentence_transformers import SentenceTransformer

from src.config import settings
from src.models import Message

BATCH_SIZE = 100       # Smaller batches to reduce memory
ENCODE_BATCH = 16      # Smaller encode batches
MAX_MESSAGES = int(os.environ.get("BACKFILL_MAX", "100000"))

engine = create_async_engine(settings.database_url, pool_size=3)
async_session = async_sessionmaker(engine, class_=AsyncSession)


async def main():
    print(f"Loading model: {settings.embedding_model}")
    model = SentenceTransformer(settings.embedding_model)

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

            ids = [r[0] for r in rows]
            texts = [r[1][:2000] for r in rows]

        # Encode and immediately write back one batch at a time
        embeddings = model.encode(
            texts, batch_size=ENCODE_BATCH, show_progress_bar=False, convert_to_numpy=True,
        )

        async with async_session() as session:
            for msg_id, emb in zip(ids, embeddings):
                await session.execute(
                    update(Message).where(Message.id == msg_id).values(embedding=emb.tolist())
                )
            await session.commit()

        total_processed += len(rows)
        elapsed = time.time() - start_time
        rate = total_processed / elapsed if elapsed > 0 else 0
        
        # Free memory explicitly
        del embeddings, texts, ids, rows
        gc.collect()
        
        print(f"  Progress: {total_processed}/{MAX_MESSAGES} ({rate:.0f} msg/s) [mem-safe]")

    elapsed = time.time() - start_time
    print(f"\nDone! Processed {total_processed} messages in {elapsed:.1f}s")

    async with async_session() as session:
        total = (await session.execute(select(func.count()).select_from(Message))).scalar()
        with_emb = (await session.execute(
            select(func.count()).where(Message.embedding.isnot(None))
        )).scalar()
        print(f"Total: {total}, with embedding: {with_emb} ({100*with_emb/total:.1f}%)")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
