#!/usr/bin/env python3
"""Lightweight embedding backfill for constrained environments."""
import asyncio, time, os, sys
sys.path.insert(0, '/app')
from sqlalchemy import select, text as sa_text, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sentence_transformers import SentenceTransformer
from src.config import settings
from src.models import Message

BATCH_SIZE = int(os.environ.get('BACKFILL_BATCH', '100'))
ENCODE_BATCH = int(os.environ.get('BACKFILL_ENCODE', '32'))
MAX_MESSAGES = int(os.environ.get('BACKFILL_MAX', '50000'))

engine = create_async_engine(settings.database_url, pool_size=3)
async_session = async_sessionmaker(engine, class_=AsyncSession)

async def main():
    print(f'Loading model: {settings.embedding_model}', flush=True)
    model = SentenceTransformer(settings.embedding_model, device='cpu')
    total_processed = 0
    start_time = time.time()
    while total_processed < MAX_MESSAGES:
        async with async_session() as session:
            stmt = select(Message.id, Message.content).where(
                Message.embedding.is_(None), Message.role.in_(['user', 'assistant'])
            ).order_by(Message.created_at.desc()).limit(BATCH_SIZE)
            result = await session.execute(stmt)
            rows = result.all()
            if not rows:
                print('No more messages without embeddings.', flush=True)
                break
            ids = [str(r[0]) for r in rows]
            texts = [(r[1] or '')[:2000] for r in rows]
        print(f'Encoding batch of {len(texts)} texts...', flush=True)
        embeddings = model.encode(texts, batch_size=ENCODE_BATCH, show_progress_bar=False, convert_to_numpy=True)
        print(f'Encoded {len(embeddings)} embeddings.', flush=True)
        async with async_session() as session:
            emb_literals = []
            for e in embeddings:
                emb_literals.append('ARRAY[' + ','.join(str(x) for x in e.tolist()) + ']::vector(384)')
            placeholders = []
            params = {}
            for i, (mid, emb) in enumerate(zip(ids, emb_literals)):
                placeholders.append(f'(:id{i}, {emb})')
                params[f'id{i}'] = mid
            sql = sa_text('UPDATE messages AS m SET embedding = d.embedding FROM (VALUES ' + ','.join(placeholders) + ') AS d(id, embedding) WHERE m.id = d.id::uuid').bindparams(**params)
            await session.execute(sql)
            await session.commit()
        total_processed += len(rows)
        elapsed = time.time() - start_time
        rate = total_processed / elapsed if elapsed > 0 else 0
        print(f'Progress: {total_processed} ({rate:.0f} msg/s, {elapsed:.0f}s)', flush=True)
    elapsed = time.time() - start_time
    print(f'Done! Processed {total_processed} messages in {elapsed:.1f}s', flush=True)
    async with async_session() as session:
        total = (await session.execute(select(func.count()).select_from(Message))).scalar()
        with_emb = (await session.execute(select(func.count()).where(Message.embedding.isnot(None)))).scalar()
        pct = round(100 * with_emb / total, 1) if total else 0
        print(f'FINAL: total={total}, with_embedding={with_emb} ({pct}%)', flush=True)
    await engine.dispose()

asyncio.run(main())
