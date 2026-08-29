#!/usr/bin/env python3
"""
KG extract worker - 生产级验证+溯源版
特性：
- 置信度阈值（≥0.7 才入库）
- 验证机制（实体/关系必须在原文中出现）
- 溯源记录（记录提取来源）
- 幻觉率监控
"""
import json, os, re, time, uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
import redis
import threading
import random

DB_URL = os.getenv("KG_DB_URL", "postgresql://postgres:postgres@postgres:5432/session_memory")
API_KEY = os.getenv("BACKUP_OPENAI_API_KEY", "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0")
BASE_URL = os.getenv("BACKUP_OPENAI_BASE_URL", "https://api.edgefn.net/v1")
MODEL_URLS = {
    "dots-studio/dots-3-note-prev-free": "https://api.atlascloud.ai/v1",
}

# Shared key pool (9 keys shared between R1 and KAT)
SHARED_KEYS = [
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
]

# Per-model key pools (model-specific keys + shared pool)
R1_KEYS = ["sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0"] + SHARED_KEYS  # 10 keys
KAT_KEYS = ["sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0"] + SHARED_KEYS  # 10 keys

MODEL_KEYS = {
    "dots-studio/dots-3-note-prev-free": "apikey-03a16d10aec040208ac3597f0e529d8e",
}

MODELS = ["DeepSeek-V3.2-EXP", "KAT-Coder-Exp-72B-1010", "DeepSeek-R1-0528-Qwen3-8B", "dots-studio/dots-3-note-prev-free"]
MULTI_KEY_MODELS = {"DeepSeek-R1-0528-Qwen3-8B": R1_KEYS, "KAT-Coder-Exp-72B-1010": KAT_KEYS}

ALLOWED_RELATIONS = {'depends_on','modifies','fixes','exemplifies','prefers_over','contradicts','reinforces','evolved_into','invalidated_by','leads_to','derived_from','part_of'}

# 验证配置
CONFIDENCE_THRESHOLD = 0.7  # 置信度阈值
VERIFICATION_ENABLED = True  # 是否启用验证
TRACEABILITY_ENABLED = True  # 是否启用溯源

R = redis.Redis(host="redis", port=6379, db=0, socket_timeout=3)

def _incr(metric, route, amount=1):
    try:
        key = f"llm:route:{metric}:kg:{route}"
        R.incrby(key, amount)
        R.expire(key, 86400)
    except Exception:
        pass

def _incr_verification(metric, amount=1):
    """记录验证统计"""
    try:
        key = f"kg:verification:{metric}"
        R.incrby(key, amount)
        R.expire(key, 86400)
    except Exception:
        pass

def verify_entity_in_text(entity_name, original_text):
    """验证实体是否在原文中出现"""
    if not entity_name or not original_text:
        return False
    
    # 精确匹配（不区分大小写）
    if entity_name.lower() in original_text.lower():
        return True
    
    # 部分匹配（实体名称长度 ≥ 3 且包含在原文中）
    if len(entity_name) >= 3 and entity_name.lower() in original_text.lower():
        return True
    
    # 允许合理推断（如 "Python" → "编程语言"）
    # 这里可以添加更多规则
    
    return False

def verify_relation_in_text(source, target, relation_type, original_text):
    """验证关系是否在原文中有依据"""
    if not source or not target or not original_text:
        return False
    
    # 检查源和目标是否都在原文中出现
    source_exists = verify_entity_in_text(source, original_text)
    target_exists = verify_entity_in_text(target, original_text)
    
    if not source_exists or not target_exists:
        return False
    
    # 对于某些关系类型，可以尝试在原文中找到相关表述
    # 例如 "depends_on" 可能在原文中有 "依赖"、"需要" 等表述
    
    return True

