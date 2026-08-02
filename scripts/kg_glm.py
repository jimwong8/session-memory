#!/usr/bin/env python3
"""GLM-4.5-Air KG worker with sufficient max_tokens"""
import argparse, json, os, re, time, uuid, requests, psycopg2
from psycopg2.extras import RealDictCursor

DB = "host=postgres dbname=session_memory user=postgres password=postgres"
SF_URL = "https://api.siliconflow.cn/v1"
SF_KEY = "sk-jvddfulkyqfzfozykrkcdwzyirscwsocomcgjmvrdejdzqui"

def extract(text, model, key, url):
    prompt = f"Extract KG JSON only. Format: {{\"entities\":[{{\"name\":\"x\",\"type\":\"concept\"}}],\"relations\":[{{\"source\":\"a\",\"target\":\"b\",\"type\":\"depends_on\"}}]}}. Text: {text[:2000]}"
    try:
        r = requests.post(f"{url}/chat/completions", json={
            "model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1000, "temperature": 0
        }, headers={"Authorization": f"Bearer {key}"}, timeout=30)
        if r.status_code == 200:
            raw = r.json()["choices"][0]["message"]["content"]
            raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(raw[start:end+1])
                except json.JSONDecodeError:
                    pass
            return {"entities": [], "relations": []}
        elif r.status_code == 429:
            time.sleep(3)
    except Exception as e:
        print(f"  err: {str(e)[:30]}", flush=True)
    return None

def process_partition(partition, total, model, key, url):
    conn = psycopg2.connect(DB, cursor_factory=RealDictCursor)
    conn.autocommit = False
    cur = conn.cursor()
    processed = 0
    
    while True:
        cur.execute("""SELECT id, content, session_id FROM messages 
            WHERE metadata_json->>'kg_extract_pending'='true' AND length(content)>=50
            AND abs(hashtext(id::text)) %% %s = %s
            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED""",
            (total, partition))
        row = cur.fetchone()
        if not row:
            break
        
        data = extract(row["content"] or "", model, key, url)
        if data is None:
            conn.rollback()
            continue
        
        emap = {}
        for ent in data.get("entities", []):
            name = str(ent.get("name", ""))[:255]
            if not name:
                continue
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
            if not si or not ti:
                continue
            rid = str(uuid.uuid4())
            cur.execute("INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,relation_type,created_at) VALUES (%s,%s,%s,%s,%s,NOW()) ON CONFLICT DO NOTHING",
                (rid, row["session_id"], si, ti, rtype))
        
        cur.execute("UPDATE messages SET metadata_json=metadata_json||'{\"kg_extract_pending\":\"false\",\"kg_extract_status\":\"done_local\"}' WHERE id=%s", (row["id"],))
        conn.commit()
        processed += 1
        if processed % 10 == 0:
            print(f"  [P{partition}] {processed} msgs", flush=True)
    
    cur.close()
    conn.close()
    print(f"  [P{partition}] DONE: {processed} msgs", flush=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--partition", type=int, required=True)
    parser.add_argument("--total", type=int, default=8)
    args = parser.parse_args()
    
    model, key, url = "zai-org/GLM-4.5-Air", SF_KEY, SF_URL
    
    print(f"[P{args.partition}] model={model}", flush=True)
    process_partition(args.partition, args.total, model, key, url)

if __name__ == "__main__":
    main()
