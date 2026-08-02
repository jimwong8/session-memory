#!/usr/bin/env python3
"""AtlasCloud V3.2-Exp KG Worker - fixed: skip on repeated failures"""
import time, json, re, uuid, requests, psycopg2
from psycopg2.extras import RealDictCursor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DB = "host=127.0.0.1 port=5432 dbname=session_memory user=postgres"
AC_URL = "https://api.atlascloud.ai/v1"
AC_KEY = "apikey-03a16d10aec040208ac3597f0e529d8e"
MODEL = "deepseek-ai/DeepSeek-V3.2-Exp"

import urllib3
urllib3.disable_warnings()

session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))
session.verify = False

def clean(text):
    """Strip Hermes artifacts that confuse the model"""
    text = re.sub(r'\[SILENT\]', '', text)
    text = re.sub(r'\[IMPORTANT:.*?\]', '', text, flags=re.DOTALL)
    text = re.sub(r'DELIVERY:.*?(\n|$)', '', text)
    text = re.sub(r'```\s*\n.*?```', '', text, flags=re.DOTALL)
    return text.strip()

def extract(text):
    text = clean(text)
    prompt = "Extract KG JSON: entities:[name,type],relations:[source,target,type]. Types: person,product,technology,concept,file,function,error,tool,command,system. Relations: depends_on,modifies,fixes,uses,created_by,leads_to. Output ONLY JSON. Text: " + text[:1500]
    try:
        r = session.post(AC_URL + "/chat/completions", json={
            "model": MODEL, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1000, "temperature": 0
        }, headers={"Authorization": "Bearer " + AC_KEY}, timeout=60)
        if r.status_code == 200:
            raw = r.json()["choices"][0]["message"]["content"]
            raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(raw[start:end+1])
                except:
                    pass
    except Exception as e:
        print(f"  err: {str(e)[:60]}", flush=True)
    return None

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--partition", type=int, default=0)
    parser.add_argument("--total", type=int, default=1)
    args = parser.parse_args()
    
    print(f"[P{args.partition}] model={MODEL}", flush=True)
    conn = psycopg2.connect(DB, cursor_factory=RealDictCursor)
    conn.autocommit = False
    cur = conn.cursor()
    processed = 0
    fails = 0
    skip_count = 0
    
    while True:
        cur.execute("""
            SELECT id, content, session_id 
            FROM messages 
            WHERE metadata_json->>'kg_extract_pending'='true' AND length(content)>=50 
            ORDER BY id 
            LIMIT 1 
            FOR UPDATE SKIP LOCKED
        """)
        row = cur.fetchone()
        if not row:
            break
        
        mid = str(row['id'])
        print(f"[P{args.partition}] {mid[:20]}...", flush=True)
        
        data = extract(row["content"] or "")
        if not data:
            fails += 1
            if fails >= 10:
                cur.execute("UPDATE messages SET metadata_json=metadata_json||'{\"kg_extract_status\":\"failed\"}' WHERE id=%s", (mid,))
                conn.commit()
                skip_count += 1
                fails = 0
                print(f"  [P{args.partition}] SKIP {mid[:20]}... (after 10 fails, total_skipped={skip_count})", flush=True)
            else:
                conn.rollback()
                time.sleep(1)
            continue
        
        fails = 0
        emap = {}
        for ent in data.get("entities", []):
            name = str(ent.get("name", ""))[:255]
            if not name: continue
            etype = str(ent.get("type", "concept"))[:50]
            cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s", (row["session_id"], name))
            er = cur.fetchone()
            if er:
                emap[name] = er["id"]
            else:
                eid = str(uuid.uuid4())
                cur.execute("INSERT INTO kg_entities (id,session_id,name,entity_type,level,created_at) VALUES (%s,%s,%s,%s,'L1',NOW())",
                    (eid, row["session_id"], name, etype))
                emap[name] = eid
        
        for rel in data.get("relations", []):
            src = str(rel.get("source", ""))[:255]
            tgt = str(rel.get("target", ""))[:255]
            rtype = str(rel.get("type", "depends_on"))[:50]
            si, ti = emap.get(src), emap.get(tgt)
            if not si or not ti: continue
            rid = str(uuid.uuid4())
            cur.execute("INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,relation_type,created_at) VALUES (%s,%s,%s,%s,%s,NOW()) ON CONFLICT DO NOTHING",
                (rid, row["session_id"], si, ti, rtype))
        
        cur.execute("UPDATE messages SET metadata_json=metadata_json||'{\"kg_extract_pending\":\"false\",\"kg_extract_status\":\"done_local\"}' WHERE id=%s", (mid,))
        conn.commit()
        processed += 1
        if processed % 50 == 0:
            print(f"  [P{args.partition}] {processed} msgs done", flush=True)
    
    cur.close()
    conn.close()
    print(f"[P{args.partition}] DONE: {processed} msgs, {skip_count} skipped", flush=True)

if __name__ == "__main__":
    main()
