"""Multi-hop bridge discovery service for Hermes Session Memory.

Implements graph traversal algorithms inspired by AutoMem's recall system:
- Bridge discovery: find memories that connect two seed memories via graph edges
- Entity expansion: find memories mentioning entities from seed results
- Auto-decomposition: break complex queries into entity+topic sub-queries

Uses PostgreSQL recursive CTE for multi-hop traversal (no graph database needed).
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel
from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings

logger = logging.getLogger(__name__)

# Extended relation types for richer graph traversal
EXTENDED_RELATION_TYPES = {
    "exemplifies",
    "prefers_over",
    "contradicts",
    "reinforces",
    "evolved_into",
    "invalidated_by",
    "leads_to",
    "occurred_before",
    "derived_from",
    "part_of",
}

# Stopwords for entity extraction
ENTITY_STOPWORDS = {
    "what", "would", "could", "does", "did", "how", "why", "when",
    "where", "which", "who", "whose", "will", "can", "should", "has",
    "have", "had", "is", "are", "was", "were", "do", "been", "being",
    "the", "answer", "yes", "no", "likely", "based", "according",
    "since", "because",
}


@dataclass
class BridgeResult:
    """A bridge memory that connects seed memories."""
    memory_id: str
    content: str
    role: str
    session_id: str
    score: float
    bridge_path: list[dict] = field(default_factory=list)
    relation_types: list[str] = field(default_factory=list)
    entity_names: list[str] = field(default_factory=list)
    timestamp: str = ""


@dataclass
class EntityResult:
    """Memory found via entity expansion."""
    memory_id: str
    content: str
    role: str
    session_id: str
    score: float
    matched_entity: str = ""
    entity_category: str = ""
    timestamp: str = ""


class BridgeService:
    """Multi-hop bridge discovery over Hermes' KG (PostgreSQL)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def discover_bridges(
        self,
        seed_message_ids: list[str],
        *,
        max_hops: int = 2,
        per_seed_limit: int = 5,
        expansion_limit: int = 10,
        min_score: float = 0.0,
        allowed_relations: set[str] | None = None,
    ) -> list[BridgeResult]:
        """Find bridge memories that connect seed messages via graph traversal."""
        if not seed_message_ids:
            return []

        allowed = allowed_relations or EXTENDED_RELATION_TYPES | {
            "depends_on", "modifies", "fixes", "calls", "implements"
        }

        seed_entities = await self._get_seed_entities(seed_message_ids)
        if not seed_entities:
            return []

        entity_ids = [e["id"] for e in seed_entities]
        entity_names = {e["id"]: e["name"] for e in seed_entities}

        bridge_entity_ids = await self._traverse_bridge_entities(
            entity_ids, allowed, max_hops, expansion_limit
        )
        if not bridge_entity_ids:
            return []

        bridge_info = await self._get_bridge_info(
            entity_ids, bridge_entity_ids, allowed, max_hops
        )
        messages = await self._find_messages_with_entities(
            list(bridge_info.keys()), seed_message_ids, limit=expansion_limit * 2
        )

        results = []
        for msg in messages:
            msg_id = str(msg["id"])
            score = self._compute_bridge_score(msg, bridge_info, entity_names)
            if score < min_score:
                continue

            path = []
            for ent_id in msg.get("entity_ids", []):
                if ent_id in bridge_info:
                    info = bridge_info[ent_id]
                    path.append({
                        "entity_id": str(ent_id),
                        "entity_name": entity_names.get(ent_id, str(ent_id)),
                        "depth": info["depth"],
                        "relation_types": info.get("relation_types", []),
                    })

            results.append(BridgeResult(
                memory_id=msg_id,
                content=msg.get("content", "")[:500],
                role=msg.get("role", "user"),
                session_id=str(msg.get("session_id", "")),
                score=score,
                bridge_path=path,
                relation_types=list(set(
                    t
                    for ent_id in msg.get("entity_ids", [])
                    for t in bridge_info.get(ent_id, {}).get("relation_types", [])
                    if t
                )),
                entity_names=[
                    entity_names.get(ent_id, str(ent_id))
                    for ent_id in msg.get("entity_ids", [])
                    if ent_id in entity_names
                ],
                timestamp=str(msg.get("created_at", "")),
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:expansion_limit]

    async def expand_by_entity(
        self,
        seed_message_ids: list[str],
        *,
        limit_per_entity: int = 5,
        total_limit: int = 10,
    ) -> list[EntityResult]:
        """Extract entities from seed messages, find other messages mentioning them."""
        if not seed_message_ids:
            return []

        seed_entities = await self._get_seed_entities(seed_message_ids)
        if not seed_entities:
            return []

        entity_names = [e["name"] for e in seed_entities[:5]]

        results = []
        seen_ids = set(seed_message_ids)

        for entity_name in entity_names:
            if len(results) >= total_limit:
                break

            msg_sql = sa_text("""
                SELECT id, content, role, session_id, created_at
                FROM messages
                WHERE content ILIKE :entity_pattern
                  AND id NOT IN :seen_ids
                  AND role IN ('user', 'assistant')
                ORDER BY created_at DESC
                LIMIT :limit
            """)

            try:
                result = await self.db.execute(
                    msg_sql.bindparams(
                        entity_pattern=f"%{entity_name}%",
                        seen_ids=tuple(seen_ids) if seen_ids else (uuid.uuid4(),),
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

                results.append(EntityResult(
                    memory_id=msg_id,
                    content=(row[1] or "")[:500],
                    role=row[2],
                    session_id=str(row[3]),
                    score=score,
                    matched_entity=entity_name,
                    entity_category="",
                    timestamp=str(row[4]),
                ))

                if len(results) >= total_limit:
                    break

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:total_limit]

    async def _get_seed_entities(
        self, message_ids: list[str]
    ) -> list[dict]:
        """Get KG entities associated with seed messages."""
        if not message_ids:
            return []

        sql = sa_text("""
            SELECT DISTINCT e.id, e.name
            FROM kg_entities e
            JOIN kg_relations r ON (
                r.source_entity_id = e.id OR r.target_entity_id = e.id
            )
            WHERE r.message_id IN :msg_ids
            LIMIT 20
        """)

        try:
            result = await self.db.execute(
                sql.bindparams(msg_ids=tuple(message_ids))
            )
            rows = result.all()
        except Exception as exc:
            logger.debug("Failed to get seed entities: %s", exc)
            return []

        return [{"id": row[0], "name": row[1]} for row in rows]

    async def _traverse_bridge_entities(
        self,
        entity_ids: list,
        allowed: set[str],
        max_hops: int,
        expansion_limit: int,
    ) -> list:
        """Use recursive CTE to find reachable entities."""
        allowed_list = sorted(allowed)
        relation_placeholders = ", ".join(f"'{r}'" for r in allowed_list)

        traversal_sql = sa_text(f"""
            WITH RECURSIVE bridge_path AS (
                SELECT
                    e.id AS entity_id,
                    e.name AS entity_name,
                    1 AS depth,
                    ARRAY[e.id] AS path_ids,
                    ARRAY[]::uuid[] AS relation_ids,
                    ARRAY[]::text[] AS relation_types,
                    e.id AS source_entity_id
                FROM kg_entities e
                WHERE e.id = ANY(:entity_ids)

                UNION ALL

                SELECT
                    target.id AS entity_id,
                    target.name AS entity_name,
                    bp.depth + 1 AS depth,
                    bp.path_ids || target.id AS path_ids,
                    bp.relation_ids || r.id AS relation_ids,
                    bp.relation_types || r.relation_type AS relation_types,
                    bp.source_entity_id
                FROM bridge_path bp
                JOIN kg_relations r ON (
                    r.source_entity_id = bp.entity_id
                    OR r.target_entity_id = bp.entity_id
                )
                JOIN kg_entities target ON (
                    target.id = CASE
                        WHEN r.source_entity_id = bp.entity_id THEN r.target_entity_id
                        ELSE r.source_entity_id
                    END
                )
                WHERE bp.depth < :max_hops
                  AND target.id <> ALL(bp.path_ids)
                  AND r.relation_type IN ({relation_placeholders})
            )
            SELECT DISTINCT ON (entity_id)
                entity_id,
                entity_name,
                depth,
                path_ids,
                relation_ids,
                relation_types,
                source_entity_id
            FROM bridge_path
            WHERE depth > 1
            ORDER BY entity_id, depth ASC
            LIMIT :expansion_limit
        """)

        try:
            result = await self.db.execute(
                traversal_sql.bindparams(
                    entity_ids=entity_ids,
                    max_hops=max_hops,
                    expansion_limit=expansion_limit,
                )
            )
            return [row[0] for row in result.all()]
        except Exception as exc:
            logger.warning("Bridge traversal failed: %s", exc)
            return []

    async def _get_bridge_info(
        self,
        seed_entity_ids: list,
        bridge_entity_ids: list,
        allowed: set[str],
        max_hops: int,
    ) -> dict:
        """Get bridge info including depth and relation types."""
        if not bridge_entity_ids:
            return {}

        allowed_list = sorted(allowed)
        relation_placeholders = ", ".join(f"'{r}'" for r in allowed_list)

        info_sql = sa_text(f"""
            WITH RECURSIVE bridge_path AS (
                SELECT
                    e.id AS entity_id,
                    1 AS depth,
                    ARRAY[e.id] AS path_ids,
                    ARRAY[]::text[] AS relation_types
                FROM kg_entities e
                WHERE e.id = ANY(:seed_ids)

                UNION ALL

                SELECT
                    target.id AS entity_id,
                    bp.depth + 1 AS depth,
                    bp.path_ids || target.id AS path_ids,
                    bp.relation_types || r.relation_type AS relation_types
                FROM bridge_path bp
                JOIN kg_relations r ON (
                    r.source_entity_id = bp.entity_id
                    OR r.target_entity_id = bp.entity_id
                )
                JOIN kg_entities target ON (
                    target.id = CASE
                        WHEN r.source_entity_id = bp.entity_id THEN r.target_entity_id
                        ELSE r.source_entity_id
                    END
                )
                WHERE bp.depth < :max_hops
                  AND target.id <> ALL(bp.path_ids)
                  AND r.relation_type IN ({relation_placeholders})
            )
            SELECT entity_id, depth, relation_types
            FROM bridge_path
            WHERE entity_id = ANY(:bridge_ids)
        """)

        try:
            result = await self.db.execute(
                info_sql.bindparams(
                    seed_ids=seed_entity_ids,
                    bridge_ids=bridge_entity_ids,
                    max_hops=max_hops,
                )
            )
            return {
                row[0]: {"depth": row[1], "relation_types": row[2] or []}
                for row in result.all()
            }
        except Exception as exc:
            logger.debug("Failed to get bridge info: %s", exc)
            return {}

    async def _find_messages_with_entities(
        self,
        entity_ids: list,
        exclude_ids: list[str],
        limit: int = 20,
    ) -> list[dict]:
        """Find messages that mention any of the given entities."""
        if not entity_ids:
            return []

        sql = sa_text("""
            SELECT DISTINCT m.id, m.content, m.role, m.session_id, m.created_at,
                   array_agg(DISTINCT e.id) as entity_ids
            FROM messages m
            JOIN kg_relations r ON r.message_id = m.id
            JOIN kg_entities e ON e.id = r.source_entity_id OR e.id = r.target_entity_id
            WHERE e.id = ANY(:entity_ids)
              AND m.id NOT IN :exclude_ids
              AND m.role IN ('user', 'assistant')
            GROUP BY m.id
            ORDER BY m.created_at DESC
            LIMIT :limit
        """)

        try:
            result = await self.db.execute(
                sql.bindparams(
                    entity_ids=entity_ids,
                    exclude_ids=tuple(exclude_ids) if exclude_ids else (uuid.uuid4(),),
                    limit=limit,
                )
            )
            rows = result.all()
        except Exception as exc:
            logger.debug("Message search failed: %s", exc)
            return []

        return [
            {
                "id": row[0],
                "content": row[1],
                "role": row[2],
                "session_id": row[3],
                "created_at": row[4],
                "entity_ids": row[5] or [],
            }
            for row in rows
        ]

    def _compute_bridge_score(
        self,
        msg: dict,
        bridge_info: dict,
        seed_entity_names: dict,
    ) -> float:
        """Compute bridge score based on relation strength and path depth."""
        score = 0.0
        entity_ids = msg.get("entity_ids", [])

        for ent_id in entity_ids:
            if ent_id in bridge_info:
                info = bridge_info[ent_id]
                depth = info.get("depth", 1)
                score += 1.0 / depth

                relation_types = info.get("relation_types", [])
                rich_types = {"exemplifies", "prefers_over", "leads_to", "contradicts"}
                if any(t in rich_types for t in relation_types):
                    score += 0.3

        if any(eid in seed_entity_names for eid in entity_ids):
            score += 0.2

        return round(score, 3)

    def _compute_entity_score(self, content: str, entity_name: str) -> float:
        """Score a message for entity match quality."""
        content_lower = content.lower()
        entity_lower = entity_name.lower()

        count = content_lower.count(entity_lower)
        score = min(count * 0.1, 0.5)

        if entity_lower in content_lower:
            score += 0.3

        content_len = len(content)
        if 50 <= content_len <= 1000:
            score += 0.2

        return round(score, 3)


def apply_temporal_recency_bias(
    results: list[dict],
    weight: float = 0.15,
) -> list[dict]:
    """Apply temporal recency bias to scored results.

    After scoring, boost newer results by a configurable weight.
    weight: 0.0 = no bias, 0.15 = moderate recency preference
    """
    if not results or weight <= 0:
        return results

    from datetime import datetime

    epochs = []
    valid_epochs = []
    for r in results:
        ts = r.get("timestamp") or (r.get("memory", {}) or {}).get("timestamp")
        if not ts:
            epochs.append(None)
            continue
        try:
            if isinstance(ts, str):
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                dt = ts
            epoch = dt.timestamp()
            epochs.append(epoch)
            valid_epochs.append(epoch)
        except (ValueError, TypeError):
            epochs.append(None)

    if len(valid_epochs) < 2:
        return results

    spread = max(valid_epochs) - min(valid_epochs)
    if spread <= 0:
        return results

    oldest = min(valid_epochs)
    for r, epoch in zip(results, epochs):
        if epoch is None:
            continue
        relative_recency = (epoch - oldest) / spread
        final_score = float(r.get("final_score", r.get("score", 0.0)))
        r["final_score"] = final_score + weight * relative_recency
        r["score"] = r["final_score"]
        components = r.setdefault("score_components", {})
        components["temporal"] = relative_recency

    results.sort(key=lambda x: float(x.get("final_score", x.get("score", 0.0))), reverse=True)
    return results


# Schema for API responses
class BridgeResponse(BaseModel):
    """API response for bridge discovery."""
    status: str
    query: str
    bridges: list[dict]
    expansion: dict
    query_time_ms: float


class DecompositionResponse(BaseModel):
    """API response for query decomposition."""
    original_query: str
    decomposed_queries: list[str]
    entities_found: list[str]
    topics_found: list[str]


def decompose_query(query: str) -> list[str]:
    """Auto-decompose a complex query into entity+topic sub-queries.

    Inspired by AutoMem's auto_decompose:
    Extract entities (capitalized words) and topics, generate focused
    sub-queries for multi-hop reasoning.

    Example:
        "Would Caroline pursue writing?" ->
        ["Caroline", "Caroline career", "Caroline interests", "Caroline writing"]
    """
    if not query or len(query) < 3:
        return [query] if query else []

    # Extract capitalized entities (names)
    words = query.split()
    entities = []
    for i, word in enumerate(words):
        clean = re.sub(r"[^\w]", "", word)
        if len(clean) < 2 or clean.lower() in ENTITY_STOPWORDS:
            continue
        if clean[0].isupper() and clean[1:].islower():
            if i == 0 or (i > 0 and words[i - 1][-1] not in ".?!"):
                entities.append(clean)

    # Extract topic keywords (4+ char words, not stopwords)
    topics = [
        w for w in re.findall(r"\b[a-z]{4,}\b", query.lower())
        if w not in ENTITY_STOPWORDS
    ][:5]

    if not entities:
        # No entities found: return topic-focused queries
        return topics[:3] if topics else [query]

    decomposed = []
    for entity in entities[:2]:
        # Entity alone
        decomposed.append(entity)
        # Entity + topic
        for topic in topics[:3]:
            decomposed.append(f"{entity} {topic}")
        # Entity + broad interests
        if any(t in topics for t in ["career", "job", "work", "writing"]):
            decomposed.append(f"{entity} interests goals plans")

    # Remove duplicates while preserving order
    seen = set()
    unique = []
    for q in decomposed:
        q_lower = q.lower().strip()
        if q_lower not in seen:
            seen.add(q_lower)
            unique.append(q)

    return unique[:10]