def extract_with_model(model, content, api_key=None, base_url=None, max_retries=3):
    # reasoning-model fast path (dots-3-note-prev-free): different prompt + cleanup
    if model == DOTS_MODEL:
        return extract_dots_model(model, content, api_key, base_url)
    if api_key is None:
        api_key = API_KEY
    base_url = MODEL_URLS.get(model, BASE_URL)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=30)
    
    # 修改 prompt 要求置信度
    prompt = (
        "你是一个知识图谱提取引擎。只输出JSON，禁止markdown。\n"
        "允许实体类型: file, function, concept, error, tool, person, product, technology\n"
        "允许关系: depends_on, modifies, fixes, exemplifies, prefers_over, contradicts, reinforces, evolved_into, invalidated_by, leads_to, derived_from, part_of\n"
        "\n"
        "格式:\n"
        '{"entities":[{"name":"x","type":"concept","confidence":0.9}],'
        '"relations":[{"source":"a","target":"b","type":"depends_on","confidence":0.8}]}\n'
        "\n"
        "要求:\n"
        "- 实体名称必须在原文中出现（或合理推断）\n"
        "- 关系必须基于原文中的明确表述\n"
        "- 置信度 0-1，低于 0.7 的不要输出\n"
        "- 只提取原文中明确提到的信息，不要推断\n"
        "\n"
        "内容:\n" + content[:2500]
    )
    
    last_err = None
    for attempt in range(max_retries):
        try:
            r = client.chat.completions.create(
                model=model, 
                messages=[{"role":"user","content":prompt}], 
                temperature=0, 
                max_tokens=512
            )
            _incr("hit", model)
            msg = r.choices[0].message
            t = (getattr(msg, "content", None) or getattr(msg, "reasoning_content", None) or "")

            # 解析 JSON
            for strategy in [lambda s: re.search(r"\\{[\s\S]*\}", s), lambda s: re.search(r"{(?:[^{}]|{[^{}]*})*}", s)]:
                m = strategy(t)
                if m:
                    try:
                        result = json.loads(m.group(0))
                        # 验证置信度
                        if VERIFICATION_ENABLED:
                            result = filter_by_confidence(result, CONFIDENCE_THRESHOLD)
                        return result, None
                    except:
                        continue

            return {"entities":[],"relations":[]}, None

        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            if "429" in str(e) or "RateLimit" in type(e).__name__:
                time.sleep(2 * (attempt + 1))
                continue
            _incr("fail", model)
            return None, last_err


# ---------------------------------------------------------------------------
# dots-studio/dots-3-note-prev-free specific handling
# This AtlasCloud model is a REASONING model. With thinking ENABLED it burns its
# whole token budget on chain-of-thought and never emits JSON, so we MUST disable
# thinking via extra_body={"enable_thinking": False}. With thinking off it returns
# clean entities/relations in `content`. Relation `type` values are free verbs, so
# we normalize them to the 12 allowed relation types before storage.
# ---------------------------------------------------------------------------

DOTS_MODEL = "dots-studio/dots-3-note-prev-free"

ALLOWED_REL_SET = {'depends_on','modifies','fixes','exemplifies','prefers_over',
                   'contradicts','reinforces','evolved_into','invalidated_by',
                   'leads_to','derived_from','part_of'}

_VALID_ENTITY_TYPES = {'file','function','concept','error','tool','person',
                       'product','technology'}

# map common free-form verbs the model emits -> allowed relation type
RELATION_VERB_MAP = {
    'depends_on':'depends_on','uses':'depends_on','used_by':'depends_on',
    'requires':'depends_on','needs':'depends_on','relies_on':'depends_on',
    'deploy':'depends_on','deploys':'depends_on','deployed_to':'depends_on',
    'deploy_to':'depends_on','install':'depends_on','installed_on':'depends_on',
    'runs_on':'depends_on','hosts':'depends_on','configures':'depends_on',
    'configured':'depends_on','setup':'depends_on','builds':'depends_on',
    'modifies':'modifies','edits':'modifies','changes':'modifies','updates':'modifies',
    'fixes':'fixes','resolves':'fixes','patches':'fixes','repairs':'fixes',
    'exemplifies':'exemplifies','demonstrates':'exemplifies','shows':'exemplifies',
    'prefers_over':'prefers_over','prefers':'prefers_over','chooses':'prefers_over',
    'contradicts':'contradicts','conflicts':'contradicts','opposes':'contradicts',
    'reinforces':'reinforces','supports':'reinforces','validates':'reinforces',
    'evolved_into':'evolved_into','became':'evolved_into','migrated_to':'evolved_into',
    'invalidated_by':'invalidated_by','disproves':'invalidated_by','breaks':'invalidated_by',
    'leads_to':'leads_to','causes':'leads_to','triggers':'leads_to','results_in':'leads_to',
    'derived_from':'derived_from','comes_from':'derived_from','based_on':'derived_from',
    'part_of':'part_of','contains':'part_of','belongs_to':'part_of','member_of':'part_of',
    'included_in':'part_of',
}


