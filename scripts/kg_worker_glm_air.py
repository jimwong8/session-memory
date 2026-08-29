#!/usr/bin/env python3
"""KG worker - local vLLM Qwen2.5-7B (kg_jobs table, thread-safe)."""
import json, os, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

DB_URL = "postgresql://postgres:***@postgres:5432/session_memory"
BASE_URL = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
API_KEY = os.getenv("ZHIPU_API_KEY", "")
MODEL = os.getenv("GLM_MODEL", "glm-4.5-air")

ALLOWED_RELATIONS = {'depends_on','modifies','fixes','exemplifies','prefers_over','contradicts',
                     'reinforces','evolved_into','invalidated_by','leads_to','derived_from','part_of'}
CONFIDENCE_THRESHOLD = 0.7
BATCH = int(os.getenv("KG_BATCH", "20"))
MAX_WORKERS = int(os.getenv("KG_WORKERS", "5"))

def _safe_error_text(e):
    s = f"{type(e).__name__}: {str(e)[:150]}"
    return s.replace("%", "pct")

LAST_ERROR_DETAIL = []

def _salvage_truncated_json(raw):
    """Recover entities/relations from a JSON object truncated mid-stream.

    The model emits a well-formed prefix, so we close dangling structures and
    drop the final incomplete element instead of discarding the whole result.
    """
    txt = raw.strip()
    for cut in range(len(txt), 0, -1):
        chunk = txt[:cut]
        # Trim to the last element boundary, then rebalance brackets.
        if chunk.rstrip().endswith((",", "{", "[", ":")):
            continue
        candidate = chunk
        opens = candidate.count("{") - candidate.count("}")
        bracks = candidate.count("[") - candidate.count("]")
        if opens < 0 or bracks < 0:
            continue
        candidate = candidate + ("]" * bracks) + ("}" * opens)
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            ents = data.get("entities")
            rels = data.get("relations")
            if isinstance(ents, list) or isinstance(rels, list):
                return {
                    "entities": ents if isinstance(ents, list) else [],
                    "relations": rels if isinstance(rels, list) else [],
                }
        return None
    return None



_ARTIFACT_RE = re.compile(
    r"\[SILENT\]|\[IMPORTANT:[^\]]*\]|DELIVERY:[^\n]*|\[/?OUT-OF-BAND[^\]]*\]",
    re.IGNORECASE,
)


def _strip_artifacts(text):
    """Remove Hermes control markers the model would otherwise copy verbatim."""
    return _ARTIFACT_RE.sub(" ", text or "")


def extract(content):
    content = _strip_artifacts(content)
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=120)
    prompt = (
        "你是一个知识图谱提取引擎。只输出JSON对象，禁止markdown代码块，禁止解释。\n"
        "允许实体类型: file, function, concept, error, tool, person, product, technology\n"
        "允许关系: depends_on, modifies, fixes, exemplifies, prefers_over, contradicts, reinforces, evolved_into, invalidated_by, leads_to, derived_from, part_of\n"
        '严格按此格式输出: {"entities":[{"name":"x","type":"concept","confidence":0.9}],"relations":[{"source":"a","target":"b","type":"depends_on","confidence":0.8}]}\n'
        "要求: 实体名称必须在原文出现；置信度0-1，低于0.7不输出；只提取原文明确提到的信息。\n"
        "内容:\n" + content[:2500]
    )
    for attempt in range(3):
        try:
            r = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                # 800 tokens truncated entity-rich outputs mid-JSON: failures
                # clustered at fixed offsets (char 552 / 2458) and, because
                # temperature=0 is deterministic, all 3 retries broke at the
                # same byte. 2048 covers the observed output sizes.
                # Slot KV is c/np = 4096/8 = 512 tokens. Observed KG outputs
                # are 290-350 tokens, so 480 fits with headroom while keeping
                # the fast small-KV server layout (28 jobs/min vs 12 at np=1).
                # _salvage_truncated_json() still covers rare overruns.
                max_tokens=1500,
                extra_body={"thinking": {"type": "disabled"}},
            )
            choices = getattr(r, "choices", None) or []
            if not choices:
                raise RuntimeError("empty_model_choices")
            msg = getattr(choices[0], "message", None)
            t = getattr(msg, "content", None) or ""
            m = re.search(r"\{[\s\S]*\}", t)
            if not m:
                raise RuntimeError("llm_returned_non_json")
            try:
                data = json.loads(m.group(0))
            except Exception as e:
                data = _salvage_truncated_json(m.group(0))
                if data is None:
                    raise RuntimeError("llm_returned_invalid_json: " + str(e)[:120]) from e
            entities = data.get("entities")
            relations = data.get("relations")
            if isinstance(entities, dict) or isinstance(relations, dict):
                raise RuntimeError("llm_returned_wrong_json_shape")
            if not isinstance(entities, list) or not isinstance(relations, list):
                raise RuntimeError("llm_returned_wrong_json_shape")
            return {"entities": entities, "relations": relations}
        except Exception as e:
            err = str(e)
            etype = type(e).__name__
            # Record the real cause; "llm_extract_failed" alone hid whether this
            # was a timeout, a transport error, or malformed model output.
            try:
                with open("/tmp/kg_extract_errors.log", "a") as _fh:
                    _fh.write("%s|%s|%s\n" % (time.strftime("%H:%M:%S"), etype, err[:160].replace("\n", " ")))
            except Exception:
                pass
            transient = ("429" in err or "RateLimit" in etype or
                         "rate" in err.lower() or "empty_model_choices" in err or
                         "llm_returned_" in err or "timed out" in err.lower() or
                         "timeout" in err.lower() or "Timeout" in etype or
                         "APIConnection" in etype or "APIStatus" in etype)
            if transient and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            LAST_ERROR_DETAIL.append("%s: %s" % (etype, err[:110]))
            return None
    return None

