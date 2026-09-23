"""知识图谱构建与检索服务"""
import json
import logging
import re
import time
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.llm_client import chat_completion
from src.models import KGEntity, KGRelation, Message
from src.services.kg_governance import filter_and_dedup_entities

logger = logging.getLogger(__name__)
_KG_RATE_LIMIT_UNTIL = 0.0
_KG_RATE_LIMIT_COOLDOWN_SECONDS = 120

ALLOWED_ENTITY_TYPES = {"file", "function", "concept", "error"}
ALLOWED_RELATION_TYPES = {
    "modifies", "depends_on", "fixes", "exemplifies", "prefers_over",
    "contradicts", "reinforces", "evolved_into", "invalidated_by",
    "leads_to", "derived_from", "part_of",
}
# Extended relation types observed in production model outputs.
WHITELISTED_RELATION_TYPES = ALLOWED_RELATION_TYPES | {
    "calls", "implements", "contains", "uses", "creates", "provides",
    "supports", "includes", "defines", "verifies", "causes", "maps",
    "documents", "covers", "produces", "generates", "triggers",
    "executes", "indicates", "validates", "references", "configures",
    "is_type_of", "persists_to", "hosts", "connects", "exposes",
}

_ANALYSIS_PLAN_PREFIXES = (
    "**Execution Plan**",
    "[analyze-mode]",
    "**Understanding deployment requirements**",
    "**Proceeding with validations**",
    "**Searching for repository**",
    "**Planning targeted reads**",
    "**Consulting oracle details**",
    "我把这次请求读作 exploratory-",
    "Oracle 结论和我当前判断一致",
    "我已经把路径关系厘清了",
    "先快速扫描本地目录结构",
    "- **会话恢复链路验证（明确目标与单一事实源）**",
    "已定位到",
    "已按你的要求恢复",
    "已完成",
)

_ANALYSIS_PLAN_KEYWORDS = (
    "execution plan",
    "understanding deployment requirements",
    "analyze-mode",
    "scope lock",
    "repository-local sources",
    "planning targeted reads",
    "consulting oracle",
    "验证",
    "部署",
    "计划",
    "分析",
    "步骤",
    "风险",
    "目录结构",
    "单一事实源",
    "前端入口",
    "api/v1",
    "app/static",
    "app/main.py",
)


