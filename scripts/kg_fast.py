#!/usr/bin/env python3
"""Fast KG worker - stable single-msg extraction with high parallelism"""
import argparse, json, os, re, time, uuid, requests, psycopg2
from psycopg2.extras import RealDictCursor

DB = "host=postgres dbname=session_memory user=postgres password=postgres"
EF_URL = "https://api.edgefn.net/v1"
SF_URL = "https://api.siliconflow.cn/v1"

EF_KEYS = [
    "sk-syEFrinxzFq78jolC7Fa7eEa605b4dC8B9F4F717FdBa28Bf",
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
    "sk-9oep26R1GInewtcn449243Ba4dD14aB2B79f2032Ed3e1a93",
    "sk-8Cs2nQ9f2hCLHQoR2307Ef082a4944Ab8496B06bAaA38f4e",
    "sk-gRDyHSWBEtogghNRE13b2fD4D8F44f45A1599676EdF0Dd99",
]
SF_KEY = "sk-jvddfulkyqfzfozykrkcdwzyirscwsocomcgjmvrdejdzqui"

def extract(text, model, key, url):
    prompt = f"Extract KG JSON: entities:[{{name,type}}],relations:[{{source,target,type}}]. Types: file,function,concept,error,tool,person,product,technology. Relations: depends_on,modifies,fixes,exemplifies,leads_to,part_of. Content: {text[:2000]}"
    try:
        r = requests.post(f"{url}/chat/completions", json={
            "model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200, "temperature": 0
        }, headers={"Authorization": f"Bearer {key}"}, timeout=30)
        if r.status_code == 200:
            raw = r.json()["choices"][0]["message"]["content"]
            raw = re.sub(r'```(?:json)?\s*', '', raw).strip()
            m = re.search(r'\{[\s\S]*\}', raw)
            return json.loads(m.group(0)) if m else {"entities": [], "relations": []}
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
    parser.add_argument("--total", type=int, default=16)
    args = parser.parse_args()
    
    # All partitions use siliconflow (no RPM limit, fastest)
    model, key, url = "zai-org/GLM-4.5-Air", SF_KEY, SF_URL
    
    print(f"[P{args.partition}] model={model}", flush=True)
    process_partition(args.partition, args.total, model, key, url)

if __name__ == "__main__":
    main()
