#!/usr/bin/env python3
"""KG GPU Worker v3 - Qwen 2.5 3B via llama-server, on LXC1001"""
import time, json, requests, psycopg2, sys, uuid, re
from psycopg2.extras import RealDictCursor

import argparse
p = argparse.ArgumentParser()
p.add_argument('--partition', type=int, default=0)
p.add_argument('--total', type=int, default=1)
args = p.parse_args()
PARTITION = args.partition
TOTAL = args.total

DB = "host=10.100.1.13 port=5432 dbname=session_memory user=postgres"
LLM_URL = "http://localhost:18881/v1"
MODEL = "qwen2.5-3b-instruct-q4_k_m"

KG_PROMPT = """Extract knowledge graph from this conversation. Output ONLY valid JSON, no markdown.

Conversation:
{content}

Output EXACTLY:
{{"entities":[{{"name":"entity name","type":"person|place|thing|concept|event","description":"..."}}],"relations":[{{"from":"entity name","to":"entity name","type":"relation","description":"..."}}]}}"""

def get_conn():
    return psycopg2.connect(DB, cursor_factory=RealDictCursor)

def fix_json(text):
    """Fix common JSON syntax errors from LLMs"""
    # Remove trailing commas
    text = re.sub(r',(\s*[}\]])', r'\1', text)
    # Fix double commas
    text = re.sub(r',\s*,', ',', text)
    return text

def extract_first_json(s):
    # Try direct parse
    try:
        return json.loads(s)
    except:
        pass
    
    # Try fixing common errors
    try:
        return json.loads(fix_json(s))
    except:
        pass
    
    # Find first { or [ and try to extract
    for i, c in enumerate(s):
        if c in ('{', '['):
            depth = 0
            for j in range(i, len(s)):
                if s[j] in ('{', '['):
                    depth += 1
                elif s[j] in ('}', ']'):
                    depth -= 1
                    if depth == 0:
                        chunk = s[i:j+1]
                        try:
                            return json.loads(chunk)
                        except:
                            try:
                                return json.loads(fix_json(chunk))
                            except:
                                break
    
    # Fallback: extract entities/relations via regex
    entities = []
    relations = []
    
    # Find all name/type pairs
    name_type = re.findall(r'"name"\s*:\s*"([^"]+)"[^}]*"type"\s*:\s*"([^"]+)"', s)
    for name, etype in name_type:
        entities.append({"name": name, "type": etype, "description": ""})
    
    # Find from/to/type triples for relations
    from_to = re.findall(r'"from"\s*:\s*"([^"]+)"[^}]*"to"\s*:\s*"([^"]+)"', s)
    for f, t in from_to:
        relations.append({"from": f, "to": t, "type": "related", "description": ""})
    
    if entities or relations:
        return {"entities": entities, "relations": relations}
    
    return None

def normalize(data):
    entities = []
    relations = []
    
    if isinstance(data, dict):
        if "entities" in data:
            for e in data["entities"]:
                if isinstance(e, dict):
                    if "name" in e:
                        entities.append(e)
                    else:
                        for k, v in e.items():
                            if isinstance(v, str) and len(v) > 1:
                                entities.append({"name": v, "type": k, "description": ""})
                                break
        if "relations" in data:
            for r in data["relations"]:
                if isinstance(r, dict):
                    if "from" in r:
                        relations.append(r)
                    elif "entity" in r and "object" in r:
                        relations.append({"from": r["entity"], "to": r["object"], "type": r.get("relation", "related"), "description": r.get("description", "")})
        
        if "entity" in data and "object" in data:
            entities.append({"name": str(data["entity"]), "type": "concept", "description": ""})
            entities.append({"name": str(data["object"]), "type": "concept", "description": ""})
            relations.append({"from": str(data["entity"]), "to": str(data["object"]), 
                            "type": str(data.get("relation", "related")), "description": ""})
        
        for v in data.values():
            if isinstance(v, (dict, list)):
                e2, r2 = normalize(v)
                entities.extend(e2)
                relations.extend(r2)
    
    elif isinstance(data, list):
        for item in data:
            e2, r2 = normalize(item)
            entities.extend(e2)
            relations.extend(r2)
    
    return entities, relations

