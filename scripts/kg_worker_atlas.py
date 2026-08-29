#!/usr/bin/env python3
"""KG worker - AtlasCloud V3.2-Exp 版（经 hk142 反代，kg_jobs 表，线程安全）"""
import json, os, re, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

DB_URL = "postgresql://postgres:postgres@postgres:5432/session_memory"
BASE_URL = os.getenv("ATLAS_BASE_URL", "http://103.240.198.142:18080/v1")
API_KEY = os.getenv("ATLAS_API_KEY", "apikey-03a16d10aec040208ac3597f0e529d8e")
MODEL = os.getenv("ATLAS_MODEL", "deepseek-ai/DeepSeek-V3.2-Exp")

ALLOWED_RELATIONS = {'depends_on','modifies','fixes','exemplifies','prefers_over','contradicts',
                     'reinforces','evolved_into','invalidated_by','leads_to','derived_from','part_of'}
CONFIDENCE_THRESHOLD = 0.7
BATCH = int(os.getenv("KG_BATCH", "150"))
MAX_WORKERS = int(os.getenv("KG_WORKERS", "20"))

def extract(content):
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=60)
    prompt = (
        "你是一个知识图谱提取引擎。只输出JSON对象，禁止markdown代码块，禁止解释。\n"
        "允许实体类型: file, function, concept, error, tool, person, product, technology\n"
        "允许关系: depends_on, modifies, fixes, exemplifies, prefers_over, contradicts, reinforces, evolved_into, invalidated_by, leads_to, derived_from, part_of\n"
        '严格按此格式输出: {"entities":[{"name":"x","type":"concept","confidence":0.9}],"relations":[{"source":"a","target":"b","type":"depends_on","confidence":0.8}]}\n'
        "要求: 实体名称必须在原文出现；置信度0-1，低于0.7不输出；只提取原文明确提到的信息。\n"
        "内容:\n" + content[:2500]
    )
    for attempt in range(3):
        try:
            r = client.chat.completions.create(
                model=MODEL, messages=[{"role":"user","content":prompt}],
                temperature=0, max_tokens=800
            )
            t = r.choices[0].message.content or ""
            m = re.search(r"\{[\s\S]*\}", t)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
            return {"entities": [], "relations": []}
        except Exception as e:
            if "429" in str(e) or "RateLimit" in type(e).__name__:
                time.sleep(2 * (attempt + 1))
                continue
            return None
    return None

def verify_entity_in_text(name, text):
    if not name or not text:
        return False
    return name.lower() in text.lower()

def process_job(job):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        result = extract(job["content"])
        if result is None:
            cur.execute("UPDATE kg_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                        ("Atlas extract failed", job["id"]))
            conn.commit()
            return False

        entity_ids = {}
        for e in result.get("entities", []):
            name = str(e.get("name", "")).strip()
            if not name:
                continue
            etype = str(e.get("type", "concept")).strip()
            try:
                conf = float(e.get("confidence", 1.0))
            except Exception:
                conf = 1.0
            if conf < CONFIDENCE_THRESHOLD:
                continue
            if not verify_entity_in_text(name, job["content"]):
                continue
            cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s", (job["session_id"], name))
            row = cur.fetchone()
            if row:
                entity_ids[name] = row["id"]
            else:
                eid = os.urandom(16).hex()
                cur.execute("INSERT INTO kg_entities (id,session_id,name,entity_type,level,created_at) VALUES (%s,%s,%s,%s,'L1',NOW())",
                            (eid, job["session_id"], name, etype))
                entity_ids[name] = eid
                try:
                    tid = os.urandom(16).hex()
                    cur.execute("INSERT INTO kg_extraction_sources (id,entity_id,message_id,source_text,confidence,extraction_model,created_at) VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                                (tid, eid, job["message_id"], job["content"][:500], conf, MODEL))
                except Exception:
                    pass

        for r in result.get("relations", []):
            src = str(r.get("source", "")).strip()
            tgt = str(r.get("target", "")).strip()
            rtype = str(r.get("type", "")).strip().lower()
            if rtype not in ALLOWED_RELATIONS:
                continue
            try:
                conf = float(r.get("confidence", 1.0))
            except Exception:
                conf = 1.0
            if conf < CONFIDENCE_THRESHOLD:
                continue
            si, ti = entity_ids.get(src), entity_ids.get(tgt)
            if not si or not ti:
                continue
            if not (verify_entity_in_text(src, job["content"]) and verify_entity_in_text(tgt, job["content"])):
                continue
            rid = os.urandom(16).hex()
            cur.execute("INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,relation_type,valid_at,created_at) VALUES (%s,%s,%s,%s,%s,NOW(),NOW()) ON CONFLICT DO NOTHING",
                        (rid, job["session_id"], si, ti, rtype))
            try:
                tid = os.urandom(16).hex()
                cur.execute("INSERT INTO kg_extraction_sources (id,relation_id,message_id,source_text,confidence,extraction_model,created_at) VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                            (tid, rid, job["message_id"], job["content"][:500], conf, MODEL))
            except Exception:
                pass

        cur.execute("UPDATE kg_jobs SET status='completed', model_used=%s, updated_at=now() WHERE id=%s",
                    (MODEL, job["id"]))
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
            cur.execute("UPDATE kg_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                        (f"{type(e).__name__}: {str(e)[:150]}", job["id"]))
            conn.commit()
        except Exception:
            pass
        return False
    finally:
        cur.close()
        conn.close()

def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT count(*) FROM kg_jobs WHERE status='pending'")
    pending = cur.fetchone()["count"]
    if pending == 0:
        conn.close()
        return
    cur.execute(
        "SELECT j.id, j.message_id, j.session_id, m.content "
        "FROM kg_jobs j JOIN messages m ON j.message_id = m.id "
        "WHERE j.status='pending' ORDER BY j.created_at LIMIT %s FOR UPDATE SKIP LOCKED",
        (BATCH,)
    )
    jobs = cur.fetchall()
    if not jobs:
        conn.close()
        return
    for job in jobs:
        cur.execute("UPDATE kg_jobs SET status='running', locked_at=now() WHERE id=%s", (job["id"],))
    conn.commit()
    conn.close()

    print(f"pending={pending} batch={len(jobs)} model={MODEL}", flush=True)
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(process_job, j): j for j in jobs}
        for f in as_completed(futs):
            if f.result():
                ok += 1
            else:
                fail += 1
    print(f"done: ok={ok} fail={fail}", flush=True)

if __name__ == "__main__":
    main()
