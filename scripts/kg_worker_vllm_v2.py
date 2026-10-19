#!/usr/bin/env python3
"""KG worker v2 - 摘要→SPO 两级提取合同 + 失败隔离 + LLM 熔断。

改造自 graph-memory (adoresever/graph-memory) 的设计：
1. 提取合同：先写自包含摘要，再从摘要拆 SPO，禁止从原文直接抽
2. 失败隔离：失败 job 进 'quarantined' 状态，不自动 requeue，显式重试
3. LLM 熔断：401/403/404 触发 10min 熔断，429/5xx 重试不熔断
"""
import json, os, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

DB_URL = "postgresql://postgres:***@postgres:5432/session_memory"
BASE_URL = os.getenv("VLLM_BASE_URL", "http://10.100.1.15:8000/v1")
API_KEY = os.getenv("VLLM_API_KEY", "local-no-key")
MODEL = os.getenv("VLLM_MODEL", "/model")

ALLOWED_RELATIONS = {'depends_on','modifies','fixes','exemplifies','prefers_over','contradicts',
                     'reinforces','evolved_into','invalidated_by','leads_to','derived_from','part_of'}
CONFIDENCE_THRESHOLD = 0.7
ALLOWED_ENTITY_TYPES = {'file','function','concept','error','tool','person','product','technology'}
# 名称黑名单：纯数字、百分比、已知元信息词、数字占比>40%
_NAME_BLACKLIST = re.compile(
    r'^[\d\.,\/%]+$'
    r'|^(kg|messages|sessions|embeddings|dashboard|audit|pending|running|completed|failed'
    r'|count|total|entities|relations|done|error|ok|none|nan|undefined|health|status'
    r'|kg_jobs|kg_entities|kg_relations|session_memory|api|postgres|redis|docker|opencode'
    r'|hermes|warning|warn|invalid_at|valid_at|transaction_at|created_at|updated_at|locked_at'
    r'|available_at|extraction_model|source_text|message_id|session_id|entity_id|relation_id)$',
    re.I
)
# 复合黑名单：名称中包含以下子串 → 跳过
_NAME_SUBSTR_BLACKLIST = ['kg_', 'audit_', '_at', 'pending', 'running', 'completed', 'failed', 'locked', 'session_memory']
_MIN_NAME_LEN = 2
_MAX_DIGIT_RATIO = 0.3
BATCH = int(os.getenv("KG_BATCH", "200"))
MAX_WORKERS = int(os.getenv("KG_WORKERS", "32"))
KG_PARTITION = int(os.getenv("KG_PARTITION", "0"))
KG_TOTAL_PARTITIONS = int(os.getenv("KG_TOTAL_PARTITIONS", "1"))

# ─── LLM 熔断器 ───────────────────────────────────────────────
# 401/403/404 → 熔断 10 分钟（凭据/端点问题，重试无意义）
# 429/500/502/503/529 → 可重试，不熔断
_PAUSING_STATUSES = {401, 403, 404}
_RETRYABLE_STATUSES = {429, 500, 502, 503, 529}
_circuit_breaker = {"paused_until": 0.0}

def _extract_llm_status(error_text):
    m = re.search(r'\b(?:LLM|API|HTTP)\s*(?:API\s*)?(\d{3})\b', str(error_text))
    return int(m.group(1)) if m else None

def _is_circuit_breaking(error_text):
    status = _extract_llm_status(error_text)
    return status in _PAUSING_STATUSES

def _is_retryable(error_text):
    status = _extract_llm_status(error_text)
    if status in _RETRYABLE_STATUSES:
        return True
    err_lower = str(error_text).lower()
    return any(kw in err_lower for kw in ["timed out", "timeout", "rate limit", "rate gate",
                                          "circuit open", "empty_model_choices",
                                          "llm_returned_", "api connection", "api status"])

def _check_circuit_breaker():
    """返回 True 表示可以运行，False 表示熔断中"""
    return time.monotonic() >= _circuit_breaker["paused_until"]

