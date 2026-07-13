"""混合召回服务 - BM25（关键词）+ 向量（语义）+ RRF 融合"""

import asyncio
import os
import logging
import time
import uuid
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.embedding_service import create_embedding

logger = logging.getLogger(__name__)


@dataclass
class Hit:
    """召回结果"""
    id: str
    score: float
    ranker: str  # "keyword" | "vector" | "hybrid"
    content: str = ""
    table: str = ""
    metadata: dict = field(default_factory=dict)


class Ranker:
    """混排融合 - Reciprocal Rank Fusion"""

    @staticmethod
    def fuse(lists: list[list[Hit]], k: int = 60) -> list[Hit]:
        """RRF 融合多个 ranker 结果"""
        scores: dict[str, dict] = {}
        for ranker_results in lists:
            for rank, hit in enumerate(ranker_results, 1):
                key = f"{hit.table}|{hit.id}"
                if key not in scores:
                    scores[key] = {
                        "id": hit.id,
                        "table": hit.table,
                        "content": hit.content,
                        "metadata": hit.metadata,
                        "rrf_score": 0.0,
                        "sources": [],
                    }
                scores[key]["rrf_score"] += 1.0 / (k + rank)
                scores[key]["sources"].append(hit.ranker)

        sorted_hits = sorted(
            scores.values(),
            key=lambda x: x["rrf_score"],
            reverse=True,
        )
        return [
            Hit(
                id=h["id"],
                score=h["rrf_score"],
                ranker="hybrid",
                content=h["content"],
                table=h["table"],
                metadata=h["metadata"],
            )
            for h in sorted_hits
        ]


class KeywordSearcher:
    """BM25 风格关键词搜索（基于 tsvector + trigram）"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def search_messages(
        self, query: str, session_id: uuid.UUID | None = None, limit: int = 10
    ) -> list[Hit]:
        """关键词搜索 messages"""
        return await self._search_table(
            table="messages",
            columns="id, content, role, created_at",
            tsv_column="content_tsv",
            query=query,
            session_filter=session_id,
            limit=limit,
        )

    async def search_atoms(
        self, query: str, user_id: str | None = None, limit: int = 10
    ) -> list[Hit]:
        """关键词搜索 memory_atoms"""
        hits = await self._search_table(
            table="memory_atoms",
            columns="id, content, kind, title",
            tsv_column="content_tsv",
            query=query,
            where_clause="superseded_by IS NULL",
            limit=limit,
        )
        if user_id:
            hits = [h for h in hits if h.metadata.get("user_id") == user_id]
        return hits

    async def search_scenarios(
        self, query: str, user_id: str | None = None, limit: int = 10
    ) -> list[Hit]:
        """关键词搜索 memory_scenarios"""
        return await self._search_table(
            table="memory_scenarios",
            columns="id, narrative_md, title",
            tsv_column="content_tsv",
            query=query,
            limit=limit,
        )

    async def _search_table(
        self,
        table: str,
        columns: str,
        tsv_column: str,
        query: str,
        session_filter: uuid.UUID | None = None,
        where_clause: str | None = None,
        limit: int = 10,
    ) -> list[Hit]:
        """通用关键词搜索"""
        # 清理查询词
        clean_query = " & ".join(
            [f"'{w}'" for w in query.split() if len(w) >= 2]
        )
        if not clean_query:
            return []

        conditions = [f"{tsv_column} @@ to_tsquery('simple', :q)"]
        params: dict = {"q": clean_query}

        if session_filter:
            conditions.append("session_id = :sid")
            params["sid"] = session_filter
        if where_clause:
            conditions.append(where_clause)

        where = " AND ".join(conditions)
        sql = sa_text(f"""
            SELECT {columns}
            FROM {table}
            WHERE {where}
            ORDER BY ts_rank({tsv_column}, to_tsquery('simple', :q)) DESC
            LIMIT :lim
        """)

        try:
            result = await self.db.execute(
                sql.bindparams(**params).bindparams(lim=limit)
            )
            rows = result.all()
        except Exception as exc:
            logger.warning("keyword search failed on %s: %s", table, exc)
            return []

        hits = []
        for row in rows:
            row_dict = dict(row._mapping)
            content = str(row_dict.get("content") or row_dict.get("narrative_md") or "")
            hits.append(Hit(
                id=str(row_dict.get("id", "")),
                score=0.0,
                ranker="keyword",
                content=content[:500],
                table=table,
                metadata=row_dict,
            ))
        return hits


class VectorSearcher:
    """向量语义搜索"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def search(
        self,
        query: str,
        table: str = "messages",
        session_id: uuid.UUID | None = None,
        limit: int = 10,
    ) -> list[Hit]:
        """向量搜索"""
        try:
            embedding = await create_embedding(query)
        except Exception as exc:
            logger.warning("vector search embedding failed: %s", exc)
            return []

        emb_str = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"

        conditions = []
        if session_id:
            conditions.append(f"session_id = '{session_id}'")
        if table == "memory_atoms":
            conditions.append("superseded_by IS NULL")
        content_col = "narrative_md" if table == "memory_scenarios_v2" else "content"
        if table == "memory_scenarios_v2":
            table = "memory_scenarios" 
        where = " AND ".join(conditions) if conditions else "TRUE"

        sql = sa_text(f"""
            SELECT id, {content_col} AS content
            FROM {table}
            WHERE {where}
              AND embedding IS NOT NULL
            ORDER BY embedding <=> '{emb_str}'::vector
            LIMIT :lim
        """)

        try:
            result = await self.db.execute(sql.bindparams(lim=limit))
            rows = result.all()
        except Exception as exc:
            logger.warning("vector search failed on %s: %s", table, exc)
            return []

        return [
            Hit(
                id=str(row.id),
                score=0.0,
                ranker="vector",
                content=(row.content or "")[:500],
                table=table,
            )
            for row in rows
        ]



