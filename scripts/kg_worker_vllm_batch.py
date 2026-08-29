#!/usr/bin/env python3
"""KG worker - BATCH extraction via vLLM/zen proxy (kg_jobs table, thread-safe).

Batch version: one API call extracts KG for N messages, multiplying throughput
per quota (e.g. deepseek-v4-flash 25 calls/min quota x batch N => Nx throughput).
Built from kg_worker_vllm.py, keeps per-job writeback with fallback to single
extract on per-item failure.
"""
import json, os, re, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

DB_URL = "postgresql://postgres:postgres@postgres:5432/session_memory"
BASE_URL = os.getenv("VLLM_BASE_URL", "http://10.100.1.15:8000/v1")
API_KEY = os.getenv("VLLM_API_KEY", "local-no-key")
MODEL = os.getenv("VLLM_MODEL", "/model")

ALLOWED_RELATIONS = {'depends_on','modifies','fixes','exemplifies','prefers_over','contradicts',
                     'reinforces','evolved_into','invalidated_by','leads_to','derived_from','part_of'}
CONFIDENCE_THRESHOLD = 0.7
BATCH = int(os.getenv("KG_BATCH", "8"))
MAX_WORKERS = int(os.getenv("KG_WORKERS", "8"))
KG_PARTITION = int(os.getenv("KG_PARTITION", "0"))
KG_TOTAL_PARTITIONS = int(os.getenv("KG_TOTAL_PARTITIONS", "1"))

def _safe_error_text(e):
    s = f"{type(e).__name__}: {str(e)[:150]}"
    return s.replace("%", "pct")

def _log_error(etype, err):
    try:
        with open("/tmp/kg_extract_errors.log", "a") as fh:
            fh.write("%s|%s|%s\n" % (time.strftime("%H:%M:%S"), etype, str(err)[:160].replace("\n", " ")))
    except Exception:
        pass

def extract_single(content, client):
    prompt = (
        "你是一个知识图谱提取引擎。只输出JSON对象，禁止markdown代码块，禁止解释。\n"
        "允许实体类型: file, function, concept, error, tool, person, product, technology\n"
        "允许关系: depends_on, modifies, fixes, exemplifies, prefers_over, contradicts, reinforces, evolved_into, invalidated_by, leads_to, derived_from, part_of\n"
        '严格按此格式输出: {"entities":[{"name":"x","type":"concept","confidence":0.9}],"relations":[{"source":"a","target":"b","type":"depends_on","confidence":0.8}]}\n'
        "要求: 实体名称必须在原文出现；置信度0-1，低于0.7不输出；只提取原文明确提到的信息。\n"
        "内容:\n" + content[:2500]
    )
    r = client.chat.completions.create(model=MODEL, messages=[{"role":"user","content":prompt}], temperature=0, max_tokens=480)
    choices = getattr(r, "choices", None) or []
    if not choices:
        raise RuntimeError("empty_model_choices")
    t = getattr(choices[0].message, "content", None) or ""
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        raise RuntimeError("llm_returned_non_json")
    data = json.loads(m.group(0))
    return {"entities": data.get("entities", []), "relations": data.get("relations", [])}

def extract_batch(contents):
    """One API call extracting KG for N messages. Returns dict {idx: {entities, relations}}."""
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=180)
    batch_text = "\n---\n".join(f"[{i}] {c[:2500]}" for i, c in enumerate(contents))
    prompt = (
        "你是知识图谱提取引擎。只输出JSON对象，禁止markdown代码块，禁止解释。\n"
        "内容是多条消息，用[序号]分隔，如 [0] 消息0 --- [1] 消息1。\n"
        "允许实体类型: file, function, concept, error, tool, person, product, technology\n"
        "允许关系: depends_on, modifies, fixes, exemplifies, prefers_over, contradicts, reinforces, evolved_into, invalidated_by, leads_to, derived_from, part_of\n"
        '对每条消息分别提取，按序号输出，格式: {"0":{"entities":[{"name":"x","type":"concept","confidence":0.9}],"relations":[]},"1":{"entities":[],"relations":[]}}\n'
        "要求: 实体名称必须在对应原文出现；置信度0-1，低于0.7不输出；没有实体的消息输出空entities/relations。\n"
        "内容:\n" + batch_text
    )
    for attempt in range(3):
        try:
            r = client.chat.completions.create(model=MODEL, messages=[{"role":"user","content":prompt}], temperature=0, max_tokens=min(2000, max(1200, len(contents) * 600)))
            choices = getattr(r, "choices", None) or []
            if not choices:
                raise RuntimeError("empty_model_choices")
            t = getattr(choices[0].message, "content", None) or ""
            m = re.search(r"\{[\s\S]*\}", t)
            if not m:
                raise RuntimeError("llm_returned_non_json")
            data = json.loads(m.group(0))
            # normalize: ensure each index key exists
            result = {}
            for i in range(len(contents)):
                item = data.get(str(i)) or data.get(i) or {}
                result[str(i)] = {
                    "entities": item.get("entities", []) if isinstance(item, dict) else [],
                    "relations": item.get("relations", []) if isinstance(item, dict) else [],
                }
            return result
        except Exception as e:
            err = str(e); etype = type(e).__name__
            _log_error(etype, err)
            transient = ("429" in err or "RateLimit" in etype or "rate" in err.lower()
                         or "empty_model_choices" in err or "llm_returned_" in err
                         or "timeout" in err.lower() or "Timeout" in etype
                         or "APIConnection" in etype or "APIStatus" in etype)
            if transient and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
    return None