def _trip_circuit_breaker(cooldown_s=600):
    _circuit_breaker["paused_until"] = time.monotonic() + cooldown_s

def _safe_error_text(e):
    s = f"{type(e).__name__}: {str(e)[:150]}"
    return s.replace("%", "pct")

def _clean_artifact(text):
    """清洗 Hermes/OpenCode artifact，防止 [SILENT] 等进入摘要。"""
    import re
    text = re.sub(r'\[SILENT\]', '', text)
    text = re.sub(r'\[IMPORTANT:.*?\]', '', text, flags=re.DOTALL)
    text = re.sub(r'\[OUT-OF-BAND.*?\].*?\[/OUT-OF-BAND.*?\]', '', text, flags=re.DOTALL)
    text = re.sub(r'DELIVERY:.*?(\n|$)', '', text)
    text = re.sub(r'```\s*\n.*?```', '', text, flags=re.DOTALL)
    return text.strip()

def _is_system_message(text):
    """识别系统消息/报告/HTML → 无需摘要，直接 informational。"""
    if not text:
        return True
    # HTML/XML
    if text.strip().startswith('<') and text.strip().endswith('>'):
        return True
    # 纯数字/统计报告
    if re.match(r'^[\d\.,\s\-\+%]+$', text):
        return True
    # 过短
    if len(text) < 20:
        return True
    # 纯 markdown 表格（每行都有 | 且行数>5 且非空行都有 |）
    lines = [l for l in text.split('\n') if l.strip()]
    if len(lines) > 5 and all('|' in line for line in lines) and text.count('|') > len(lines) * 2:
        return True
    return False

LAST_ERROR_DETAIL = []

# ─── 提取合同（中文、合同式、带自检） ─────────────────────────
# 核心原则：先写摘要，再从摘要拆 SPO。摘要不存在的事实，三元组在源头被禁止。
_EXTRACT_SYS = """【任务】
把一段对话转换为一句自包含摘要和零条或多条主语—谓词—宾语关系。

【输入】
- Content 是用户与助手的对话原文，是本轮唯一事实来源。

【处理原则】
1. summary 用一句简短、自包含的话写清本轮对象、结论和最终回答明确报告的完成情况；不复述过程，不添加输入未表达的信息。
2. outcome 按最终回答选择 completed、partial、failed、informational、unknown 之一。
3. triples 只从 summary 拆分——summary 中不存在的事实绝不能出现在 triples 里。subject 和 object 使用具体可检索短语，predicate 使用简短自然语言；没有明确关系时使用空数组，不补充、不猜测。

【输出合同】
只输出一个 JSON 对象，必须且只能包含 summary、outcome、triples 三个顶层字段，三个字段都不能省略。禁止 markdown 代码块，禁止解释或正文。

示例一：
输入：用户要求把周会改到周四；最终回答确认日程已更新。
输出：{"summary":"周会已改到周四，日程已更新。","outcome":"completed","triples":[{"subject":"周会","predicate":"改到","object":"周四"}]}

示例二：
输入只确认稍后继续讨论，没有新结论。
输出：{"summary":"本轮确认稍后继续讨论，未产生新结论。","outcome":"informational","triples":[]}

调用前自检：参数是否恰好包含三个顶层字段；outcome 是否属于枚举；没有关系时 triples 是否仍明确写为 []。"""

def _build_extract_prompt(content):
    return f"{_EXTRACT_SYS}\n\n【Content】\n{content[:2500]}"

def _iter_json_candidates(text):
    r"""Yield balanced-brace JSON object candidates from arbitrary LLM output.

    LLM 可能先输出推理文本再给 JSON，贪婪正则 (\{[\s\S]*\}) 会从第一个 '{'
    跨到最后一个 '}'，把中间内容一起吞掉导致 "Extra data"。这里做括号配对扫描，
    正确处理字符串字面量与转义，逐个产出可解析的 JSON 对象片段。
    """
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[i:j + 1]
                        i = j + 1
                        break
            j += 1
        else:
            # 未闭合（被 max_tokens 截断）——交给 salvage 处理
            yield text[i:]
            return