def _dots_prompt(content):
    """Strict prompt (English) proven to make the model emit clean JSON with
    thinking disabled."""
    return (
        "Extract a knowledge graph as JSON from the text. Use exactly this structure "
        "and nothing else:\\n"
        '{"entities":[{"name":STR,"type":STR,"confidence":FLOAT}],'
        '"relations":[{"source":STR,"target":STR,"type":STR,"confidence":FLOAT}]}\\n'
        "entity type must be one of: file, function, concept, error, tool, person, "
        "product, technology\\n"
        "relation type must be one of: depends_on, modifies, fixes, exemplifies, "
        "prefers_over, contradicts, reinforces, evolved_into, invalidated_by, "
        "leads_to, derived_from, part_of\\n"
        "Entity names must literally appear in the text. Output JSON only, no markdown.\\n"
        "Text: " + content[:2500]
    )


def _normalize_rel_type(verb):
    v = str(verb or "").strip().lower()
    if v in ALLOWED_REL_SET:
        return v
    return RELATION_VERB_MAP.get(v, "depends_on")


def _coerce_to_entities_relations(obj):
    """Accept entities/relations OR nodes/edges OR subject/predicate/object and
    normalize to the expected entities/relations shape. Drops invalid entries."""
    if not isinstance(obj, dict):
        return None
    ents = obj.get("entities")
    rels = obj.get("relations")
    if (ents is None) and (rels is None):
        nodes = obj.get("nodes") or obj.get("vertices") or []
        edges = obj.get("edges") or obj.get("links") or []
        subs = obj.get("triples") or []
        if nodes or edges:
            ents, rels = [], []
            name_by_id = {}
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                nid = str(n.get("id") or n.get("label") or "").strip()
                label = str(n.get("label") or n.get("name") or nid).strip()
                if not label:
                    continue
                name_by_id[nid] = label
                etype = str(n.get("type") or n.get("entity_type") or "concept").strip().lower()
                if etype not in _VALID_ENTITY_TYPES:
                    etype = "concept"
                try:
                    conf = float(n.get("confidence", 0.9))
                except Exception:
                    conf = 0.9
                ents.append({"name": label, "type": etype, "confidence": conf})
            for e in edges:
                if not isinstance(e, dict):
                    continue
                s = str(e.get("source") or e.get("from") or "").strip()
                t = str(e.get("target") or e.get("to") or "").strip()
                s = name_by_id.get(s, s); t = name_by_id.get(t, t)
                if not s or not t:
                    continue
                rels.append({"source": s, "target": t,
                             "type": _normalize_rel_type(e.get("type") or e.get("label")),
                             "confidence": _safe_conf(e.get("confidence", 0.9))})
        elif subs:
            ents, rels = [], []
            seen = set()
            for tri in subs:
                if not isinstance(tri, dict):
                    continue
                s = str(tri.get("subject") or tri.get("source") or "").strip()
                o = str(tri.get("object") or tri.get("target") or "").strip()
                pred = _normalize_rel_type(tri.get("predicate") or tri.get("relation") or tri.get("type"))
                for nm in (s, o):
                    if nm and nm not in seen:
                        seen.add(nm)
                        ents.append({"name": nm, "type": "concept", "confidence": 0.9})
                if s and o:
                    rels.append({"source": s, "target": o, "type": pred, "confidence": 0.9})
    if ents is None and rels is None:
        return None
    if not isinstance(ents, list):
        ents = []
    if not isinstance(rels, list):
        rels = []
    clean_e, seen = [], set()
    for e in ents:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        if not name or name in seen:
            continue
        etype = str(e.get("type") or "concept").strip().lower()
        if etype not in _VALID_ENTITY_TYPES:
            etype = "concept"
        seen.add(name)
        clean_e.append({"name": name, "type": etype, "confidence": _safe_conf(e.get("confidence", 0.9))})
    clean_r = []
    names = {e["name"] for e in clean_e}
    for r in rels:
        if not isinstance(r, dict):
            continue
        s = str(r.get("source") or r.get("subject") or "").strip()
        t = str(r.get("target") or r.get("object") or "").strip()
        if s not in names or t not in names:
            continue
        rtype = _normalize_rel_type(r.get("type") or r.get("predicate"))
        if rtype not in ALLOWED_REL_SET:
            continue
        clean_r.append({"source": s, "target": t, "type": rtype,
                        "confidence": _safe_conf(r.get("confidence", 0.9))})
    return {"entities": clean_e, "relations": clean_r}