def verify_entity_in_text(name, text):
    if not name or not text:
        return False
    return name.lower() in text.lower()

def _insert_source(cur, entity_id, relation_id, message_id, source_text, confidence, job_id):
    tid = os.urandom(16).hex()
    try:
        if entity_id:
            cur.execute(
                "INSERT INTO kg_extraction_sources (id,entity_id,message_id,source_text,confidence,extraction_model,created_at) VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                (tid, entity_id, message_id, source_text[:500], float(confidence), MODEL))
        elif relation_id:
            cur.execute(
                "INSERT INTO kg_extraction_sources (id,relation_id,message_id,source_text,confidence,extraction_model,created_at) VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                (tid, relation_id, message_id, source_text[:500], float(confidence), MODEL))
        else:
            raise ValueError("no source anchor")
        return True
    except Exception as e:
        try:
            cur.execute(
                "UPDATE kg_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                (f"{MODEL}: source_write_failed: {_safe_error_text(e)}", job_id))
        except Exception:
            pass
        return False

def writeback_job(job, result, cur):
    """Write entities/relations for one job from a parsed result dict."""
    entity_ids = {}
    for e in result.get("entities", []):
        try:
            name = str(e.get("name", "")).strip()
            etype = str(e.get("type", "concept")).strip() or "concept"
            conf = float(e.get("confidence", 1.0))
        except Exception:
            continue
        if not name or conf < CONFIDENCE_THRESHOLD or not verify_entity_in_text(name, job["content"]):
            continue
        cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s", (job["session_id"], name))
        row = cur.fetchone()
        if row:
            entity_ids[name] = row["id"]
            continue
        eid = os.urandom(16).hex()
        cur.execute(
            "INSERT INTO kg_entities (id,session_id,name,entity_type,level,created_at) VALUES (%s,%s,%s,%s,'L1',NOW())",
            (eid, job["session_id"], name, etype))
        entity_ids[name] = eid
        _insert_source(cur, eid, None, job["message_id"], job["content"], conf, job["id"])

    for rel in result.get("relations", []):
        try:
            src = str(rel.get("source", "")).strip()
            tgt = str(rel.get("target", "")).strip()
            rtype = str(rel.get("type", "")).strip().lower()
            conf = float(rel.get("confidence", 1.0))
        except Exception:
            continue
        if rtype not in ALLOWED_RELATIONS or conf < CONFIDENCE_THRESHOLD:
            continue
        si, ti = entity_ids.get(src), entity_ids.get(tgt)
        if not si or not ti or not verify_entity_in_text(src, job["content"]) or not verify_entity_in_text(tgt, job["content"]):
            continue
        rid = os.urandom(16).hex()
        cur.execute(
            "INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,relation_type,valid_at,created_at) VALUES (%s,%s,%s,%s,%s,NOW(),NOW()) ON CONFLICT DO NOTHING",
            (rid, job["session_id"], si, ti, rtype))
        _insert_source(cur, None, rid, job["message_id"], job["content"], conf, job["id"])

def process_batch(jobs):
    """One API call for the batch, then writeback each job in its own conn."""
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        batch_result = extract_batch([j["content"] for j in jobs])
        if batch_result is None:
            # whole batch failed at LLM layer -> mark all failed
            for j in jobs:
                try:
                    cur.execute("UPDATE kg_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                                (f"{MODEL}: batch_extract_failed", j["id"]))
                except Exception:
                    pass
            conn.commit()
            return 0
        ok = 0
        for idx, job in enumerate(jobs):
            res = batch_result.get(str(idx)) or {"entities": [], "relations": []}
            try:
                writeback_job(job, res, cur)
                cur.execute("UPDATE kg_jobs SET status='completed', model_used=%s, updated_at=now() WHERE id=%s", (MODEL, job["id"]))
                ok += 1
            except Exception as e:
                try:
                    cur.execute("UPDATE kg_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                                (f"{MODEL}: worker_exception: {_safe_error_text(e)}", job["id"]))
                except Exception:
                    pass
        conn.commit()
        return ok
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        cur.close()
        conn.close()

def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT count(*) FROM kg_jobs WHERE status='pending'")
        pending = cur.fetchone()["count"]
        if pending == 0:
            return
        cur.execute(
            "SELECT j.id, j.message_id, j.session_id, m.content "
            "FROM kg_jobs j JOIN messages m ON j.message_id = m.id "
            "WHERE j.status='pending' "
            "AND abs(hashtext(j.id::text)) %% %s = %s "
            "ORDER BY j.created_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (KG_TOTAL_PARTITIONS, KG_PARTITION, BATCH))
        jobs = cur.fetchall()
        if not jobs:
            return
        for job in jobs:
            cur.execute("UPDATE kg_jobs SET status='running', locked_at=now() WHERE id=%s", (job["id"],))
        conn.commit()
    finally:
        cur.close()
        conn.close()

    print(f"pending={len(jobs)} batch={len(jobs)} model={MODEL}", flush=True)
    ok = process_batch(jobs)
    fail = len(jobs) - ok
    print(f"done: ok={ok} fail={fail}", flush=True)

if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print(f"main error: {_safe_error_text(e)}", flush=True)
        time.sleep(1)