#!/usr/bin/env python3
"""KG worker - dots-studio/dots-3-note-prev-free (AtlasCloud, thinking disabled).

Reasoning model: must send extra_body={"enable_thinking": False} or it burns the
whole token budget on chain-of-thought and never emits JSON. With thinking off,
message.content holds clean entities/relations JSON, but relation types are free verbs
that must be normalized to the 12 allowed relation types before DB write.
"""
import json, os, re, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

DB_URL = "postgresql://postgres:****@postgres:5432/session_memory"
BASE_URL = os.getenv("DOTS_BASE_URL", "https://api.atlascloud.ai/v1")
API_KEY = os.getenv("DOTS_API_KEY", "apikey-03a16d10aec040208ac3597f0e529d8e")
MODEL = os.getenv("DOTS_MODEL", "dots-studio/dots-3-note-prev-free")

ALLOWED_RELATIONS = {'depends_on','modifies','fixes','exemplifies','prefers_over','contradicts',
                     'reinforces','evolved_into','invalidated_by','leads_to','derived_from','part_of'}
CONFIDENCE_THRESHOLD = 0.7
BATCH = int(os.getenv("KG_BATCH", "100"))
MAX_WORKERS = int(os.getenv("KG_WORKERS", "48"))

RELATION_VERB_MAP = {
    'depends_on':'depends_on','uses':'depends_on','used_by':'depends_on','requires':'depends_on',
    'needs':'depends_on','relies_on':'depends_on','deploy':'depends_on','deploys':'depends_on',
    'deployed_to':'depends_on','deploy_to':'depends_on','install':'depends_on','installed_on':'depends_on',
    'runs_on':'depends_on','hosts':'depends_on','configures':'depends_on','configured':'depends_on',
    'setup':'depends_on','builds':'depends_on','modifies':'modifies','edits':'modifies','changes':'modifies',
    'updates':'modifies','fixes':'fixes','resolves':'fixes','patches':'fixes','repairs':'fixes',
    'exemplifies':'exemplifies','demonstrates':'exemplifies','shows':'exemplifies','prefers_over':'prefers_over',
    'prefers':'prefers_over','chooses':'prefers_over','contradicts':'contradicts','conflicts':'contradicts',
    'opposes':'contradicts','reinforces':'reinforces','supports':'reinforces','validates':'reinforces',
    'evolved_into':'evolved_into','became':'evolved_into','migrated_to':'evolved_into',
    'invalidated_by':'invalidated_by','disproves':'invalidated_by','breaks':'invalidated_by',
    'leads_to':'leads_to','causes':'leads_to','triggers':'leads_to','results_in':'leads_to',
    'derived_from':'derived_from','comes_from':'derived_from','based_on':'derived_from','part_of':'part_of',
    'contains':'part_of','belongs_to':'part_of','member_of':'part_of','included_in':'part_of',
}

VALID_ENTITY_TYPES = {'file','function','concept','error','tool','person','product','technology'}


def _normalize_rel_type(verb):
    v = str(verb or "").strip().lower()
    if v in ALLOWED_RELATIONS:
        return v
    return RELATION_VERB_MAP.get(v, "depends_on")


def _safe_conf(v, default=0.9):
    try:
        return float(v)
    except Exception:
        return default


