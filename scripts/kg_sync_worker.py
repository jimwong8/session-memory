#!/usr/bin/env python3
"""Simple synchronous KG backfill worker"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

# Config
DB_URL = "postgresql://postgres:postgres@postgres:5432/session_memory"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-cKS0fCy7CjqQV2R7sZ8dE7bG8d3d5566AaB37c055f9b8d3d")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.edgefn.net/v1")
KG_MODEL = "DeepSeek-V3.2-EXP"  # V3 doesn't have rate limit

ALLOWED_RELATIONS = {
    'depends_on', 'modifies', 'fixes', 'exemplifies', 'prefers_over',
    'contradicts', 'reinforces', 'evolved_into', 'invalidated_by',
    'leads_to', 'derived_from', 'part_of'
}

client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)

def call_kg_extraction(content, session_id=None):
    """Extract KG from content using V3"""
    prompt = f"""你是一个知识图谱提取器。从下面的对话中提取实体和关系。

对话内容：
{content[:3000]}

请以JSON格式输出：
```json
{{
  "entities": [
    {{"name": "实体名称", "type": "person|concept|tool|location|event|product|technology"}},
  ],
  "relations": [
    {{"source": "源实体", "target": "目标实体", "type": "depends_on|modifies|fixes|exemplifies|prefers_over|contradicts|reinforces|evolved_into|invalidated_by|leads_to|derived_from|part_of"}}
  ]
}}
```

只输出JSON，不要解释。"""

    try:
        resp = client.chat.completions.create(
            model=KG_MODEL,
            messages=[
                {"role": "system", "content": "你只能输出一个JSON对象，不能输出解释、markdown、或额外文本。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0,
            max_tokens=2000
        )
        
        text = resp.choices[0].message.content
        if text is None:
            return None
        
        # Try to extract JSON from the response
        import re
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        else:
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                text = json_match.group(0)
        
        return json.loads(text)
    except Exception as e:
        print(f"LLM call failed: {e}")
        return None


def save_entities_and_relations(db_cursor, session_id, kg_data):
    """Save extracted KG to database"""
    from src.models import KGEntity, KGRelation
    
    entities_saved = []
    
    # Save entities
    for ent in kg_data.get('entities', []):
        name = ent.get('name', '').strip()
        ent_type = ent.get('type', 'concept').strip().lower()
        if not name:
            continue
        
        # Check if entity exists for this session
        db_cursor.execute(
            "SELECT id FROM kg_entities WHERE session_id = %s AND name = %s LIMIT 1",
            (session_id, name)
        )
        existing = db_cursor.fetchone()
        
        if existing:
            entities_saved.append((existing[0], name))
        else:
            import uuid
            entity_id = str(uuid.uuid4())
            db_cursor.execute(
                "INSERT INTO kg_entities (id, session_id, name, entity_type, source, confidence, created_at) VALUES (%s, %s, %s, %s, 'backfill', 0.8, NOW())",
                (entity_id, session_id, name, ent_type)
            )
            entities_saved.append((entity_id, name))
    
    # Build name to id mapping
    name_to_id = {name: eid for eid, name in entities_saved}
    
    # Save relations
    for rel in kg_data.get('relations', []):
        source_name = rel.get('source', '').strip()
        target_name = rel.get('target', '').strip()
        rel_type = rel.get('type', '').strip().lower()
        
        if not source_name or not target_name or not rel_type:
            continue
        if rel_type not in ALLOWED_RELATIONS:
            continue
        
        source_id = name_to_id.get(source_name)
        target_id = name_to_id.get(target_name)
        
        if not source_id or not target_id:
            continue
        
        import uuid
        rel_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO kg_relations (id, session_id, source_entity_id, target_entity_id, relation_type, confidence, source, created_at) VALUES (%s, %s, %s, %s, %s, 0.7, 'backfill', NOW())",
            (rel_id, session_id, source_id, target_id, rel_type)
        )
    
    return len(entities_saved)


def main():
    batch_size = 10
    sleep_seconds = 2
    
    print(f"Starting KG sync worker with batch_size={batch_size}")
    
    total_processed = 0
    total_success = 0
    total_failed = 0
    
    while True:
        # Fetch pending
        conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
        now_iso = datetime.now(timezone.utc).isoformat()
        
        with conn.cursor() as cur:
            cur.execute("""
                SELECT m.id, m.content, m.session_id, m.role, m.metadata_json, m.created_at
                FROM messages m
                WHERE m.role IN ('user', 'assistant')
                  AND m.metadata_json->>'kg_extract_pending' = 'true'
                  AND (
                      m.metadata_json->>'kg_extract_next_attempt_at' IS NULL
                      OR m.metadata_json->>'kg_extract_next_attempt_at' <= %s
                  )
                  AND length(m.content) >= 50
                ORDER BY m.created_at ASC
                LIMIT %s
                FOR UPDATE OF m SKIP LOCKED
            """, (now_iso, batch_size))
            messages = cur.fetchall()
        
        if not messages:
            print(f"No more pending. Total: {total_processed} success={total_success} failed={total_failed}")
            conn.close()
            break
        
        for msg in messages:
            metadata = dict(msg['metadata_json'] or {})
            attempts = int(metadata.get('kg_extract_attempts', 0)) + 1
            
            # Extract KG
            kg_data = call_kg_extraction(msg['content'], msg['session_id'])
            
            metadata['kg_extract_attempts'] = attempts
            metadata['kg_extract_last_attempt_at'] = datetime.now(timezone.utc).isoformat()
            
            if kg_data:
                # Save to DB
                try:
                    save_entities_and_relations(conn.cursor(), msg['session_id'], kg_data)
                    metadata['kg_extract_pending'] = False
                    metadata['kg_extract_status'] = 'done'
                    metadata['kg_extract_error'] = None
                    metadata['kg_extract_next_attempt_at'] = None
                    total_success += 1
                    print(f"success={msg['id']}")
                except Exception as e:
                    total_failed += 1
                    if attempts >= 3:
                        metadata['kg_extract_pending'] = False
                        metadata['kg_extract_status'] = 'deadletter'
                        metadata['kg_extract_error'] = str(e)[:200]
                    else:
                        metadata['kg_extract_pending'] = True
                        metadata['kg_extract_status'] = 'retry_wait'
                        metadata['kg_extract_error'] = str(e)[:200]
                        metadata['kg_extract_next_attempt_at'] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                    print(f"error={msg['id']} {e}")
            else:
                total_failed += 1
                if attempts >= 3:
                    metadata['kg_extract_pending'] = False
                    metadata['kg_extract_status'] = 'deadletter'
                    metadata['kg_extract_error'] = 'max attempts'
                    metadata['kg_extract_next_attempt_at'] = None
                else:
                    metadata['kg_extract_pending'] = True
                    metadata['kg_extract_status'] = 'retry_wait'
                    metadata['kg_extract_error'] = 'extraction failed'
                    metadata['kg_extract_next_attempt_at'] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                print(f"failed={msg['id']}")
            
            # Save metadata back
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE messages SET metadata_json = %s WHERE id = %s",
                    (json.dumps(metadata), msg['id'])
                )
                conn.commit()
            
            total_processed += 1
        
        conn.close()
        time.sleep(sleep_seconds)
    
    print(f"Final: processed={total_processed} success={total_success} failed={total_failed}")


if __name__ == '__main__':
    main()