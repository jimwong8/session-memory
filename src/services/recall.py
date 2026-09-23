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
            try:
                await self.db.rollback()
            except Exception:
                pass
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
        # 优先 NIM nemotron (2048 -> 列 embedding_nv)，回退本地 bge-base (768 -> 列 embedding)。
        # messages / memory_atoms / memory_scenarios 三张表都已有 embedding_nv，
        # 故统一走 nemotron；失败再回落 768。（早期只有 messages 有该列，那时按表限制过。）
        # 注意：memory_atoms 的旧 embedding 列仍被 atom_builder 的向量去重使用，不动它。
        use_nim = table in ("messages", "memory_atoms", "memory_scenarios")
        emb_col = "embedding_nv" if use_nim else "embedding"
        embedding = None
        if use_nim:
            try:
                embedding = nim_embed_query(query)
            except Exception as exc:
                logger.warning("nim query embed 异常: %s", exc)
        if not embedding:
            emb_col = "embedding"
            try:
                embedding = await create_embedding(query)
            except Exception as exc:
                logger.warning("vector search embedding failed: %s", exc)
                try:
                    await self.db.rollback()
                except Exception:
                    pass
                return []

        if not embedding:
            logger.info("vector search skipped: embedding unavailable")
            return []
        dim = len(embedding)
        emb_str = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"

        # ⚠ 表名/内容列必须先归一化，再拼 where —— memory_scenarios 的列是
        #    session_ids(uuid[]) 和 narrative_md，**没有 session_id**；
        #    过去 where 在归一化之前就用死 session_id，导致每条 scenarios 检索都
        #    UndefinedColumnError（该告警在日志里刷了很久，非本次 NIM 改动引入）。
        content_col = "narrative_md" if table == "memory_scenarios_v2" else "content"
        if table == "memory_scenarios_v2":
            table = "memory_scenarios"
        conditions = []
        if session_id:
            if table == "memory_scenarios":
                conditions.append(f"'{session_id}'::uuid = ANY(session_ids)")
            else:
                conditions.append(f"session_id = '{session_id}'")
        if table == "memory_atoms":
            conditions.append("superseded_by IS NULL")
        where = " AND ".join(conditions) if conditions else "TRUE"

        sql = sa_text(f"""
            SELECT id, {content_col} AS content
            FROM {table}
            WHERE {where}
              AND {emb_col} IS NOT NULL
              AND vector_dims({emb_col}) = :dim
            ORDER BY {emb_col} <=> '{emb_str}'::vector
            LIMIT :lim
        """)

        try:
            result = await self.db.execute(sql.bindparams(lim=limit, dim=dim))
            rows = result.all()
        except Exception as exc:
            logger.warning("vector search failed on %s: %s", table, exc)
            try:
                await self.db.rollback()
            except Exception:
                pass
            rows = []

        # ⚠ 回归防护：embedding_nv 是【按时间从旧到新】回填的，未回填到的（较新）会话会命中 0 条。
        #   此时回落到 768 的 embedding 列重查一次，保证检索不会因切换而变空。
        if rows and emb_col == "embedding_nv":
            logger.info("vector(nim2048) %s hits=%d", table, len(rows))
        if not rows and emb_col == "embedding_nv":
            try:
                fb = await create_embedding(query)
                if fb and len(fb) == 768:
                    fb_str = "[" + ",".join(f"{v:.6f}" for v in fb) + "]"
                    sql_fb = sa_text(f"""
                        SELECT id, {content_col} AS content
                        FROM {table}
                        WHERE {where}
                          AND embedding IS NOT NULL
                          AND vector_dims(embedding) = :dim
                        ORDER BY embedding <=> '{fb_str}'::vector
                        LIMIT :lim
                    """)
                    result = await self.db.execute(sql_fb.bindparams(lim=limit, dim=768))
                    rows = result.all()
                    if rows:
                        logger.info("vector fallback -> embedding(768) on %s, %d hits", table, len(rows))
            except Exception as exc:
                logger.warning("vector fallback failed on %s: %s", table, exc)
                try:
                    await self.db.rollback()
                except Exception:
                    pass

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



# ---------------------------------------------------------------------------
# NIM 查询嵌入（nemotron-3-embed-1b, 2048 维）
# 存库用 input_type="passage"，检索必须用 "query" —— 用错会显著降低召回。
# 无 NIM key / 网络失败 / 超时都返回 None，调用方回退到本地 bge-base（768）。
# ---------------------------------------------------------------------------
import os as _os
import json as _json
import urllib.request as _urlreq
import random as _random

