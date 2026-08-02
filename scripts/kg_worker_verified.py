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
API_KEY = os.getenv("BACKUP_OPENAI_API_KEY", "sk-3F8...EaD0")
BASE_URL = os.getenv("BACKUP_OPENAI_BASE_URL", "https://api.edgefn.net/v1")

# Shared key pool (9 keys shared between R1 and KAT)
SHARED_KEYS = [
    "sk-gRD...Dd99",
    "sk-75W...0032",
    "sk-qK8...8b80",
    "sk-TUb...1c89",
    "sk-Ona...352d",
    "sk-a2j...1b60",
    "sk-EbE...DaF3",
    "sk-YJZ...D708",
    "sk-PaP...B1B0",
]

# Per-model key pools (model-specific keys + shared pool)
R1_KEYS = ["sk-9oe...1a93"] + SHARED_KEYS  # 10 keys
KAT_KEYS = ["sk-8Cs...8f4e"] + SHARED_KEYS  # 10 keys

MODEL_KEYS = {
    "DeepSeek-V4-Flash": "sk-HeE...166d",
}

MODELS = ["DeepSeek-V3.2-EXP", "KAT-Coder-Exp-72B-1010", "DeepSeek-R1-0528-Qwen3-8B", "DeepSeek-V4-Flash"]
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

def extract_with_model(model, content, api_key=None, max_retries=3):
    if api_key is None:
        api_key = API_KEY
    client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=30)
    
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
            t = r.choices[0].message.content or ""
            
            # 解析 JSON
            for strategy in [lambda s: re.search(r"\{[\s\S]*\}", s), lambda s: re.search(r"\{(?:[^{}]|{[^{}]*})*\}", s)]:
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

def filter_by_confidence(result, threshold):
    """根据置信度过滤结果"""
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

def process_job(job, model, api_key=None):
    """处理 KG 提取任务（带验证和溯源）"""
    result, err = extract_with_model(model, job["content"], api_key=api_key)
    
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
                record_traceability(
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
            record_traceability(
                relation_id=rid,
                message_id=job["message_id"],
                source_text=job["content"][:500],
                confidence=confidence,
                extraction_model=model
            )
    
    # 更新任务状态
    cur.execute(
        "UPDATE kg_jobs SET status='completed', updated_at=now() WHERE id=%s",
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

def record_traceability(entity_id=None, relation_id=None, message_id=None, 
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
    global conn, cur
    
    QPM = {
        "DeepSeek-V3.2-EXP": 16,
        "DeepSeek-R1-0528-Qwen3-8B": 100,
        "KAT-Coder-Exp-72B-1010": 60,
        "DeepSeek-V4-Flash": 18
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
    
    conn.close()
    print(f"done {len(jobs)}", flush=True)

def process_serial(model, job_list, api_key=None):
    """串行处理任务"""
    for job in job_list:
        try:
            process_job(job, model, api_key=api_key)
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

if __name__ == "__main__":
    main()
