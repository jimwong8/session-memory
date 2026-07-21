#!/usr/bin/env python3
"""Backfill embeddings using local bge-small-zh-v1.5 (safetensors, torch 2.5.1 compatible)."""
import asyncio, time, os, uuid
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from transformers import AutoConfig, AutoModel, AutoTokenizer
from safetensors.torch import load_file
import torch

DB = os.getenv("KG_DB_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/session_memory")
MODEL_PATH = "/models/bge-small-zh"
BATCH = 5

engine = create_async_engine(DB, pool_size=2)
async_session = async_sessionmaker(engine, class_=AsyncSession)

print(f"Loading: {MODEL_PATH}")
tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
config = AutoConfig.from_pretrained(MODEL_PATH, local_files_only=True)
model = AutoModel.from_config(config)
state_dict = load_file(os.path.join(MODEL_PATH, "model.safetensors"))
state_dict.pop("embeddings.position_ids", None)
model.load_state_dict(state_dict, strict=False)
dim = model.config.hidden_size
print(f"Loaded, dim={dim}")

def encode(texts):
    inputs = tokenizer(texts, padding=True, truncation=True, return_tensors="pt", max_length=512)
    with torch.no_grad():
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
            ids = [r[0] for r in rows]  # UUID objects
            texts = [(r[1] or "")[:2000] for r in rows]
        embs = encode(texts)
        async with async_session() as s:
            for mid, emb in zip(ids, embs):
                emb_str = str(emb)
                await s.execute(
                    sa_text("UPDATE messages SET embedding = :emb\\:\\:vector(512) WHERE id = :id").bindparams(emb=emb_str, id=mid)
                )
            await s.commit()
        total += len(rows)
        elapsed = time.time() - t0
        rate = total / elapsed if elapsed > 0 else 0
        print(f"  {total} done ({rate:.0f}/s)")
    print(f"Done: {total} messages embedded")

asyncio.run(main())
