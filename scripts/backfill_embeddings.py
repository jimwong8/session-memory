#!/usr/bin/env python3
"""Optimized bulk embedding backfill using unnest for batch updates."""
import asyncio
import time
import os
os.chdir("/app")

from sqlalchemy import select, text as sa_text, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sentence_transformers import SentenceTransformer

from src.config import settings
from src.models import Message

BATCH_SIZE = 500
ENCODE_BATCH = 256
MAX_MESSAGES = int(os.environ.get("BACKFILL_MAX", "220000"))

engine = create_async_engine(settings.database_url, pool_size=5)
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

            ids = [str(r[0]) for r in rows]
            texts = [(r[1] or "")[:2000] for r in rows]

        print(f"  Encoding batch of {len(texts)} texts...")
        embeddings = model.encode(
            texts, batch_size=ENCODE_BATCH, show_progress_bar=False, convert_to_numpy=True,
        )

        # Bulk update using unnest - much faster than individual updates
        async with async_session() as session:
            emb_arr = [e.tolist() for e in embeddings]
            id_arr = ids

            # Format embeddings as PG array literals
            emb_literals = []
            for e in emb_arr:
                emb_literals.append("ARRAY[" + ",".join(str(x) for x in e) + "]::vector(384)")

            placeholders = []
            params = {}
            for i, (mid, emb) in enumerate(zip(id_arr, emb_literals)):
                placeholders.append(f"(:id{i}, {emb})")
                params[f"id{i}"] = mid

            sql = sa_text(f"""
                UPDATE messages AS m
                SET embedding = d.embedding
                FROM (VALUES { ",".join(placeholders) }) AS d(id, embedding)
                WHERE m.id = d.id::uuid
            """).bindparams(**params)

            await session.execute(sql)
            await session.commit()

        total_processed += len(rows)
        elapsed = time.time() - start_time
        rate = total_processed / elapsed if elapsed > 0 else 0
        print(f"  Progress: {total_processed}/{MAX_MESSAGES} ({rate:.0f} msg/s, elapsed {elapsed:.0f}s)")

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