def filter_by_confidence(result, threshold):
    """Filter KG result by confidence threshold."""
    filtered_entities = []
    for entity in result.get("entities", []):
        confidence = entity.get("confidence", 1.0)
        if confidence >= threshold:
            filtered_entities.append(entity)
        else:
            _incr_verification("filtered_low_confidence_entity")
    filtered_relations = []
    for relation in result.get("relations", []):
        confidence = relation.get("confidence", 1.0)
        if confidence >= threshold:
            filtered_relations.append(relation)
        else:
            _incr_verification("filtered_low_confidence_relation")
    return {
        "entities": filtered_entities,
        "relations": filtered_relations
    }

def _safe_conf(v, default=0.9):
    try:
        return float(v)
    except Exception:
        return default


def extract_dots_model(model, content, api_key, base_url):
    """Extraction path for dots-studio/dots-3-note-prev-free (thinking disabled)."""
    client = OpenAI(api_key=api_key, base_url=base_url or "https://api.atlascloud.ai/v1", timeout=120)
    last_err = None
    for attempt in range(3):
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": _dots_prompt(content)}],
                temperature=0,
                max_tokens=2000,
                extra_body={"enable_thinking": False},
            )
            _incr("hit", model)
            msg = r.choices[0].message
            t = (getattr(msg, "content", None) or getattr(msg, "reasoning_content", None) or "")
            candidates = []
            for strat in [lambda s: re.findall(r"\{[\s\S]*\}", s),
                          lambda s: re.findall(r"\{(?:[^{}]|[^{}]*)*\}", s)]:
                found = strat(t)
                if found:
                    candidates.extend(found)
            result = None
            for cand in candidates:
                try:
                    obj = json.loads(cand)
                except Exception:
                    continue
                norm = _coerce_to_entities_relations(obj)
                if norm and (norm["entities"] or norm["relations"]):
                    result = norm
                    break
            if result is None:
                return {"entities": [], "relations": []}, None
            if VERIFICATION_ENABLED:
                result = filter_by_confidence(result, CONFIDENCE_THRESHOLD)
            return result, None
        except Exception as ex:
            last_err = f"{type(ex).__name__}: {str(ex)[:120]}"
            # AtlasCloud is intermittently flaky (transient 400/timeout); retry all
            time.sleep(2 * (attempt + 1))
            continue
    return None, last_err