class EntitySearcher:
    """Entity-boost search via knowledge graph matching."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 10,
    ) -> list[Hit]:
        """Find content related to KG entities matching the query."""
        from src.models import KGEntity

        query_words = [w for w in query.split() if len(w) >= 2]
        if not query_words:
            return []

        conditions = []
        params = {}
        for i, word in enumerate(query_words[:5]):
            conditions.append(f"kg.name ILIKE :word{i}")
            params[f"word{i}"] = f"%{word}%"

        where = " OR ".join(conditions)

        entity_sql = sa_text(f"""
            SELECT DISTINCT kg.session_id, kg.name, kg.entity_type
            FROM kg_entities kg
            WHERE {where}
            LIMIT 20
        """).bindparams(**params)

        try:
            ent_result = await self.db.execute(entity_sql)
            entity_rows = ent_result.all()
        except Exception as exc:
            logger.warning("entity search failed: %s", exc)
            return []

        if not entity_rows:
            return []

        session_ids = list({row[0] for row in entity_rows})
        entity_names = [row[1] for row in entity_rows]

        placeholders = ", ".join(f":sid{i}" for i in range(len(session_ids)))
        for i, sid in enumerate(session_ids):
            params[f"sid{i}"] = sid

        msg_sql = sa_text(f"""
            SELECT id, content, role, session_id
            FROM messages
            WHERE session_id IN ({placeholders})
              AND role IN ('user', 'assistant')
            ORDER BY created_at DESC
            LIMIT :lim
        """).bindparams(**params).bindparams(lim=limit)

        try:
            msg_result = await self.db.execute(msg_sql)
            rows = msg_result.all()
        except Exception as exc:
            logger.warning("entity-boosted message search failed: %s", exc)
            return []

        hits = []
        for row in rows:
            content_lower = (row[1] or "").lower()
            ent_match_score = sum(1 for en in entity_names if en.lower() in content_lower)
            hits.append(Hit(
                id=str(row[0]),
                score=float(ent_match_score),
                ranker="entity",
                content=(row[1] or "")[:500],
                table="messages",
                metadata={"session_id": str(row[3]), "entity_names": entity_names},
            ))
        return hits


async def hybrid_recall(
    db: AsyncSession,
    query: str,
    *,
    strategy: str = "hybrid",
    session_id: uuid.UUID | None = None,
    user_id: str | None = None,
    limit: int = 10,
    k_rrf: int = 60,
    timeout_ms: int = 5000,
) -> list[Hit]:
    """混合召回入口

    Args:
        strategy: "keyword" | "vector" | "hybrid"
    """
    kw = KeywordSearcher(db)
    vec = VectorSearcher(db)

    results: list[list[Hit]] = []
    errors: list[str] = []
    deadline = time.monotonic() + timeout_ms / 1000

    searches: list[tuple] = []
    if strategy in ("keyword", "hybrid"):
        searches.append(("keyword_messages", lambda: kw.search_messages(query, session_id, limit)))
        searches.append(("keyword_atoms", lambda: kw.search_atoms(query, user_id, limit)))
        searches.append(("keyword_scenarios", lambda: kw.search_scenarios(query, user_id, limit)))
    if strategy in ("vector", "hybrid"):
        searches.append(("vector_messages", lambda: vec.search(query, "messages", session_id, limit)))
        searches.append(("vector_atoms", lambda: vec.search(query, "memory_atoms", session_id, limit)))
        searches.append(("vector_scenarios", lambda: vec.search(query, "memory_scenarios_v2", session_id, limit)))
    # Third signal: entity-boost (always active for hybrid)
    if strategy == "hybrid":
        ent = EntitySearcher(db)
        searches.append(("entity_boost", lambda: ent.search(query, user_id, limit)))

    # CPU-aware fallback: high load => keyword-only
    if strategy == "hybrid":
        try:
            load1 = os.getloadavg()[0]
            cpu_cnt = os.cpu_count() or 1
            if load1 > cpu_cnt * 1.5:
                logger.info("CPU load %.1f > %d cores * 1.5, fallback to keyword-only", load1, cpu_cnt)
                searches = [s for s in searches if s[0].startswith("keyword")]
        except Exception:
            pass

    for name, search_fn in searches:
        if time.monotonic() >= deadline:
            errors.append(f"{name}: deadline exceeded")
            break
        try:
            per_search_timeout = min(2.0, max(0.1, deadline - time.monotonic()))
            result = await asyncio.wait_for(
                search_fn(),
                timeout=per_search_timeout,
            )
            if result:
                results.append(result)
        except asyncio.TimeoutError:
            errors.append(f"{name}: timed out")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            logger.debug("ranker %s failed (continuing): %s", name, exc)

    if errors:
        logger.debug("recall partial errors: %s", "; ".join(errors))

    if strategy == "hybrid":
        fused = Ranker.fuse(results, k=k_rrf)
    else:
        # keyword or vector: flatten without fusion
        seen = set()
        fused = []
        for ranking in results:
            for hit in ranking:
                key = f"{hit.table}|{hit.id}"
                if key not in seen:
                    seen.add(key)
                    fused.append(hit)

    return fused[:limit]


async def recall_for_context(
    db: AsyncSession,
    query: str,
    user_id: str | None = None,
    limit: int = 5,
) -> list[Hit]:
    """为 context_builder 准备的快速召回（hybrid + 短超时）"""
    return await hybrid_recall(
        db,
        query,
        strategy="hybrid",
        user_id=user_id,
        limit=limit,
        timeout_ms=3000,
    )