_NIM_URL = "https://integrate.api.nvidia.com/v1/embeddings"
_NIM_MODEL = "nvidia/nemotron-3-embed-1b"
# ⚠ key 文件必须放在【绑定挂载】里：容器被重建（不只是 restart）时可写层会丢，
#    /app/.nim_keys 就这样丢过一次 —— 后果是 nim_embed_query 静默返回 None、
#    所有检索悄悄回落 768，且没有任何报错（最难查的一类故障）。
#    /app/data/memory 是 bind mount（宿主 /home/jimwong/session-memory-backend/data/memory），
#    重建容器也不丢。按顺序取第一个存在的。
_NIM_KEYFILES = [
    _os.getenv("NIM_KEYFILE", ""),
    "/app/data/memory/.nim_keys",
    "/app/.nim_keys",
]
_NIM_KEYFILE = next((p for p in _NIM_KEYFILES if p and _os.path.exists(p)),
                    "/app/data/memory/.nim_keys")
_nim_keys_cache: list[str] | None = None

# 长连接客户端（避免每次查询重建 TLS）+ 查询向量 LRU 缓存
import httpx as _httpx
from collections import OrderedDict as _OD

_nim_cli = None


def _nim_client():
    global _nim_cli
    if _nim_cli is None:
        _nim_cli = _httpx.Client(http2=False, timeout=1.2,
                                 limits=_httpx.Limits(max_keepalive_connections=4,
                                                      max_connections=8))
    return _nim_cli


class _LRU(_OD):
    CAP = 256

    def get(self, k, default=None):
        if k in self:
            self.move_to_end(k)
            return _OD.__getitem__(self, k)
        return default

    def __setitem__(self, k, v):
        _OD.__setitem__(self, k, v)
        self.move_to_end(k)
        while len(self) > self.CAP:
            self.popitem(last=False)


_nim_cache = _LRU()


def _nim_keys() -> list[str]:
    global _nim_keys_cache
    if _nim_keys_cache is None:
        try:
            ks = [k.strip() for k in open(_NIM_KEYFILE).read().splitlines() if k.strip()]
            _random.shuffle(ks)
        except Exception:
            ks = []
        if ks:                      # 只有成功读到才缓存；空列表要允许下次重试，
            _nim_keys_cache = ks    # 否则 key 文件一旦暂时不可读就永久失效（踩过）
        else:
            return []
    return _nim_keys_cache


def nim_embed_query(text: str) -> list[float] | None:
    """用 NIM nemotron 做【查询】嵌入（2048 维）；任何失败返回 None。

    性能要点（实测）：
      * 每路检索只有 2 秒时间预算，而新建 TLS 连接要 ~0.5s ⇒ 必须【长连接】
      * 交互式查询高度重复 ⇒ 加 LRU 缓存，命中即 0 延迟
      * 回填任务在抢同一账号额度时单次可能 >2s ⇒ 缓存 + 缩短超时，宁可回退 768
    """
    if _os.getenv("SM_NIM_EMBED", "1") == "0":
        return None
    key = text[:500].strip()
    hit = _nim_cache.get(key)
    if hit is not None:
        return hit
    keys = _nim_keys()
    if not keys:
        return None
    payload = {
        "input": [text[:4000]],
        "model": _NIM_MODEL,
        "input_type": "query",
        "encoding_format": "float",
    }
    # ⚠ 绝对不要写 `with _nim_client() as cli:` —— with 会在退出时 close 掉这个
    #    模块级单例，之后每次查询都报 "Cannot reopen a client instance"（实测踩过）。
    try:
        cli = _nim_client()
        # ⚠ 每路检索总预算只有 2s：NIM 必须快速失败，否则整个检索被取消 -> 返回 0 条
        for attempt in range(1):
            k = keys[attempt % len(keys)]
            try:
                r = cli.post(_NIM_URL, json=payload,
                             headers={"Authorization": "Bearer " + k}, timeout=1.2)
                if r.status_code != 200:
                    logger.warning("nim embed HTTP %s: %s", r.status_code, r.text[:100])
                    continue
                vec = r.json()["data"][0]["embedding"]
                if vec and len(vec) == 2048:
                    out = [float(x) for x in vec]
                    _nim_cache[key] = out
                    return out
                logger.warning("nim embed 维度异常: %s", len(vec) if vec else None)
                return None
            except Exception as exc:
                logger.warning("nim embed 失败(第%d次): %s", attempt + 1, str(exc)[:100])
    except Exception as exc:
        logger.warning("nim client 异常: %s", str(exc)[:100])
    return None


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
            try:
                await self.db.rollback()
            except Exception:
                pass
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
            try:
                await self.db.rollback()
            except Exception:
                pass
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
            # A failed search may leave the shared session in a broken transaction;
            # roll back so subsequent searches on the same session can proceed.
            try:
                await db.rollback()
            except Exception as rb_exc:
                logger.debug("hybrid_recall rollback failed after %s: %s", name, rb_exc)

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