def _extract_json_payload(text):
    """从 LLM 输出中提取 {summary, outcome, triples} 结构。

    返回 dict 或 None。优先选最后一个能解析出 triples 列表的对象
    （模型常先给示例 JSON 再给最终 JSON）。
    """
    fallback = None
    best = None
    for cand in _iter_json_candidates(text):
        try:
            data = json.loads(cand)
        except Exception:
            salvaged = _salvage_truncated_json(cand)
            if salvaged is not None:
                fallback = salvaged
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("triples"), list):
            # 模型通常在推理之后才给最终 JSON，故取最后一个带 triples 的对象
            best = {
                "summary": data.get("summary") if isinstance(data.get("summary"), str) else "",
                "outcome": data.get("outcome", "unknown"),
                "triples": data.get("triples"),
            }
        elif fallback is None and isinstance(data.get("summary"), str):
            fallback = {
                "summary": data.get("summary", ""),
                "outcome": data.get("outcome", "unknown"),
                "triples": [],
            }
    return best if best is not None else fallback


def _salvage_truncated_json(raw):
    """Recover entities/relations from a JSON object truncated mid-stream."""
    txt = raw.strip()
    for cut in range(len(txt), 0, -1):
        chunk = txt[:cut]
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
            summary = data.get("summary")
            triples = data.get("triples")
            if isinstance(triples, list):
                return {
                    "summary": summary if isinstance(summary, str) else "",
                    "outcome": data.get("outcome", "unknown"),
                    "triples": triples,
                }
        return None
    return None

def extract(content):
    """提取入口：返回 {summary, outcome, triples} 或 None（失败）。"""
    if not _check_circuit_breaker():
        LAST_ERROR_DETAIL.append("circuit_breaker: paused")
        return "CIRCUIT_BREAKER"

    # 内容类型识别：系统消息/报告/HTML → 直接标记为 informational，不强制摘要
    cleaned = _clean_artifact(content)
    if not cleaned or len(cleaned) < 30 or _is_system_message(cleaned):
        return {"summary": "", "outcome": "informational", "triples": []}

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=120)
    prompt = _build_extract_prompt(content)

    for attempt in range(3):
        try:
            r = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=2048,
            )
            choices = getattr(r, "choices", None) or []
            if not choices:
                raise RuntimeError("empty_model_choices")
            msg = getattr(choices[0], "message", None)
            t = getattr(msg, "content", None) or ""
            data = _extract_json_payload(t)
            if data is None:
                raise RuntimeError("llm_returned_non_json")

            summary = data.get("summary", "")
            outcome = data.get("outcome", "unknown")
            triples = data.get("triples", [])
            if not isinstance(summary, str) or not isinstance(triples, list):
                raise RuntimeError("llm_returned_wrong_json_shape")
            if outcome not in ("completed", "partial", "failed", "informational", "unknown"):
                outcome = "unknown"
            return {"summary": summary, "outcome": outcome, "triples": triples}

        except Exception as e:
            err = str(e)
            etype = type(e).__name__
            try:
                with open("/tmp/kg_extract_errors.log", "a") as _fh:
                    _fh.write("%s|%s|%s\n" % (time.strftime("%H:%M:%S"), etype, err[:160].replace("\n", " ")))
            except Exception:
                pass

            # 凭据/端点级错误 → 熔断
            if _is_circuit_breaking(err):
                _trip_circuit_breaker(600)
                LAST_ERROR_DETAIL.append(f"{etype}: {err[:110]}")
                return "CIRCUIT_BREAKER"

            # 可重试错误 → 退避重试
            if _is_retryable(err) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue

            LAST_ERROR_DETAIL.append(f"{etype}: {err[:110]}")
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

