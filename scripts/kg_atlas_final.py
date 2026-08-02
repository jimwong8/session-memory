#!/usr/bin/env python3
"""AtlasCloud KG Worker - 13号机 host, uses Docker gateway IP for DB"""
import time, json, re, uuid, requests, psycopg2
from psycopg2.extras import RealDictCursor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DB = "host=100.77.184.40 port=5432 dbname=session_memory user=postgres"
AC_URL = "https://api.atlascloud.ai/v1"
AC_KEY = "apikey-03a16d10aec040208ac3597f0e529d8e"
MODEL = "deepseek-ai/DeepSeek-V3.2-Exp"

session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

def extract(text):
    prompt = "Extract KG JSON: entities:[name,type],relations:[source,target,type]. Types: person,product,technology,concept,file,function,error,tool. Relations: depends_on,modifies,fixes,uses,created_by,leads_to. Text: " + text[:1500]
    try:
        r = session.post(AC_URL + "/chat/completions", json={
            "model": MODEL, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1000, "temperature": 0
        }, headers={"Authorization": "Bearer " + AC_KEY}, timeout=120)
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
        print("  err: " + str(e)[:40], flush=True)
    return None

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--partition", type=int, default=0)
    parser.add_argument("--total", type=int, default=1)
    args = parser.parse_args()
    
    print("[P" + str(args.partition) + "] model=" + MODEL, flush=True)
    conn = psycopg2.connect(DB, cursor_factory=RealDictCursor)
    conn.autocommit = False
    cur = conn.cursor()
    processed = 0
    
    while True:
        cur.execute("SELECT id, content, session_id FROM messages WHERE metadata_json->>'kg_extract_pending'='true' AND length(content)>=50 AND abs(hashtext(id::text)) %% %s = %s ORDER BY created_at LIMIT 1",
            (args.total, args.partition))
        row = cur.fetchone()
        if not row:
            break
        
        data = extract(row["content"] or "")
        if not data:
            conn.rollback()
            continue
        
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
        
        cur.execute("UPDATE messages SET metadata_json=metadata_json||'{\"kg_extract_pending\":\"false\",\"kg_extract_status\":\"done_local\"}' WHERE id=%s", (row["id"],))
        conn.commit()
        processed += 1
        if processed % 10 == 0:
            print("  [P" + str(args.partition) + "] " + str(processed) + " msgs", flush=True)
    
    cur.close()
    conn.close()
    print("  [P" + str(args.partition) + "] DONE: " + str(processed) + " msgs", flush=True)

if __name__ == "__main__":
    main()
