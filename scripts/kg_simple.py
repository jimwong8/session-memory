#!/usr/bin/env python3
"""Simple KG worker - no locks, no batching"""
import time, json, re, uuid, requests, psycopg2
from psycopg2.extras import RealDictCursor

DB = "host=postgres dbname=session_memory user=postgres password=postgres"
SF_URL = "https://api.siliconflow.cn/v1"
SF_KEY = "sk-jvddfulkyqfzfozykrkcdwzyirscwsocomcgjmvrdejdzqui"

def extract(text):
    prompt = f"Extract KG JSON: entities:[name,type],relations:[source,target,type]. Text: {text[:1500]}"
    try:
        r = requests.post(f"{SF_URL}/chat/completions", json={
            "model": "zai-org/GLM-4.5-Air",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300, "temperature": 0
        }, headers={"Authorization": f"Bearer {SF_KEY}"}, timeout=20)
        if r.status_code == 200:
            raw = r.json()["choices"][0]["message"]["content"]
            raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end > start:
                return json.loads(raw[start:end+1])
    except:
        pass
    return None

conn = psycopg2.connect(DB, cursor_factory=RealDictCursor)
conn.autocommit = False
cur = conn.cursor()
processed = 0

while True:
    cur.execute("""SELECT id, content, session_id FROM messages 
        WHERE metadata_json->>'kg_extract_pending'='true' AND length(content)>=50
        ORDER BY created_at LIMIT 1""")
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
        si, ti = emap.get(src), emap.get(tgt)
        if not si or not ti: continue
        rid = str(uuid.uuid4())
        cur.execute("INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,relation_type,created_at) VALUES (%s,%s,%s,%s,%s,NOW()) ON CONFLICT DO NOTHING",
            (rid, row["session_id"], si, ti, str(rel.get("type","depends_on"))[:50]))
    
    cur.execute("UPDATE messages SET metadata_json=metadata_json||'{\"kg_extract_pending\":\"false\",\"kg_extract_status\":\"done_local\"}' WHERE id=%s", (row["id"],))
    conn.commit()
    processed += 1
    if processed % 10 == 0:
        print(f"  {processed} msgs", flush=True)

conn.close()
print(f"DONE: {processed}")