def process_job(job, model, api_key, conn, cur):
    """处理 KG 提取任务（带验证和溯源）"""
    base_url = MODEL_URLS.get(model)
    result, err = extract_with_model(model, job["content"], api_key=api_key, base_url=base_url)
    
    if result is None:
        cur.execute(
            "UPDATE kg_jobs SET status='failed', last_error=%s WHERE id=%s",
            (f"LLM({model[:6]}): {err}" if err else f"LLM({model[:6]}) error", job["id"])
        )
        conn.commit()
        return False
    
    entity_ids = {}
    verified_count = 0
    total_count = 0
    
    for e in result.get("entities", []):
        name = e.get("name", "unknown")
        etype = e.get("type", "concept")
        confidence = e.get("confidence", 1.0)
        total_count += 1
        
        # 验证实体是否在原文中出现
        if VERIFICATION_ENABLED:
            if not verify_entity_in_text(name, job["content"]):
                _incr_verification("rejected_entity_not_in_text")
                continue
            else:
                _incr_verification("verified_entity")
                verified_count += 1
        
        # 检查实体是否已存在
        cur.execute(
            "SELECT id FROM kg_entities WHERE session_id=%s AND name=%s",
            (job["session_id"], name)
        )
        row = cur.fetchone()
        
        if row:
            entity_ids[name] = row["id"]
        else:
            eid = os.urandom(16).hex()
            cur.execute(
                "INSERT INTO kg_entities (id,session_id,name,entity_type,level,created_at) "
                "VALUES (%s,%s,%s,%s,'L1',NOW())",
                (eid, job["session_id"], name, etype)
            )
            entity_ids[name] = eid
            
            # 记录溯源信息
            if TRACEABILITY_ENABLED:
                record_traceability(cur,
                    entity_id=eid,
                    message_id=job["message_id"],
                    source_text=job["content"][:500],  # 存储前500字符
                    confidence=confidence,
                    extraction_model=model
                )
    
    for r in result.get("relations", []):
        src_name = r.get("source", "")
        tgt_name = r.get("target", "")
        rtype = r.get("type", "")
        confidence = r.get("confidence", 1.0)
        total_count += 1
        
        if rtype not in ALLOWED_RELATIONS:
            continue
        
        src_id = entity_ids.get(src_name)
        tgt_id = entity_ids.get(tgt_name)
        
        if not src_id or not tgt_id:
            continue
        
        # 验证关系是否在原文中有依据
        if VERIFICATION_ENABLED:
            if not verify_relation_in_text(src_name, tgt_name, rtype, job["content"]):
                _incr_verification("rejected_relation_not_in_text")
                continue
            else:
                _incr_verification("verified_relation")
                verified_count += 1
        
        # 更新旧关系
        cur.execute(
            "UPDATE kg_relations SET invalid_at=NOW(), version=COALESCE(version,1)+1 "
            "WHERE session_id=%s AND source_entity_id=%s AND target_entity_id=%s "
            "AND relation_type<>%s AND invalid_at IS NULL",
            (job["session_id"], src_id, tgt_id, rtype)
        )
        
        # 插入新关系
        rid = os.urandom(16).hex()
        cur.execute(
            "INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,"
            "relation_type,valid_at,created_at) VALUES (%s,%s,%s,%s,%s,NOW(),NOW())",
            (rid, job["session_id"], src_id, tgt_id, rtype)
        )
        
        # 记录溯源信息
        if TRACEABILITY_ENABLED:
            record_traceability(cur,
                relation_id=rid,
                message_id=job["message_id"],
                source_text=job["content"][:500],
                confidence=confidence,
                extraction_model=model
            )
    
    # 更新任务状态
    cur.execute(
        "UPDATE kg_jobs SET status='completed', model_used=%s, updated_at=now() WHERE id=%s",
        (job["id"],)
    )
    conn.commit()
    
    # 记录验证统计
    if VERIFICATION_ENABLED:
        _incr_verification("total_processed")
        if verified_count > 0:
            _incr_verification("total_verified")
            hallucination_rate = 1 - (verified_count / total_count) if total_count > 0 else 0
            _incr_verification("hallucination_rate_sum", int(hallucination_rate * 100))
    
    print(f"  KG:{job['id'][:8]} [{model[:20]}] verified:{verified_count}/{total_count}", flush=True)
    return True