def _triple_to_entities_relations(triple, job, cur, entity_ids):
    """从一条 SPO triple 提取实体和关系。predicate 映射到 ALLOWED_RELATIONS 或跳过。"""
    try:
        subject = str(triple.get("subject", "")).strip()
        predicate = str(triple.get("predicate", "")).strip().lower()
        obj = str(triple.get("object", "")).strip()
        conf = float(triple.get("confidence", 0.85))
    except Exception:
        return False
    if not subject or not obj or conf < CONFIDENCE_THRESHOLD:
        return False
    # 黑名单：纯数字/百分比/已知元信息词 → 跳过
    if _NAME_BLACKLIST.match(subject) or _NAME_BLACKLIST.match(obj):
        return False
    # 复合黑名单：名称包含 kg_ / audit_ / _at / pending 等子串 → 跳过
    def _has_blacklisted_substr(name):
        return any(sub in name.lower() for sub in _NAME_SUBSTR_BLACKLIST)
    if _has_blacklisted_substr(subject) or _has_blacklisted_substr(obj):
        return False
    # 数字占比检查：名称中数字字符占比 > 40% → 跳过（如 "6,150"、"1.2万+条"）
    def _digit_ratio(s):
        if not s:
            return 0
        return sum(1 for c in s if c.isdigit()) / len(s)
    if _digit_ratio(subject) > _MAX_DIGIT_RATIO or _digit_ratio(obj) > _MAX_DIGIT_RATIO:
        return False
    if len(subject) < _MIN_NAME_LEN or len(obj) < _MIN_NAME_LEN:
        return False
    # predicate 不在允许列表 → 用 derived_from 兜底（通用关系）
    rtype = predicate if predicate in ALLOWED_RELATIONS else "derived_from"
    # 验证实体在原文出现
    if not verify_entity_in_text(subject, job["content"]) or not verify_entity_in_text(obj, job["content"]):
        return False

    # upsert subject entity
    if subject not in entity_ids:
        cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s", (job["session_id"], subject))
        row = cur.fetchone()
        if row:
            entity_ids[subject] = row["id"]
        else:
            eid = os.urandom(16).hex()
            cur.execute(
                "INSERT INTO kg_entities (id,session_id,name,entity_type,level,created_at) VALUES (%s,%s,%s,%s,'L1',NOW())",
                (eid, job["session_id"], subject, "concept")
            )
            entity_ids[subject] = eid
            _insert_source(cur, eid, None, job["message_id"], job["content"], conf, job["id"])

    # upsert object entity
    if obj not in entity_ids:
        cur.execute("SELECT id FROM kg_entities WHERE session_id=%s AND name=%s", (job["session_id"], obj))
        row = cur.fetchone()
        if row:
            entity_ids[obj] = row["id"]
        else:
            eid = os.urandom(16).hex()
            cur.execute(
                "INSERT INTO kg_entities (id,session_id,name,entity_type,level,created_at) VALUES (%s,%s,%s,%s,'L1',NOW())",
                (eid, job["session_id"], obj, "concept")
            )
            entity_ids[obj] = eid
            _insert_source(cur, eid, None, job["message_id"], job["content"], conf, job["id"])

    si, ti = entity_ids.get(subject), entity_ids.get(obj)
    if not si or not ti:
        return False
    rid = os.urandom(16).hex()
    cur.execute(
        "INSERT INTO kg_relations (id,session_id,source_entity_id,target_entity_id,relation_type,valid_at,created_at) VALUES (%s,%s,%s,%s,%s,NOW(),NOW()) ON CONFLICT DO NOTHING",
        (rid, job["session_id"], si, ti, rtype)
    )
    _insert_source(cur, None, rid, job["message_id"], job["content"], conf, job["id"])
    return True

