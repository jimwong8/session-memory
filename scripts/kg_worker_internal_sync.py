#!/usr/bin/env python3
"""KG worker with batch LLM calls - N messages in 1 API call"""
import argparse, json, os, re, time, redis
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

DB_URL=os.getenv("KG_DB_URL","postgresql://postgres:postgres@localhost:5432/session_memory")
BASE_URL=os.getenv("KG_LLM_URL")
API_KEY=os.getenv("KG_LLM_KEY")
MODEL=os.getenv("KG_LLM_MODEL")
PARTITION=os.getenv("KG_PARTITION","")
TOTAL=int(os.getenv("KG_PARTITIONS","1"))
ALLOWED={'depends_on','modifies','fixes','exemplifies','leads_to','part_of','prefers_over','contradicts','reinforces','evolved_into','invalidated_by','derived_from'}
client=OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=120)

R = redis.Redis(host="redis", port=6379, db=0, socket_timeout=3)
def _incr(metric, route, amount=1):
    try:
        key=f"llm:route:{metric}:kg:{route}"; R.incrby(key,amount); R.expire(key,86400)
        bucket=datetime.now(timezone.utc).strftime("%Y%m%d%H")
        wkey=f"llm:route:window:{bucket}:{metric}:kg:{route}"; R.incrby(wkey,amount); R.expire(wkey,7200)
    except: pass

def extract_batch(texts):
    """Extract KG from multiple texts in one API call"""
    items = "\n---\n".join(f"[{i}] {t[:1500]}" for i, t in enumerate(texts))
    prompt = f"Extract KG for each [N] item. Output JSON dict with keys 0,1,2... each containing {{entities:[{{name,type}}],relations:[{{source,target,type}}]}}. Items:\n{items}"
    for attempt in range(2):
        try:
            r=client.chat.completions.create(model=MODEL, messages=[{"role":"user","content":prompt}], temperature=0, max_tokens=500*len(texts))
            t=r.choices[0].message.content or ""
            t=re.sub(r'```(?:json)?\s*','',t).strip()
            m=re.search(r"\{[\s\S]*\}", t)
            if m:
                data=json.loads(m.group(0))
                results=[]
                for i in range(len(texts)):
                    results.append(data.get(str(i),{"entities":[],"relations":[]}))
                _incr("hit", MODEL, len(texts))
                return results
        except Exception as e:
            if "429" in str(e) and attempt==0: time.sleep(5); continue
    _incr("fail", MODEL, len(texts))
    return None

def upsert_entity(cur, sid, name, etype):
    cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s",(sid,name))
    row=cur.fetchone()
    if row: return row["id"]
    eid=os.urandom(16).hex()
    cur.execute("INSERT INTO kg_entities (id,session_id,name,entity_type,level,created_at) VALUES (%s,%s,%s,%s,'L1',NOW())",(eid,sid,name,etype))
    return eid

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--batch",type=int,default=5); args=ap.parse_args()
    conn=psycopg2.connect(DB_URL, cursor_factory=RealDictCursor); conn.autocommit=True
    with conn.cursor() as cur:
        if PARTITION!="":
            cur.execute("SELECT id,content,session_id,metadata_json FROM messages WHERE role IN ('user','assistant') AND metadata_json->>'kg_extract_pending'='true' AND length(content)>=50 AND abs(hashtext(id::text)) %% %s = %s ORDER BY created_at ASC LIMIT %s FOR UPDATE SKIP LOCKED",(TOTAL,PARTITION,args.batch))
        else:
            cur.execute("SELECT id,content,session_id,metadata_json FROM messages WHERE role IN ('user','assistant') AND metadata_json->>'kg_extract_pending'='true' AND length(content)>=50 ORDER BY created_at ASC LIMIT %s FOR UPDATE SKIP LOCKED",(args.batch,))
        msgs=cur.fetchall()
    if not msgs: print("pending=0",flush=True); return
    print(f"pending={len(msgs)}",flush=True)
    
    texts=[m["content"] or "" for m in msgs]
    results=extract_batch(texts)
    if not results:
        for m in msgs:
            meta=dict(m["metadata_json"] or {}); meta["kg_extract_status"]="retry_wait"
            conn.cursor().execute("UPDATE messages SET metadata_json=%s WHERE id=%s",(json.dumps(meta),m["id"]))
        return
    
    n_ent=n_rel=0
    for msg, data in zip(msgs, results):
        ents={}
        for e in data.get("entities",[]):
            if e.get("name"): ents[e["name"]]=upsert_entity(conn.cursor(),msg["session_id"],e["name"],e.get("type","concept"))
        for r in data.get("relations",[]):
            s=ents.get(r.get("source")); t=ents.get(r.get("target")); rt=r.get("type")
            if s and t and rt in ALLOWED:
                try:
                    conn.cursor().execute("UPDATE kg_relations SET invalid_at=NOW() WHERE session_id=%s AND source_entity_id=%s AND target_entity_id=%s AND relation_type<>%s AND invalid_at IS NULL",(msg["session_id"],s,t,rt))
                    rid=os.urandom(16).hex()
                    conn.cursor().execute("INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,relation_type,valid_at,created_at) VALUES (%s,%s,%s,%s,%s,NOW(),NOW())",(rid,msg["session_id"],s,t,rt))
                    n_rel+=1
                except: pass
        meta=dict(msg["metadata_json"] or {}); meta["kg_extract_pending"]=False
        meta["kg_extract_status"]="done" if ents else "empty"
        conn.cursor().execute("UPDATE messages SET metadata_json=%s WHERE id=%s",(json.dumps(meta),msg["id"]))
        n_ent+=len(ents)
    print(f"  entities={n_ent} relations={n_rel}",flush=True)

if __name__=="__main__":
    main()