def _coerce(obj):
    if not isinstance(obj, dict):
        return {"entities": [], "relations": []}
    ents = obj.get("entities") or []
    rels = obj.get("relations") or []
    # also accept nodes/edges or subject/predicate/object shapes
    if (not ents) and (not rels):
        nodes = obj.get("nodes") or obj.get("vertices") or []
        edges = obj.get("edges") or obj.get("links") or []
        subs = obj.get("triples") or []
        if nodes or edges:
            ents, rels = [], []
            name_by_id = {}
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                nid = str(n.get("id") or n.get("label") or "").strip()
                label = str(n.get("label") or n.get("name") or nid).strip()
                if not label:
                    continue
                name_by_id[nid] = label
                et = str(n.get("type") or "concept").strip().lower()
                if et not in VALID_ENTITY_TYPES:
                    et = "concept"
                ents.append({"name": label, "type": et, "confidence": _safe_conf(n.get("confidence", 0.9))})
            for e in edges:
                if not isinstance(e, dict):
                    continue
                s = str(e.get("source") or e.get("from") or "").strip()
                t = str(e.get("target") or e.get("to") or "").strip()
                s = name_by_id.get(s, s); t = name_by_id.get(t, t)
                if not s or not t:
                    continue
                rels.append({"source": s, "target": t, "type": _normalize_rel_type(e.get("type") or e.get("label")), "confidence": _safe_conf(e.get("confidence", 0.9))})
        elif subs:
            ents, rels = [], []
            seen = set()
            for tri in subs:
                if not isinstance(tri, dict):
                    continue
                s = str(tri.get("subject") or tri.get("source") or "").strip()
                o = str(tri.get("object") or tri.get("target") or "").strip()
                pred = _normalize_rel_type(tri.get("predicate") or tri.get("relation") or tri.get("type"))
                for nm in (s, o):
                    if nm and nm not in seen:
                        seen.add(nm)
                        ents.append({"name": nm, "type": "concept", "confidence": 0.9})
                if s and o:
                    rels.append({"source": s, "target": o, "type": pred, "confidence": 0.9})
    if not isinstance(ents, list):
        ents = []
    if not isinstance(rels, list):
        rels = []
    clean_e, seen = [], set()
    for e in ents:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        if not name or name in seen:
            continue
        et = str(e.get("type") or "concept").strip().lower()
        if et not in VALID_ENTITY_TYPES:
            et = "concept"
        seen.add(name)
        clean_e.append({"name": name, "type": et, "confidence": _safe_conf(e.get("confidence", 0.9))})
    clean_r = []
    names = {e["name"] for e in clean_e}
    for r in rels:
        if not isinstance(r, dict):
            continue
        s = str(r.get("source") or r.get("subject") or "").strip()
        t = str(r.get("target") or r.get("object") or "").strip()
        if s not in names or t not in names:
            continue
        rt = _normalize_rel_type(r.get("type") or r.get("predicate"))
        if rt not in ALLOWED_RELATIONS:
            continue
        clean_r.append({"source": s, "target": t, "type": rt, "confidence": _safe_conf(r.get("confidence", 0.9))})
    return {"entities": clean_e, "relations": clean_r}


def extract(content):
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=120)
    prompt = (
        "Extract a knowledge graph as JSON from the text. Use exactly this structure and nothing else:\n"
        '{"entities":[{"name":STR,"type":STR,"confidence":FLOAT}],'
        '"relations":[{"source":STR,"target":STR,"type":STR,"confidence":FLOAT}]}\n'
        "entity type must be one of: file, function, concept, error, tool, person, product, technology\n"
        "relation type must be one of: depends_on, modifies, fixes, exemplifies, prefers_over, contradicts, "
        "reinforces, evolved_into, invalidated_by, leads_to, derived_from, part_of\n"
        "Entity names must literally appear in the text. Output JSON only, no markdown.\n"
        "Text: " + content[:2500]
    )
    for attempt in range(3):
        try:
            r = client.chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": prompt}],
                temperature=0, max_tokens=2000, extra_body={"enable_thinking": False})
            msg = r.choices[0].message
            t = (getattr(msg, "content", None) or getattr(msg, "reasoning_content", None) or "")
            cand_objs = []
            try:
                cand_objs.append(json.loads(t))
            except Exception:
                pass
            for strat in [lambda s: re.findall(r"\{[\s\S]*\}", s),
                          lambda s: re.findall(r"\{(?:[^{}]|[^{}]*)*\}", s)]:
                for cand in strat(t):
                    try:
                        cand_objs.append(json.loads(cand))
                    except Exception:
                        continue
            for obj in cand_objs:
                norm = _coerce(obj)
                if norm["entities"] or norm["relations"]:
                    return norm
            return {"entities": [], "relations": []}
        except Exception as e:
            time.sleep(2 * (attempt + 1))
            continue
    return None


def verify_entity_in_text(name, text):
    if not name or not text:
        return False
    return name.lower() in text.lower()


