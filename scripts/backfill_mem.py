#!/usr/bin/env python3
"""Backfill embeddings - memory efficient with safetensors."""
import asyncio, time, os, gc
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from transformers import AutoModel, AutoTokenizer

os.chdir("/models")
MODEL_PATH = "bge-small-zh"
BATCH = 100

DB = os.getenv("KG_DB_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/session_memory")
engine = create_async_engine(DB, pool_size=3)
async_session = async_sessionmaker(engine, class_=AsyncSession)

import torch
torch.set_num_threads(2)

print(f"Loading: {MODEL_PATH}")
tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
model = AutoModel.from_pretrained(MODEL_PATH, local_files_only=True, torch_dtype=torch.float32)
dim = model.config.hidden_size
model_size = sum(p.numel()*p.element_size() for p in model.parameters())/1024/1024
print(f"Loaded, dim={dim}, model_size={model_size:.1f}MB, batch={BATCH}")

@torch.inference_mode()
def encode(texts):
    inputs = tokenizer(texts, padding=True, truncation=True, return_tensors="pt", max_length=512)
    out = model(**inputs)
    mask = inputs["attention_mask"].unsqueeze(-1).expand(out.last_hidden_state.size()).float()
    emb = torch.sum(out.last_hidden_state * mask, 1) / torch.clamp(mask.sum(1), min=1e-9)
    return emb.tolist()

async def main():
    total = 0
    t0 = time.time()
    while True:
        async with async_session() as s:
            rows = (await s.execute(
                sa_text("SELECT id, content FROM messages WHERE embedding IS NULL AND role IN ('user','assistant') ORDER BY created_at ASC LIMIT :lim").bindparams(lim=BATCH)
            )).all()
            if not rows:
                break
            ids = [r[0] for r in rows]
            texts = [(r[1] or "")[:2000] for r in rows]
        embs = encode(texts)
        async with async_session() as s:
            for mid, emb in zip(ids, embs):
                await s.execute(sa_text("UPDATE messages SET embedding=:e\\:\\:vector(512) WHERE id=:i").bindparams(e=str(emb), i=mid))
            await s.commit()
        total += len(rows)
        elapsed = time.time() - t0
        rate = total / elapsed if elapsed > 0 else 0
        print(f"  {total} done, rate={rate:.1f}/s, elapsed={elapsed/60:.1f}m")
        gc.collect()
    elapsed = time.time() - t0
    print(f"Done: {total} messages in {elapsed/60:.1f}m ({total/elapsed:.1f}/s)")

asyncio.run(main())