def record_traceability(cur, entity_id=None, relation_id=None, message_id=None, 
                       source_text="", confidence=1.0, extraction_model=""):
    """记录溯源信息"""
    try:
        trace_id = os.urandom(16).hex()
        cur.execute(
            "INSERT INTO kg_extraction_sources "
            "(id, entity_id, relation_id, message_id, source_text, confidence, extraction_model, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())",
            (trace_id, entity_id, relation_id, message_id, source_text, confidence, extraction_model)
        )
    except Exception as e:
        print(f"  [WARN] 记录溯源失败: {e}", flush=True)

def main():
    """主函数"""
    QPM = {
        "DeepSeek-V3.2-EXP": 16,
        "DeepSeek-R1-0528-Qwen3-8B": 100,
        "KAT-Coder-Exp-72B-1010": 60,
        "dots-studio/dots-3-note-prev-free": 18
    }
    total_w = sum(QPM.values())
    
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT count(*) FROM kg_jobs WHERE status='pending' LIMIT 1")
    pending = cur.fetchone()["count"]
    if pending == 0:
        conn.close()
        return
    
    print(f"pending={pending}", flush=True)
    
    BATCH = 50  # 增加批次大小
    cur.execute(
        "SELECT j.id, j.message_id, j.session_id, m.content "
        "FROM kg_jobs j JOIN messages m ON j.message_id = m.id "
        "WHERE j.status='pending' ORDER BY j.created_at LIMIT %s",
        (BATCH,)
    )
    jobs = cur.fetchall()
    if not jobs:
        conn.close()
        return
    
    # 标记为运行中
    for job in jobs:
        cur.execute(
            "UPDATE kg_jobs SET status='running', locked_at=now() WHERE id=%s",
            (job["id"],)
        )
    conn.commit()
    conn.close()  # 主连接用完即关，线程用独立连接
    
    n = len(jobs)
    alloc = {m: 0 for m in MODELS}
    
    # 按 QPM 权重分配
    for i in range(n):
        best, best_r = None, float("inf")
        for m in MODELS:
            ratio = (alloc[m] + 1) / QPM[m]
            if ratio < best_r:
                best_r, best = ratio, m
        alloc[best] += 1
    
    model_queue = []
    for m in MODELS:
        model_queue += [m] * alloc[m]
    random.seed(0)
    random.shuffle(model_queue)
    
    by_model = {m: [] for m in MODELS}
    for job, m in zip(jobs, model_queue):
        by_model[m].append(job)
    
    print(f"alloc={ {k: len(v) for k,v in by_model.items()} }", flush=True)
    
    threads = []
    
    # Multi-key models: each key gets its own thread
    for m in MODELS:
        mjobs = by_model.get(m, [])
        if not mjobs:
            continue
        
        keys = MULTI_KEY_MODELS.get(m)
        if keys and len(keys) > 1:
            buckets = {k: [] for k in keys}
            for i, job in enumerate(mjobs):
                buckets[keys[i % len(keys)]].append(job)
            for k, kjobs in buckets.items():
                if kjobs:
                    t = threading.Thread(target=process_serial, args=(m, kjobs, k))
                    t.start()
                    threads.append(t)
            print(f"{m[:20]}: {len(mjobs)} jobs / {len(keys)} keys", flush=True)
        else:
            api_key = API_KEY
            if m in MODEL_KEYS:
                api_key = MODEL_KEYS[m]
            elif m in MULTI_KEY_MODELS:
                api_key = MULTI_KEY_MODELS[m][0]
            t = threading.Thread(target=process_serial, args=(m, mjobs, api_key))
            t.start()
            threads.append(t)
    
    for t in threads:
        t.join()
    
    print(f"done {len(jobs)}", flush=True)

def process_serial(model, job_list, api_key=None):
    """串行处理任务（每线程独立连接）"""
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    for job in job_list:
        try:
            process_job(job, model, api_key, conn, cur)
        except Exception as e:
            print(f"  [ERROR] 处理失败 {job['id'][:8]}: {e}", flush=True)
            try:
                cur.execute(
                    "UPDATE kg_jobs SET status='failed', last_error=%s WHERE id=%s",
                    (str(e)[:200], job["id"])
                )
                conn.commit()
            except:
                pass
    conn.close()

if __name__ == "__main__":
    main()
