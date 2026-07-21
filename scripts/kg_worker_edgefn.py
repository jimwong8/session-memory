#!/usr/bin/env python3
"""KG extract worker using edgefn API"""
import argparse, json, os, re, time
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

DB_URL = os.getenv("KG_DB_URL", "postgresql://postgres:postgres@localhost:5432/session_memory")
BASE_URL = "https://api.edgefn.net/v1"
API_KEY = "sk-3F8BFExyLzMVEb6y833f178aDcEc445a94D7Ce00Ec5fEaD0"
MODEL = "DeepSeek-V3.2-EXP"

ALLOWED_RELATIONS = {'depends_on','modifies','fixes','exemplifies','prefers_over','contradicts','reinforces','evolved_into','invalidated_by','leads_to','derived_from','part_of'}

client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=30)

def extract(content):
    prompt = ("你是一个知识图谱提取引擎。只输出JSON，禁止markdown。\n"
              "允许实体类型: file, function, concept, error, tool, person, product, technology\n"
              "允许关系: depends_on, modifies, fixes, exemplifies, prefers_over, contradicts, reinforces, evolved_into, invalidated_by, leads_to, derived_from, part_of\n"
              '格式: {"entities":[{"name":"x","type":"concept"}],"relations":[{"source":"a","target":"b","type":"depends_on"}]}\n'
              "内容:\n" + content[:2500])
    try:
        r = client.chat.completions.create(model=MODEL, messages=[{"role":"user","content":prompt}], temperature=0, max_tokens=512)
        t = r.choices[0].message.content or ""
        # Try multiple extraction strategies
        for strategy in [
            lambda s: re.search(r"\{[\s\S]*\}", s),           # greedy JSON
            lambda s: re.search(r"\{[\s\S]*?\}(?=\s*$)", s),  # last JSON object
            lambda s: re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", s),  # balanced braces
        ]:
            m = strategy(t)
            if m:
                try:
                    return json.loads(m.group(0))
                except:
                    continue
        return {"entities":[],"relations":[]}
    except Exception as e:
        print("LLM err: "+str(e), flush=True); return None

def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT count(*) FROM kg_jobs WHERE status='pending' LIMIT 1")
    pending = cur.fetchone()["count"]
    print(f"pending={pending}", flush=True)
    if pending == 0: conn.close(); return
    cur.execute(
        "SELECT j.id, j.message_id, j.session_id, m.content "
        "FROM kg_jobs j JOIN messages m ON j.message_id = m.id "
        "WHERE j.status='pending' ORDER BY j.created_at LIMIT 5"
    )
    jobs = cur.fetchall()
    for job in jobs:
        if not job["content"]:
            cur.execute("UPDATE kg_jobs SET status='skipped' WHERE id=%s",(job["id"],))
            continue
        result = extract(job["content"])
        if result is None:
            cur.execute("UPDATE kg_jobs SET status='failed', last_error='LLM error' WHERE id=%s",(job["id"],))
            continue
        entity_ids = {}
        for e in result.get("entities",[]):
            name, etype = e.get("name","unknown"), e.get("type","concept")
            cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s",(job["session_id"],name))
            row = cur.fetchone()
            if row: entity_ids[name] = row["id"]
            else:
                eid = os.urandom(16).hex()
                cur.execute("INSERT INTO kg_entities (id,session_id,name,entity_type,level,created_at) VALUES (%s,%s,%s,%s,'L1',NOW())",(eid,job["session_id"],name,etype))
                entity_ids[name] = eid
        for r in result.get("relations",[]):
            src_name, tgt_name, rtype = r.get("source",""), r.get("target",""), r.get("type","")
            if rtype not in ALLOWED_RELATIONS: continue
            src_id = entity_ids.get(src_name)
            tgt_id = entity_ids.get(tgt_name)
            if not src_id or not tgt_id: continue
            cur.execute(
                "UPDATE kg_relations SET invalid_at=NOW(), version=COALESCE(version,1)+1 "
                "WHERE session_id=%s AND source_entity_id=%s AND target_entity_id=%s AND relation_type<>%s AND invalid_at IS NULL",
                (job["session_id"],src_id,tgt_id,rtype))
            rid = os.urandom(16).hex()
            cur.execute(
                "INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,relation_type,valid_at,created_at) "
                "VALUES (%s,%s,%s,%s,%s,NOW(),NOW())",
                (rid,job["session_id"],src_id,tgt_id,rtype))
        cur.execute("UPDATE kg_jobs SET status='completed' WHERE id=%s",(job["id"],))
        conn.commit()
        print(f"  KG:{job['id'][:8]}", flush=True)
        time.sleep(0.5)
    conn.close()
    print(f"done {len(jobs)}", flush=True)

if __name__ == "__main__":
    main()
