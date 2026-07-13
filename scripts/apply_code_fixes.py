#!/usr/bin/env python3
"""Apply code fixes to session-memory Python files on the remote server."""
import re

# ============================================================
# Fix 1: knowledge_graph.py - Full-text search for KG entities
# ============================================================
with open("/app/src/services/knowledge_graph.py", "r") as f:
    kg_code = f.read()

old_search = '''    async def search(self, query: str, session_id=None, top_k: int = 5):
        entity_stmt = select(KGEntity).where(KGEntity.name.ilike(f"%{query}%"))
        relation_stmt = select(KGRelation).where(KGRelation.relation_type.ilike(f"%{query}%"))
        if session_id is not None:
            entity_stmt = entity_stmt.where(KGEntity.session_id == session_id)
            relation_stmt = relation_stmt.where(KGRelation.session_id == session_id)
        entity_stmt = entity_stmt.order_by(KGEntity.created_at.desc()).limit(top_k)
        relation_stmt = relation_stmt.order_by(KGRelation.created_at.desc()).limit(top_k)
        entity_result = await self.db.execute(entity_stmt)
        relation_result = await self.db.execute(relation_stmt)
        return list(entity_result.scalars().all()), list(relation_result.scalars().all())'''

new_search = '''    async def search(self, query: str, session_id=None, top_k: int = 5):
        from sqlalchemy import text as sa_text
        entity_ids = []
        try:
            ft_result = await self.db.execute(
                sa_text("SELECT id FROM kg_entities WHERE name_tsv @@ to_tsquery('simple', :q) ORDER BY ts_rank(name_tsv, to_tsquery('simple', :q)) DESC LIMIT :lim").bindparams(q=query, lim=top_k * 3)
            )
            entity_ids = [row[0] for row in ft_result.all()]
        except Exception:
            pass
        entity_stmt = select(KGEntity)
        relation_stmt = select(KGRelation)
        if entity_ids:
            entity_stmt = entity_stmt.where(KGEntity.id.in_(entity_ids))
        else:
            entity_stmt = entity_stmt.where(KGEntity.name.ilike(f"%{query}%"))
        if session_id is not None:
            entity_stmt = entity_stmt.where(KGEntity.session_id == session_id)
            relation_stmt = relation_stmt.where(KGRelation.session_id == session_id)
        entity_stmt = entity_stmt.limit(top_k)
        relation_stmt = relation_stmt.where(KGRelation.relation_type.ilike(f"%{query}%")).limit(top_k)
        entity_result = await self.db.execute(entity_stmt)
        relation_result = await self.db.execute(relation_stmt)
        return list(entity_result.scalars().all()), list(relation_result.scalars().all())'''

if old_search in kg_code:
    kg_code = kg_code.replace(old_search, new_search)
    print("OK: knowledge_graph.py search updated to full-text")
else:
    print("WARN: Could not find exact search method to replace")

with open("/app/src/services/knowledge_graph.py", "w") as f:
    f.write(kg_code)

# ============================================================
# Fix 2: ScenarioBuilder - Limit atom query
# ============================================================
with open("/app/src/services/pyramid/scenario_builder.py", "r") as f:
    sc_code = f.read()

old_fetch = '''        stmt = (
            select(MemoryAtom)
            .where(
                MemoryAtom.user_id == user_id,
                MemoryAtom.superseded_by.is_(None),
            )
            .order_by(MemoryAtom.created_at.asc())
        )'''

new_fetch = '''        stmt = (
            select(MemoryAtom)
            .where(
                MemoryAtom.user_id == user_id,
                MemoryAtom.superseded_by.is_(None),
            )
            .order_by(MemoryAtom.created_at.asc())
            .limit(500)
        )'''

if old_fetch in sc_code:
    sc_code = sc_code.replace(old_fetch, new_fetch)
    print("OK: ScenarioBuilder atom fetch limited to 500")
else:
    print("WARN: Could not find scenario fetch to limit")

with open("/app/src/services/pyramid/scenario_builder.py", "w") as f:
    f.write(sc_code)

# ============================================================
# Fix 3: recall.py - Per-search timeout
# ============================================================
with open("/app/src/services/recall.py", "r") as f:
    recall_code = f.read()

old_loop = '''    for name, search_fn in searches:
        if time.monotonic() >= deadline:
            errors.append(f"{name}: deadline exceeded")
            break
        try:
            result = await asyncio.wait_for(
                search_fn(),
                timeout=max(0.1, deadline - time.monotonic()),
            )'''

new_loop = '''    for name, search_fn in searches:
        if time.monotonic() >= deadline:
            errors.append(f"{name}: deadline exceeded")
            break
        try:
            per_search_timeout = min(2.0, max(0.1, deadline - time.monotonic()))
            result = await asyncio.wait_for(
                search_fn(),
                timeout=per_search_timeout,
            )'''

if old_loop in recall_code:
    recall_code = recall_code.replace(old_loop, new_loop)
    print("OK: recall.py per-search timeout added (2s cap)")
else:
    print("WARN: Could not find search loop to patch in recall.py")

with open("/app/src/services/recall.py", "w") as f:
    f.write(recall_code)

print("\nAll code fixes applied successfully.")
