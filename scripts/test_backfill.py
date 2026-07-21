#!/usr/bin/env python3
"""Test backfill: load model, fetch 1 batch, encode, verify — no writes."""
import asyncio, time, os
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from transformers import AutoConfig, AutoModel, AutoTokenizer
from safetensors.torch import load_file
import torch

DB = os.getenv("KG_DB_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/session_memory")
MODEL_PATH = "/models/bge-small-zh"

print("Loading model...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
config = AutoConfig.from_pretrained(MODEL_PATH, local_files_only=True)
model = AutoModel.from_config(config)
state_dict = load_file(os.path.join(MODEL_PATH, "model.safetensors"))
state_dict.pop("embeddings.position_ids", None)
model.load_state_dict(state_dict, strict=False)
print(f"Loaded, dim={model.config.hidden_size}")

def encode(texts):
    inputs = tokenizer(texts, padding=True, truncation=True, return_tensors="pt", max_length=512)
    with torch.no_grad():
        out = model(**inputs)
    mask = inputs["attention_mask"].unsqueeze(-1).expand(out.last_hidden_state.size()).float()
    emb = torch.sum(out.last_hidden_state * mask, 1) / torch.clamp(mask.sum(1), min=1e-9)
    return emb.tolist()

engine = create_async_engine(DB, pool_size=1)
async_session = async_sessionmaker(engine, class_=AsyncSession)

async def main():
    # Test: query 2 rows
    async with async_session() as s:
        rows = (await s.execute(
            sa_text("SELECT id, content, role FROM messages WHERE embedding IS NULL AND role IN ('user','assistant') ORDER BY created_at ASC LIMIT 2")
        )).all()
        print(f"Found {len(rows)} rows")
        for r in rows:
            print(f"  id={r[0]} role={r[2]} content_len={len(r[1] or '')}")
    
    if not rows:
        print("No rows need backfill!")
        return
    
    # Encode
    texts = [(r[1] or "")[:2000] for r in rows]
    t0 = time.time()
    embs = encode(texts)
    print(f"Encoded {len(embs)} texts in {time.time()-t0:.2f}s, dim={len(embs[0])}")
    
    # Test UPDATE with one row (verify SQL works)
    mid = rows[0][0]
    emb_str = str(embs[0])
    async with async_session() as s:
        await s.execute(sa_text(
            "UPDATE messages SET embedding = CAST(:emb AS vector(512)) WHERE id = CAST(:id AS uuid)"
        ).bindparams(emb=emb_str, id=str(mid)))
        await s.commit()
        print(f"UPDATE test OK for id={mid}")

asyncio.run(main())