def _looks_like_non_kg_analysis_message(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return False

    if text.startswith(("{", "[", "```json")):
        return False

    if any(text.startswith(prefix) for prefix in _ANALYSIS_PLAN_PREFIXES):
        return True

    lowered = text.lower()
    keyword_hits = sum(1 for keyword in _ANALYSIS_PLAN_KEYWORDS if keyword in lowered or keyword in text)
    structure_hits = sum(1 for marker in ("## ", "### ", "\n- ", "\n1. ", "\n2. ", "**") if marker in text)

    if len(text) >= 1200 and keyword_hits >= 2 and structure_hits >= 2:
        return True
    if len(text) >= 600 and keyword_hits >= 3 and structure_hits >= 3:
        return True
    return False


class EntityExtractionResult(BaseModel):
    entities: list[dict[str, str]] = Field(description="提取的实体列表，每个实体包含 name/type")
    relations: list[dict[str, str]] = Field(description="提取的关系列表，包含 source/target/type")


class KnowledgeGraphService:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _result(
        self,
        *,
        ok: bool,
        reason: str,
        retryable: bool = False,
        noop: bool = False,
        detail: str | None = None,
        entity_count: int = 0,
        relation_count: int = 0,
    ) -> dict:
        return {
            "ok": ok,
            "reason": reason,
            "retryable": retryable,
            "noop": noop,
            "detail": detail,
            "entity_count": entity_count,
            "relation_count": relation_count,
        }

    def _fallback_parse_reasoning_text(self, raw_reply: str) -> dict | None:
        text = raw_reply.strip()
        entities: list[dict[str, str]] = []
        relations: list[dict[str, str]] = []
        seen_entities: set[tuple[str, str]] = set()
        relation_type_map = {"依赖": "depends_on", "修改": "modifies", "修复": "fixes", "例如": "exemplifies", "偏好": "prefers_over", "更喜欢": "prefers_over", "矛盾": "contradicts", "反驳": "contradicts", "强化": "reinforces", "支持": "reinforces", "演化": "evolved_into", "演化为": "evolved_into", "失效": "invalidated_by", "取代": "invalidated_by", "导致": "leads_to", "来源于": "derived_from", "源自": "derived_from", "属于": "part_of", "部分": "part_of"}

        for line in text.splitlines():
            stripped = line.strip()
            entity_match = re.search(r'^\s*(?:\d+[\.、)]|[-*])\s*(.+?)\s*[-—:：].*?类型为\s*([a-zA-Z_]+)', stripped, flags=re.IGNORECASE)
            if entity_match:
                name = entity_match.group(1).strip().strip(chr(96)).strip(chr(39)).strip(chr(34))
                ent_type = entity_match.group(2).strip().lower()
                if ent_type not in ALLOWED_ENTITY_TYPES:
                    ent_type = "concept"
                if name and (name, ent_type) not in seen_entities:
                    seen_entities.add((name, ent_type))
                    entities.append({"name": name[:160], "type": ent_type})

            relation_match = re.search(r'^\s*(?:\d+[\.、)]|[-*])?\s*(.+?)\s*和\s*(.+?)\s*[-—:：].*?(modifies|depends_on|fixes|exemplifies|prefers_over|contradicts|reinforces|evolved_into|invalidated_by|leads_to|derived_from|part_of|依赖|修改|修复|例如|偏好|更喜欢|矛盾|反驳|强化|支持|演化|演化为|失效|取代|导致|来源于|源自|属于|部分)', stripped, flags=re.IGNORECASE)
            if relation_match:
                source = relation_match.group(1).strip().strip(chr(96)).strip(chr(39)).strip(chr(34))
                target = relation_match.group(2).strip().strip(chr(96)).strip(chr(39)).strip(chr(34))
                rel_type = relation_match.group(3).strip().lower()
                rel_type = relation_type_map.get(rel_type, rel_type)
                if source and target and rel_type in ALLOWED_RELATION_TYPES:
                    relations.append({"source": source[:160], "target": target[:160], "type": rel_type})

        if entities or relations:
            return {"entities": entities[:20], "relations": relations[:30]}
        return None

    def _extract_json_payload(self, raw_reply: str) -> dict:
        cleaned = raw_reply.strip()
        without_closed_think = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
        if without_closed_think:
            cleaned = without_closed_think
        if "</think>" in cleaned:
            cleaned = cleaned.split("</think>", 1)[1].strip()
        if "<think>" in cleaned:
            tail = cleaned.split("<think>", 1)[1]
            first_json_marker = tail.find("```json")
            first_brace = tail.find("{")
            if first_json_marker != -1 and (first_brace == -1 or first_json_marker < first_brace):
                cleaned = tail[first_json_marker:]
            elif first_brace != -1:
                cleaned = tail[first_brace:]

        if "```json" in cleaned:
            cleaned = cleaned.split("```json", 1)[1]
        elif "```" in cleaned:
            cleaned = cleaned.split("```", 1)[1]
        if "```" in cleaned:
            cleaned = cleaned.split("```", 1)[0]

        first_brace = cleaned.find("{")
        last_brace = cleaned.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            cleaned = cleaned[first_brace:last_brace + 1]
        cleaned = cleaned.strip()

        if cleaned:
            try:
                return json.loads(cleaned)
            except Exception:
                pass

            decoder = json.JSONDecoder()
            for idx, ch in enumerate(cleaned):
                if ch != "{":
                    continue
                try:
                    data, _ = decoder.raw_decode(cleaned[idx:])
                    if isinstance(data, dict) and ("entities" in data or "relations" in data):
                        return data
                except Exception:
                    continue

            candidates = re.findall(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", cleaned, flags=re.DOTALL)
            for candidate in reversed(candidates):
                try:
                    data = json.loads(candidate)
                    if isinstance(data, dict) and ("entities" in data or "relations" in data):
                        return data
                except Exception:
                    continue

        fallback = self._fallback_parse_reasoning_text(raw_reply)
        if fallback is not None:
            logger.warning(
                "知识图谱模型未返回 JSON，已从推理文本保守恢复 entities=%s relations=%s",
                len(fallback.get("entities", [])),
                len(fallback.get("relations", [])),
            )
            return fallback
        raise ValueError(f"无法从模型返回中提取 JSON: {raw_reply[:500]}")

    async def list_entities(self, session_id=None, limit: int = 50):
        stmt = select(KGEntity).order_by(KGEntity.created_at.desc()).limit(limit)
        if session_id is not None:
            stmt = select(KGEntity).where(KGEntity.session_id == session_id).order_by(KGEntity.created_at.desc()).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_relations(self, session_id=None, limit: int = 50):
        stmt = select(KGRelation).order_by(KGRelation.created_at.desc()).limit(limit)
        if session_id is not None:
            stmt = select(KGRelation).where(KGRelation.session_id == session_id).order_by(KGRelation.created_at.desc()).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def search(self, query: str, session_id=None, top_k: int = 5):
        from sqlalchemy import text as sa_text, or_
        entity_ids = []

        # Detect Chinese characters (CJK range)
        has_chinese = any('\u4e00' <= c <= '\u9fff' for c in query)

        # Split at: whitespace, hyphen, underscore, slash, AND CJK/Latin boundaries
        # e.g., "PVE虚拟机" → ["PVE", "虚拟机"], "配置-transparent" → ["配置", "transparent"]
        query_spaced = re.sub(r'([\u4e00-\u9fff])([\w])', r'\1 \2', query)
        query_spaced = re.sub(r'([\w])([\u4e00-\u9fff])', r'\1 \2', query_spaced)
        words = [w.strip() for w in re.split(r'[\s\-_/]+', query_spaced) if w.strip() and len(w.strip()) > 1]
        if not words:
            words = [query.strip()] if query.strip() else [""]

        # Strategy 1: Full-text search (English only — simple config does not tokenize Chinese)
        if not has_chinese and words:
            ts_query = words[0] if len(words) == 1 else " & ".join(words)
            try:
                ft_result = await self.db.execute(
                    sa_text(
                        "SELECT id FROM kg_entities WHERE name_tsv @@ to_tsquery(\'simple\', :q) "
                        "ORDER BY ts_rank(name_tsv, to_tsquery(\'simple\', :q)) DESC LIMIT :lim"
                    ).bindparams(q=ts_query, lim=top_k * 3)
                )
                entity_ids = [row[0] for row in ft_result.all()]
            except Exception:
                pass
                try:
                    await self.db.rollback()
                except Exception:
                    pass

        entity_stmt = select(KGEntity)
        relation_stmt = select(KGRelation)

        if entity_ids:
            # Use FTS results
            entity_stmt = entity_stmt.where(KGEntity.id.in_(entity_ids))
        elif len(words) <= 1:
            # Single word: ILIKE substring (uses trigram GIN index)
            search_term = words[0] if words else query.strip()
            entity_stmt = entity_stmt.where(KGEntity.name.ilike(f"%{search_term}%"))
        else:
            # Multi-word (any language): match ANY word (OR semantics for broader recall)
            entity_stmt = entity_stmt.where(
                or_(*[KGEntity.name.ilike(f"%{w}%") for w in words])
            )

        if session_id is not None:
            entity_stmt = entity_stmt.where(KGEntity.session_id == session_id)
            relation_stmt = relation_stmt.where(KGRelation.session_id == session_id)

        entity_stmt = entity_stmt.limit(top_k)

        # Relations: match by any word (OR semantics)
        if words:
            relation_stmt = relation_stmt.where(
                or_(*[KGRelation.relation_type.ilike(f"%{w}%") for w in words])
            )
        relation_stmt = relation_stmt.limit(top_k)

        entity_result = await self.db.execute(entity_stmt)
        relation_result = await self.db.execute(relation_stmt)
        return list(entity_result.scalars().all()), list(relation_result.scalars().all())


    async def get_graph(self, session_id=None, limit: int = 80):
        entities = await self.list_entities(session_id=session_id, limit=limit)
        relations = await self.list_relations(session_id=session_id, limit=limit * 2)
        nodes = [
            {"id": str(e.id), "label": e.name, "type": e.entity_type, "session_id": e.session_id}
            for e in entities
        ]
        edges = [
            {
                "id": str(r.id),
                "source": str(r.source_entity_id),
                "target": str(r.target_entity_id),
                "relation_type": r.relation_type,
                "session_id": r.session_id,
            }
            for r in relations
        ]
        return nodes, edges

    async def extract_knowledge_result(self, message: Message) -> dict:
        global _KG_RATE_LIMIT_UNTIL
        if not message.content or len(message.content) < 50:
            return self._result(ok=True, noop=True, reason="content_too_short")
        if _looks_like_non_kg_analysis_message(message.content):
            logger.info("跳过知识图谱提取：命中分析/计划型消息特征")
            return self._result(ok=True, noop=True, reason="non_kg_analysis_message")
        if _KG_RATE_LIMIT_UNTIL > time.time():
            logger.info("跳过知识图谱提取：429 冷却窗口中")
            return self._result(ok=False, retryable=True, reason="rate_limited_cooldown")

        prompt = (
            "你是一个高度格式化的知识抽取引擎。必须直接输出 JSON 对象，绝对禁止包含任何 markdown 代码块标记或 <think> 标签。\n"
            '如果内容太短或无明显实体，必须返回 {"entities":[], "relations":[]}。\n'
            "允许的实体类型: file, function, concept, error\n"
            "允许的关系类型: modifies, depends_on, fixes, exemplifies(示例/例如), prefers_over(偏好/选择), contradicts(矛盾/反驳), reinforces(强化/支持), evolved_into(演化为), invalidated_by(被取代/失效), leads_to(导致), derived_from(源自), part_of(属于/部分)\n\n"
            "关系类型使用说明：\n"
            '- exemplifies: A 是 B 的示例 ("postgres exemplifies relational_db")\n'
            '- prefers_over: A 被选择超过 B ("chose_postgres prefers_over mongodb")\n'
            '- contradicts: A 与 B 矛盾 ("old_approach contradicts new_data")\n'
            '- reinforces: A 强化 B ("backup_plan reinforces reliability")\n'
            '- evolved_into: A 演化为 B ("prototype evolved_into production")\n'
            '- invalidated_by: A 被 B 取代 ("cached_result invalidated_by new_data")\n'
            '- leads_to: A 导致 B ("memory_leak leads_to crash")\n'
            '- derived_from: A 来源于 B ("config derived_from template")\n'
            '- part_of: A 是 B 的一部分 ("module part_of system")\n\n'
            '输出格式：{"entities": [{"name": "名字", "type": "concept"}], '
            '"relations": [{"source": "源", "target": "目标", "type": "depends_on"}]}\n\n'
            f"提取内容：\n{message.content[:2000]}"
        )
        try:
            reply, _ = await chat_completion([
                {"role": "system", "content": "你只能输出一个 JSON 对象，不能输出解释、markdown、<think> 或额外文本。"},
                {"role": "user", "content": prompt},
            ], temperature=0.0, max_tokens=3000, task_type="kg_extract")
            logger.info(f"知识图谱原始返回: {reply[:1000]}")
            data = self._extract_json_payload(reply)

            entities = data.get("entities", []) or []
            relations = data.get("relations", []) or []
            if not entities and not relations:
                return self._result(ok=True, noop=True, reason="empty_graph")

            # Phase 4: Filter noise + dedup
            entities = await filter_and_dedup_entities(self.db, entities, str(message.session_id))
            if not entities and not relations:
                return self._result(ok=True, noop=True, reason="all_filtered_noise")

            entity_map = {}
            entity_count = 0
            relation_count = 0
            for ent_data in entities:
                name = ent_data.get("name")
                ent_type = ent_data.get("type")
                if not name or not ent_type:
                    continue
                stmt = select(KGEntity).where(KGEntity.session_id == message.session_id, KGEntity.name == name)
                result = await self.db.execute(stmt)
                # Reuse the first row when historical imports duplicated an entity.
                existing = result.scalars().first()
                if existing:
                    entity_map[name] = existing.id
                else:
                    new_ent = KGEntity(session_id=message.session_id, name=name, entity_type=ent_type)
                    self.db.add(new_ent)
                    await self.db.flush()
                    entity_map[name] = new_ent.id
                    entity_count += 1

            for rel_data in relations:
                src_id = entity_map.get(rel_data.get("source"))
                tgt_id = entity_map.get(rel_data.get("target"))
                rel_type = rel_data.get("type")
                # Validate relation type against whitelist
                if rel_type and rel_type not in WHITELISTED_RELATION_TYPES:
                    logger.debug("Skipping invalid relation type: %s", rel_type)
                    continue
                if src_id and tgt_id and rel_type:
                    self.db.add(
                        KGRelation(
                            session_id=message.session_id,
                            source_entity_id=src_id,
                            target_entity_id=tgt_id,
                            relation_type=rel_type,
                            message_id=message.id,
                        )
                    )
                    relation_count += 1

            await self.db.commit()
            logger.info(f"成功为消息 {message.id} 提取知识图谱信息")
            return self._result(ok=True, reason="extracted", entity_count=entity_count, relation_count=relation_count)
        except Exception as e:
            text = str(e)
            if "429" in text or "Too Many Requests" in text:
                _KG_RATE_LIMIT_UNTIL = time.time() + _KG_RATE_LIMIT_COOLDOWN_SECONDS
                logger.warning(f"知识图谱提取触发 429 冷却，未来 {_KG_RATE_LIMIT_COOLDOWN_SECONDS} 秒跳过提取")
                await self.db.rollback()
                return self._result(ok=False, retryable=True, reason="rate_limited_upstream", detail=text[:500])
            if "无法从模型返回中提取 JSON" in text:
                logger.error(f"提取知识图谱信息失败: {e}")
                await self.db.rollback()
                return self._result(ok=False, retryable=False, reason="json_extract_failed", detail=text[:500])
            if "Redis 未连接" in text or "Redis 未连接，请先调用 connect()" in text:
                logger.error(f"提取知识图谱信息失败: {e}")
                await self.db.rollback()
                return self._result(ok=False, retryable=True, reason="cache_not_connected", detail=text[:500])
            lowered = text.lower()
            if any(token in lowered for token in ["sqlalchemy", "asyncpg", "psycopg", "constraint", "transaction", "rollback", "commit"]):
                logger.error(f"提取知识图谱信息失败: {e}")
                await self.db.rollback()
                return self._result(ok=False, retryable=True, reason="db_write_failed", detail=text[:500])
            logger.error(f"提取知识图谱信息失败: {e}")
            await self.db.rollback()
            return self._result(ok=False, retryable=True, reason="upstream_exception", detail=text[:500])

    async def extract_knowledge(self, message: Message) -> bool:
        result = await self.extract_knowledge_result(message)
        return bool(result.get("ok") or result.get("noop"))