def process_job(job):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        result = extract(job["content"])
        if result is None:
            cur.execute("UPDATE kg_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                        ("Dots extract failed", job["id"]))
            conn.commit()
            return False
        entity_ids = {}
        for e in result.get("entities", []):
            name = str(e.get("name", "")).strip()
            if not name:
                continue
            etype = str(e.get("type", "concept")).strip()
            try:
                conf = float(e.get("confidence", 1.0))
            except Exception:
                conf = 1.0
            if conf < CONFIDENCE_THRESHOLD:
                continue
            if not verify_entity_in_text(name, job["content"]):
                continue
            cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s", (job["session_id"], name))
            row = cur.fetchone()
            if row:
                entity_ids[name] = row["id"]
            else:
                eid = os.urandom(16).hex()
                cur.execute("INSERT INTO kg_entities (id,session_id,name,entity_type,level,created_at) VALUES (%s,%s,%s,%s,'L1',NOW())",
                            (eid, job["session_id"], name, etype))
                entity_ids[name] = eid
                try:
                    tid = os.urandom(16).hex()
                    cur.execute("INSERT INTO kg_extraction_sources (id,entity_id,message_id,source_text,confidence,extraction_model,created_at) VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                                (tid, eid, job["message_id"], job["content"][:500], conf, MODEL))
                except Exception:
                    pass
        for r in result.get("relations", []):
            src = str(r.get("source", "")).strip()
            tgt = str(r.get("target", "")).strip()
            rtype = str(r.get("type", "")).strip().lower()
            if rtype not in ALLOWED_RELATIONS:
                continue
            try:
                conf = float(r.get("confidence", 1.0))
            except Exception:
                conf = 1.0
            if conf < CONFIDENCE_THRESHOLD:
                continue
            si, ti = entity_ids.get(src), entity_ids.get(tgt)
            if not si or not ti:
                continue
            if not (verify_entity_in_text(src, job["content"]) and verify_entity_in_text(tgt, job["content"])):
                continue
            rid = os.urandom(16).hex()
            cur.execute("INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,relation_type,valid_at,created_at) VALUES (%s,%s,%s,%s,%s,NOW(),NOW()) ON CONFLICT DO NOTHING",
                        (rid, job["session_id"], si, ti, rtype))
            try:
                tid = os.urandom(16).hex()
                cur.execute("INSERT INTO kg_extraction_sources (id,relation_id,message_id,source_text,confidence,extraction_model,created_at) VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                            (tid, rid, job["message_id"], job["content"][:500], conf, MODEL))
            except Exception:
                pass
        cur.execute("UPDATE kg_jobs SET status='completed', model_used=%s, updated_at=now() WHERE id=%s",
                    (MODEL, job["id"]))
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
            cur.execute("UPDATE kg_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                        (f"{type(e).__name__}: {str(e)[:150]}", job["id"]))
            conn.commit()
        except Exception:
            pass
        return False
    finally:
        cur.close()
        conn.close()


def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT count(*) FROM kg_jobs WHERE status='pending'")
    pending = cur.fetchone()["count"]
    if pending == 0:
        conn.close()
        return
    cur.execute(
        "SELECT j.id, j.message_id, j.session_id, m.content "
        "FROM kg_jobs j JOIN messages m ON j.message_id = m.id "
        "WHERE j.status='pending' ORDER BY j.created_at LIMIT %s FOR UPDATE SKIP LOCKED",
        (BATCH,),
    )
    jobs = cur.fetchall()
    if not jobs:
        conn.close()
        return
    for job in jobs:
        cur.execute("UPDATE kg_jobs SET status='running', locked_at=now() WHERE id=%s", (job["id"],))
    conn.commit()
    conn.close()
    print(f"pending={pending} batch={len(jobs)} model={MODEL}", flush=True)
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(process_job, j): j for j in jobs}
        for f in as_completed(futs):
            if f.result():
                ok += 1
            else:
                fail += 1
    print(f"done: ok={ok} fail={fail}", flush=True)


if __name__ == "__main__":
    main()
