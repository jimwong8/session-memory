#!/usr/bin/env python3
import argparse, json, os, re
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
DB_URL=os.getenv("KG_DB_URL","postgresql://postgres:postgres@localhost:5432/session_memory")
BASE_URL=os.getenv("KG_LLM_URL","http://10.100.1.101:18881/v1")
API_KEY=os.getenv("KG_LLM_KEY","not-needed")
MODEL=os.getenv("KG_LLM_MODEL","/models/Gemma-4-E2B-Q4_K_M.gguf")
ALLOWED={'depends_on','modifies','fixes','exemplifies','prefers_over','contradicts','reinforces','evolved_into','invalidated_by','leads_to','derived_from','part_of'}
client=OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=240)
def extract(content):
    prompt=("你是一个知识图谱提取引擎。只输出JSON，禁止markdown。\n允许实体类型: file, function, concept, error, tool, person, product, technology\n允许关系: depends_on, modifies, fixes, exemplifies, prefers_over, contradicts, reinforces, evolved_into, invalidated_by, leads_to, derived_from, part_of\n格式: {\"entities\":[{\"name\":\"x\",\"type\":\"concept\"}],\"relations\":[{\"source\":\"a\",\"target\":\"b\",\"type\":\"depends_on\"}]}\n内容:\n"+content[:2500])
    try:
        r=client.chat.completions.create(model=MODEL, messages=[{"role":"user","content":prompt}], temperature=0, max_tokens=1500)
        t=r.choices[0].message.content or ""
        m=re.search(r"\{[\s\S]*\}", t)
        return json.loads(m.group(0)) if m else {"entities":[],"relations":[]}
    except Exception as e:
        print("LLM err: "+str(e), flush=True); return None
def upsert_entity(cur, sid, name, etype):
    cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s",(sid,name))
    row=cur.fetchone()
    if row: return row["id"]
    eid=os.urandom(16).hex()
    cur.execute("INSERT INTO kg_entities (id, session_id, name, entity_type, level, created_at) VALUES (%s,%s,%s,%s,'L1',NOW())",(eid,sid,name,etype))
    return eid
def heal_and_insert(cur, sid, src, tgt, rtype):
    cur.execute("UPDATE kg_relations SET invalid_at=NOW(), version=COALESCE(version,1)+1 WHERE session_id=%s AND source_entity_id=%s AND target_entity_id=%s AND relation_type<>%s AND invalid_at IS NULL",(sid,src,tgt,rtype))
    rid=os.urandom(16).hex()
    cur.execute("INSERT INTO kg_relations (id, session_id, source_entity_id, target_entity_id, relation_type, valid_at, created_at) VALUES (%s,%s,%s,%s,%s,NOW(),NOW())",(rid,sid,src,tgt,rtype))
    return rid
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--batch",type=int,default=5); args=ap.parse_args()
    conn=psycopg2.connect(DB_URL, cursor_factory=RealDictCursor); conn.autocommit=True
    with conn.cursor() as cur:
        cur.execute("SELECT id, content, session_id, metadata_json FROM messages WHERE role IN ('user','assistant') AND metadata_json->>'kg_extract_pending'='true' AND (metadata_json->>'kg_extract_next_attempt_at' IS NULL OR metadata_json->>'kg_extract_next_attempt_at'<=%s) AND length(content)>=50 ORDER BY created_at ASC LIMIT %s FOR UPDATE SKIP LOCKED",(datetime.now(timezone.utc).isoformat(),args.batch))
        msgs=cur.fetchall()
    print("pending="+str(len(msgs)), flush=True)
    if not msgs: return
    for msg in msgs:
        meta=dict(msg["metadata_json"] or {})
        data=extract(msg["content"])
        if not data:
            meta["kg_extract_status"]="retry_wait"; meta["kg_extract_attempts"]=int(meta.get("kg_extract_attempts") or 0)+1
            conn.cursor().execute("UPDATE messages SET metadata_json=%s WHERE id=%s",(json.dumps(meta),msg["id"])); continue
        ents={}
        for e in data.get("entities",[]):
            if e.get("name"): ents[e["name"]]=upsert_entity(conn.cursor(), msg["session_id"], e["name"], e.get("type","concept"))
        n=0
        for r in data.get("relations",[]):
            s=ents.get(r.get("source")); t=ents.get(r.get("target")); rt=r.get("type")
            if s and t and rt in ALLOWED:
                heal_and_insert(conn.cursor(), msg["session_id"], s, t, rt); n+=1
        meta["kg_extract_pending"]=False; meta["kg_extract_status"]="done" if n>0 else "empty"; meta["kg_extract_error"]=None
        conn.cursor().execute("UPDATE messages SET metadata_json=%s WHERE id=%s",(json.dumps(meta),msg["id"]))
        print("  msg "+str(msg['id'])[:8]+" entities="+str(len(ents))+" relations="+str(n), flush=True)
if __name__=="__main__":
    main()