def verify_entity_in_text(name, text):
    if not name or not text:
        return False
    return name.lower() in text.lower()

def _insert_source(cur, entity_id, relation_id, message_id, source_text, confidence, job_id):
    tid = os.urandom(16).hex()
    try:
        if entity_id:
            cur.execute(
                "INSERT INTO kg_extraction_sources (id,entity_id,message_id,source_text,confidence,extraction_model,created_at) VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                (tid, entity_id, message_id, source_text[:500], float(confidence), MODEL)
            )
        elif relation_id:
            cur.execute(
                "INSERT INTO kg_extraction_sources (id,relation_id,message_id,source_text,confidence,extraction_model,created_at) VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                (tid, relation_id, message_id, source_text[:500], float(confidence), MODEL)
            )
        else:
            raise ValueError("no source anchor")
        return True
    except Exception as e:
        try:
            cur.execute(
                "UPDATE kg_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                (f"{MODEL}: source_write_failed: {_safe_error_text(e)}", job_id)
            )
        except Exception:
            pass
        return False

def process_job(job):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        result = extract(job["content"])
        if result is None:
            cur.execute(
                "UPDATE kg_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                (f"{MODEL}: llm_extract_failed", job["id"])
            )
            conn.commit()
            return False

        entity_ids = {}
        for e in result.get("entities", []):
            try:
                name = str(e.get("name", "")).strip()
                etype = str(e.get("type", "concept")).strip() or "concept"
                conf = float(e.get("confidence", 1.0))
            except Exception:
                continue
            if not name or conf < CONFIDENCE_THRESHOLD or not verify_entity_in_text(name, job["content"]):
                continue
            cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s", (job["session_id"], name))
            row = cur.fetchone()
            if row:
                entity_ids[name] = row["id"]
                continue
            eid = os.urandom(16).hex()
            cur.execute(
                "INSERT INTO kg_entities (id,session_id,name,entity_type,level,created_at) VALUES (%s,%s,%s,%s,'L1',NOW())",
                (eid, job["session_id"], name, etype)
            )
            entity_ids[name] = eid
            _insert_source(cur, eid, None, job["message_id"], job["content"], conf, job["id"])

        for rel in result.get("relations", []):
            try:
                src = str(rel.get("source", "")).strip()
                tgt = str(rel.get("target", "")).strip()
                rtype = str(rel.get("type", "")).strip().lower()
                conf = float(rel.get("confidence", 1.0))
            except Exception:
                continue
            if rtype not in ALLOWED_RELATIONS or conf < CONFIDENCE_THRESHOLD:
                continue
            si, ti = entity_ids.get(src), entity_ids.get(tgt)
            if not si or not ti or not verify_entity_in_text(src, job["content"]) or not verify_entity_in_text(tgt, job["content"]):
                continue
            rid = os.urandom(16).hex()
            cur.execute(
                "INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,relation_type,valid_at,created_at) VALUES (%s,%s,%s,%s,%s,NOW(),NOW()) ON CONFLICT DO NOTHING",
                (rid, job["session_id"], si, ti, rtype)
            )
            _insert_source(cur, None, rid, job["message_id"], job["content"], conf, job["id"])

        cur.execute("UPDATE kg_jobs SET status='completed', model_used=%s, updated_at=now() WHERE id=%s", (MODEL, job["id"]))
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
            cur.execute(
                "UPDATE kg_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                (f"{MODEL}: worker_exception: {_safe_error_text(e)}", job["id"])
            )
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
    try:
        cur.execute("SELECT count(*) FROM kg_jobs WHERE status='pending'")
        pending = cur.fetchone()["count"]
        if pending == 0:
            return
        cur.execute(
            "SELECT j.id, j.message_id, j.session_id, m.content "
            "FROM kg_jobs j JOIN messages m ON j.message_id = m.id "
            "WHERE j.status='pending' ORDER BY j.created_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (BATCH,)
        )
        jobs = cur.fetchall()
        if not jobs:
            return
        for job in jobs:
            cur.execute("UPDATE kg_jobs SET status='running', locked_at=now() WHERE id=%s", (job["id"],))
        conn.commit()
    finally:
        cur.close()
        conn.close()

    print(f"pending={len(jobs)} batch={len(jobs)} model={MODEL}", flush=True)
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
