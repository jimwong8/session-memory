#!/usr/bin/env python3
"""Memory-optimized backfill using unnest for batch updates."""
import asyncio
import gc
import os
import sys
import time

os.chdir("/app")
sys.path.insert(0, "/app")

from sqlalchemy import func, select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sentence_transformers import SentenceTransformer

from src.config import settings
from src.models import Message

BATCH_SIZE = 200
ENCODE_BATCH = 32
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

            ids = [str(r[0]) for r in rows]
            texts = [(r[1] or "")[:2000] for r in rows]

        print(f"  Encoding batch of {len(texts)} texts...")
        embeddings = model.encode(
            texts, batch_size=ENCODE_BATCH, show_progress_bar=False, convert_to_numpy=True,
        )

        # Bulk update using unnest
        async with async_session() as session:
            emb_arr = [e.tolist() for e in embeddings]
            id_arr = ids

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
                FROM (VALUES {",".join(placeholders)}) AS d(id, embedding)
                WHERE m.id = d.id::uuid
            """).bindparams(**params)

            await session.execute(sql)
            await session.commit()

        total_processed += len(rows)
        elapsed = time.time() - start_time
        rate = total_processed / elapsed if elapsed > 0 else 0
        print(f"  Progress: {total_processed}/{MAX_MESSAGES} ({rate:.0f} msg/s, elapsed {elapsed:.0f}s)")

        del embeddings, texts, ids, rows, emb_arr, emb_literals, params
        gc.collect()

    elapsed = time.time() - start_time
    print(f"\nDone! Processed {total_processed} messages in {elapsed:.1f}s")

    async with async_session() as session:
        total = (await session.execute(select(func.count()).select_from(Message))).scalar()
        with_emb = (await session.execute(
            select(func.count()).where(Message.embedding.isnot(None))
        )).scalar()
        print(f"Total: {total}, with embedding: {with_emb} ({100*with_emb/max(total,1):.1f}%)")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())