def process_row(cur, row):
    mid = str(row['id'])
    content = (row['content'] or '')[:2000]
    session_id = str(row.get('session_id', uuid.uuid4()))
    
    prompt = KG_PROMPT.format(content=content)
    
    resp = requests.post(f"{LLM_URL}/chat/completions", json={
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 800,
        "temperature": 0.3
    }, timeout=120)
    
    if resp.status_code != 200:
        return False, f"llm {resp.status_code}"
    
    text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    
    text = text.strip()
    if text.startswith("```"):
        text = text[3:]
        if text.startswith("json"):
            text = text[4:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    
    data = extract_first_json(text)
    if data is None:
        return False, "parse fail"
    
    entities, relations = normalize(data)
    
    seen = set()
    uniq = []
    for e in entities:
        k = json.dumps(e, sort_keys=True, ensure_ascii=False)
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    entities = uniq
    
    entity_ids = {}
    for ent in entities:
        name = str(ent.get("name", ""))[:255]
        etype = str(ent.get("type", "concept"))[:100]
        desc = str(ent.get("description", ""))[:500]
        
        if not name:
            continue
        
        try:
            cur.execute("""
                INSERT INTO kg_entities (session_id, name, entity_type, level)
                VALUES (%s, %s, %s, 'atom')
                ON CONFLICT (session_id, name) DO UPDATE SET
                    entity_type = EXCLUDED.entity_type,
                    updated_at = now()
                RETURNING id
            """, (session_id, name, etype))
            result = cur.fetchone()
            if result:
                entity_ids[name] = str(result['id'])
        except Exception as e:
            print(f"  ent err: {e}")
            raise
    
    for rel in relations:
        from_name = str(rel.get("from", ""))[:255]
        to_name = str(rel.get("to", ""))[:255]
        rtype = str(rel.get("type", "related"))[:100]
        desc = str(rel.get("description", ""))[:500]
        
        from_id = entity_ids.get(from_name)
        to_id = entity_ids.get(to_name)
        
        if not from_id or not to_id:
            continue
        
        try:
            cur.execute("""
                INSERT INTO kg_relations (session_id, source_entity_id, target_entity_id, relation_type, message_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (session_id, from_id, to_id, rtype, mid))
        except Exception as e:
            print(f"  rel err: {e}")
            raise
    
    cur.execute("""
        UPDATE messages SET metadata_json = metadata_json || '{"kg_extract_pending":false,"kg_extract_status":"done_local"}'::jsonb
        WHERE id = %s
    """, (mid,))
    
    return True, f"e={len(entities)} r={len(relations)}"

def main():
    conn = get_conn()
    cur = conn.cursor()
    processed = 0
    errors = 0
    last_report = time.time()
    
    print(f"[P{PARTITION}] GPU worker started")
    
    while True:
        try:
            t0 = time.time()
            cur.execute("""
                SELECT id, content, session_id FROM messages
                WHERE metadata_json->>'kg_extract_pending'='true' AND length(content)>=50
                ORDER BY id LIMIT 50
            """)
            rows = cur.fetchall()
            fetch_ms = (time.time()-t0)*1000
            
            if not rows:
                time.sleep(5)
                continue
            
            for row in rows:
                try:
                    ok, info = process_row(cur, row)
                    if ok:
                        processed += 1
                    else:
                        errors += 1
                        print(f"  fail: {info}")
                except Exception as e:
                    errors += 1
                    print(f"  err: {e}")
                    conn.rollback()
            
            conn.commit()
            
            if time.time() - last_report > 30:
                print(f"[P{PARTITION}] processed={processed} errors={errors} fetch={fetch_ms:.0f}ms")
                last_report = time.time()
                
        except Exception as e:
            print(f"[P{PARTITION}] loop error: {e}")
            conn.rollback()
            time.sleep(2)
            try:
                conn.close()
            except:
                pass
            conn = get_conn()
            cur = conn.cursor()

if __name__ == "__main__":
    main()