#: 无抽取价值的系统/样板消息前缀 (2026-09-28: failed 队列 1946 条里 772 条是
#: cron 调度器提示词样板, 41 条是异步委派通知)。它们没有可抽取知识, 送模型只会
#: 失败并污染失败率/隔离队列 —— 直接判 skipped。
SKIP_PATTERNS = (
    "[IMPORTANT: You are running as a schedul",
    "[ASYNC DELEGATION",
    "[SILENT]",
    "[CRON_FAILURE",
    "[OUT-OF-BAND USER MESSAGE",
)


def is_boilerplate(content):
    """样板/系统消息判定 (只匹配已知前缀, 不做长度猜测以免误伤真实短消息)。"""
    c = (content or "").lstrip()
    return any(c.startswith(p) for p in SKIP_PATTERNS)


def process_job(job):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if is_boilerplate(job.get("content")):
            cur.execute(
                "UPDATE kg_jobs SET status='skipped', last_error=%s, updated_at=now() WHERE id=%s",
                ("skipped:boilerplate_no_extractable_content", job["id"])
            )
            conn.commit()
            return False
        result = extract(job["content"])

        # 熔断 → 释放回 pending，不计数失败
        if result == "CIRCUIT_BREAKER":
            cur.execute(
                "UPDATE kg_jobs SET status='pending', locked_at=NULL, locked_by=NULL, "
                "last_error=%s, updated_at=now() WHERE id=%s",
                (f"{MODEL}: circuit_breaker_pause", job["id"])
            )
            conn.commit()
            return False

        if result is None:
            last_err = (LAST_ERROR_DETAIL[-1] if LAST_ERROR_DETAIL else "")
            _le = last_err.lower()
            # 2026-09-29: 补「传输级瞬时错误」——实测 quarantined 1860 条里 99.6%
            # 是 APIConnectionError(1258) / InternalServerError <html> 502 页(310)
            # / 500 empty-or-truncated(284), 皆上游抖动, 应回 pending 重试。
            # 真正的「内容无法抽取」(llm_returned_non_json 等) 才留 quarantine 隔离。
            is_infra = ("circuit open" in _le or "rate gate" in _le
                        or "timeout" in _le or "eof" in _le
                        or "apiconnectionerror" in _le
                        or "connection error" in _le
                        or "internalservererror" in _le
                        or "empty or truncated provider response" in _le)
            if is_infra and job.get("attempts", 0) < 6:
                cur.execute(
                    "UPDATE kg_jobs SET status='pending', locked_at=NULL, locked_by=NULL, "
                    "attempts=attempts+1, last_error=%s, updated_at=now() WHERE id=%s",
                    (f"{MODEL}: infra_retry", job["id"])
                )
            else:
                # 失败隔离：进 quarantined，不自动 requeue
                cur.execute(
                    "UPDATE kg_jobs SET status='quarantined', last_error=%s, updated_at=now() WHERE id=%s",
                    (f"{MODEL}: {last_err[:200]}", job["id"])
                )
            conn.commit()
            return False

        summary = result.get("summary", "")
        outcome = result.get("outcome", "unknown")
        triples = result.get("triples", [])

        entity_ids = {}
        for triple in triples:
            try:
                _triple_to_entities_relations(triple, job, cur, entity_ids)
            except Exception:
                continue

        cur.execute(
            "UPDATE kg_jobs SET status='completed', model_used=%s, summary=%s, outcome=%s, updated_at=now() WHERE id=%s",
            (MODEL, summary[:500], outcome, job["id"])
        )
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
            cur.execute(
                "UPDATE kg_jobs SET status='quarantined', last_error=%s, updated_at=now() WHERE id=%s",
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
            "WHERE j.status='pending' "
            "AND abs(hashtext(j.id::text)) %% %s = %s "
            "ORDER BY j.created_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (KG_TOTAL_PARTITIONS, KG_PARTITION, BATCH)
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
    while True:
        try:
            main()
        except Exception as e:
            print(f"main error: {_safe_error_text(e)}", flush=True)
        time.sleep(1)
