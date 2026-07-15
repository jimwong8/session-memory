#!/usr/bin/env python3
import json, os, re, time, uuid, sys
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

DB_URL = "postgresql://postgres:postgres@postgres:5432/session_memory"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "sk-cKS0fCy7CjqQV2R7sZ8dE7bG8d3d5566AaB37c055f9b8d3d")
KG_MODEL = "DeepSeek-V3.2-EXP"

client = OpenAI(api_key=OPENAI_API_KEY, base_url=os.environ.get("OPENAI_BASE_URL", "https://api.edgefn.net/v1"), timeout=90)

ALLOWED = {'depends_on','modifies','fixes','exemplifies','prefers_over','contradicts','reinforces','evolved_into','invalidated_by','leads_to','derived_from','part_of'}

def extract_kg(content):
    prompt = (
        "Extract entities and relations from the conversation. Output ONLY JSON.\n\n"
        "Conversation:\n" + content[:2500] + "\n\n"
        "Format: {\"entities\": [{\"name\": \"entity\", \"type\": \"person|tool|concept\"}], "
        "\"relations\": [{\"source\": \"e1\", \"target\": \"e2\", \"type\": \"depends_on|modifies|fixes|exemplifies|prefers_over|contradicts|reinforces|evolved_into|invalidated_by|leads_to|derived_from|part_of\"}]}"
    )
    try:
        resp = client.chat.completions.create(model=KG_MODEL, messages=[{"role":"user","content":prompt}], temperature=0, max_tokens=1500)
        text = resp.choices[0].message.content or ""
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        print(f"LLM err: {e}", flush=True)
        if '429' in str(e):
            print("Rate limited, waiting 60s...", flush=True)
            time.sleep(15)
    return None

def main():
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    total = success = failed = batch = 0

    while True:
        batch += 1
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, content, session_id, metadata_json FROM messages
                WHERE role IN ('user', 'assistant')
                AND metadata_json->>'kg_extract_pending' = 'true'
                AND length(content) >= 50
                ORDER BY created_at ASC
                LIMIT 3 FOR UPDATE SKIP LOCKED
            """)
            msgs = cur.fetchall()
        
        if not msgs:
            print("Done!", flush=True)
            break
        
        for msg in msgs:
            print(f"[{msg['id'][:8]}] ", end="", flush=True)
            kg_data = extract_kg(msg['content'])
            metadata = dict(msg['metadata_json'] or {})
            attempts = int(metadata.get('kg_extract_attempts', 0)) + 1
            
            if kg_data:
                try:
                    with conn.cursor() as cur:
                        n2id = {}
                        for ent in kg_data.get('entities', []):
                            n = ent.get('name', '').strip()
                            if not n: continue
                            t = (ent.get('type') or 'concept').strip().lower()
                            cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s LIMIT 1", (msg['session_id'], n))
                            r = cur.fetchone()
                            if r: n2id[n] = r[0]
                            else:
                                eid = str(uuid.uuid4())
                                cur.execute("INSERT INTO kg_entities (id, session_id, name, entity_type, created_at, level) VALUES (%s,%s,%s,%s,NOW(),'L1')", (eid, msg['session_id'], n, t))
                                n2id[n] = eid
                        
                        for rel in kg_data.get('relations', []):
                            s = rel.get('source','').strip()
                            t = rel.get('target','').strip()
                            r = (rel.get('type') or '').strip().lower()
                            if not s or not t or r not in ALLOWED: continue
                            sid, tid = n2id.get(s), n2id.get(t)
                            if not sid or not tid: continue
                            cur.execute("INSERT INTO kg_relations (id, session_id, source_entity_id, target_entity_id, relation_type, created_at) VALUES (%s,%s,%s,%s,%s,NOW())", (str(uuid.uuid4()), msg['session_id'], sid, tid, r))
                        
                        metadata['kg_extract_pending'] = 'false'
                        metadata['kg_extract_status'] = 'done'
                        success += 1
                        print("OK", flush=True)
                except Exception as e:
                    metadata['kg_extract_pending'] = 'true'
                    metadata['kg_extract_status'] = 'retry_wait'
                    metadata['kg_extract_error'] = f"save: {e}"[:100]
                    failed += 1
                    print(f"ERR: {e}", flush=True)
            else:
                metadata['kg_extract_pending'] = 'true'
                metadata['kg_extract_status'] = 'retry_wait'
                metadata['kg_extract_error'] = 'no_kg'
                failed += 1
                print("FAIL", flush=True)
            
            total += 1
            with conn.cursor() as cur:
                cur.execute("UPDATE messages SET metadata_json=%s WHERE id=%s", (json.dumps(metadata), msg['id']))
        
        print(f"Batch {batch}: {len(msgs)} processed | Total: {total}, success: {success}\n", flush=True)
        time.sleep(15)

    conn.close()
    print(f"Final: {total} processed, {success} kg saved, {failed} failed")

if __name__ == '__main__':
    main()
