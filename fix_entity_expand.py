#!/usr/bin/env python3
"""Patch routes.py and bridge_service.py for entity_names support"""

# Patch routes.py
path = "src/routes.py"
c = open(path).read()

# 1. Make seed_message_ids optional, add entity_names
old = "seed_message_ids: list[str] = Field(\n        description=\"Seed message IDs for entity extraction\",\n        min_length=1, max_length=10,\n    )\n    limit_per_entity: int = Field(default=5, ge=1, le=20)"
new = "seed_message_ids: list[str] = Field(\n        description=\"Seed message IDs for entity extraction\",\n        min_length=0, max_length=10, default=[],\n    )\n    entity_names: list[str] = Field(\n        description=\"Entity names to expand directly\",\n        default=[],\n    )\n    limit_per_entity: int = Field(default=5, ge=1, le=20)"
c = c.replace(old, new)

# 2. Update route handler
old_handler = "svc = BridgeService(db)\n    expansions = await svc.expand_by_entity(\n        data.seed_message_ids,\n        limit_per_entity=data.limit_per_entity,\n        total_limit=data.total_limit,\n    )"
new_handler = "svc = BridgeService(db)\n    if data.entity_names:\n        expansions = await svc.expand_by_entity_names(\n            data.entity_names,\n            limit_per_entity=data.limit_per_entity,\n            total_limit=data.total_limit,\n        )\n    else:\n        expansions = await svc.expand_by_entity(\n            data.seed_message_ids,\n            limit_per_entity=data.limit_per_entity,\n            total_limit=data.total_limit,\n        )"
c = c.replace(old_handler, new_handler)

open(path, "w").write(c)
print("routes.py OK")

# Patch bridge_service.py — add expand_by_entity_names
path2 = "src/services/bridge_service.py"
b = open(path2).read()

new_method = """    async def expand_by_entity_names(
        self,
        entity_names: list[str],
        *,
        limit_per_entity: int = 5,
        total_limit: int = 10,
    ) -> list:
        \"\"\"Given entity names directly, find messages mentioning them.\"\"\"
        if not entity_names:
            return []

        entity_names = entity_names[:10]
        results = []
        seen_ids = set()

        for entity_name in entity_names:
            if len(results) >= total_limit:
                break

            msg_sql = sa_text(\"\"\"
                SELECT id, content, role, session_id, created_at
                FROM messages
                WHERE content ILIKE :entity_pattern
                  AND role IN ('user', 'assistant')
                ORDER BY created_at DESC
                LIMIT :limit
            \"\"\")

            try:
                result = await self.db.execute(
                    msg_sql.bindparams(
                        entity_pattern=f"%{entity_name}%",
                        limit=limit_per_entity,
                    )
                )
                rows = result.all()
            except Exception as exc:
                logger.debug("Entity expansion search failed for %s: %s", entity_name, exc)
                continue

            for row in rows:
                msg_id = str(row[0])
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                score = self._compute_entity_score(row[1] or "", entity_name)
                results.append(
                    EntityResult(
                        memory_id=msg_id,
                        content=(row[1] or "")[:500],
                        role=row[2],
                        session_id=str(row[3]),
                        score=score,
                        matched_entity=entity_name,
                        entity_category="",
                        timestamp=str(row[4]),
                    )
                )
                if len(results) >= total_limit:
                    break

        return results
"""

# Insert after the class definition or before expand_by_entity
marker = "    async def expand_by_entity("
b = b.replace(marker, new_method + "\n\n    async def expand_by_entity(")

open(path2, "w").write(b)
print("bridge_service.py OK")
