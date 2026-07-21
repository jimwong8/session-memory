#!/usr/bin/env python3
"""Memory-safe embedding backfill with env-configurable model."""
import asyncio
import gc
import os
import sys
import time

os.chdir("/app")
sys.path.insert(0, "/app")

from sqlalchemy import func, select, text as sa_text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sentence_transformers import SentenceTransformer

from src.config import settings
from src.models import Message

MODEL_PATH = os.environ.get("EMBEDDING_MODEL", settings.embedding_model)
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))
ENCODE_BATCH = int(os.environ.get("ENCODE_BATCH", "16"))
MAX_MESSAGES = int(os.environ.get("BACKFILL_MAX", "100000"))
VEC_DIM = int(os.environ.get("VECTOR_DIM", "512"))

engine = create_async_engine(settings.database_url, pool_size=2)
async_session = async_sessionmaker(engine, class_=AsyncSession)


async def main():
    print(f"Loading model: {MODEL_PATH}")
    model = SentenceTransformer(MODEL_PATH)
    actual_dim = model.get_sentence_embedding_dimension()
    print(f"Model dim: {actual_dim}, target DB dim: {VEC_DIM}")

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

        # Individual updates
        async with async_session() as session:
            for msg_id, emb in zip(ids, embeddings):
                emb_list = emb.tolist()
                emb_str = "[" + ",".join(str(x) for x in emb_list) + "]"
                sql = sa_text(
                    f"UPDATE messages SET embedding = ARRAY{emb_str}::vector({VEC_DIM}) WHERE id = :msg_id::uuid"
                )
                await session.execute(sql, {"msg_id": msg_id})
            await session.commit()

        total_processed += len(rows)
        elapsed = time.time() - start_time
        rate = total_processed / elapsed if elapsed > 0 else 0
        print(f"  Progress: {total_processed}/{MAX_MESSAGES} ({rate:.0f} msg/s, elapsed {elapsed:.0f}s)")

        del embeddings, texts, ids, rows
        gc.collect()

    elapsed = time.time() - start_time
    print(f"\nDone! Processed {total_processed} messages in {elapsed:.1f}s")

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
