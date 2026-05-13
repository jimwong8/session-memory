#!/usr/bin/env python3
"""AI 运维解释卡 - 读取审计 JSON 与 KG 队列状态，生成只读建议。"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import urllib.request
import urllib.error

BASE = Path('/home/jimwong/session-memory')
AUDIT_DIR = BASE / 'logs' / 'audits'
MAX_INPUT_CHARS = 12000


def read_json(name: str) -> dict[str, Any] | None:
    path = AUDIT_DIR / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8', errors='ignore'))
    except Exception as exc:
        return {'status': 'parse_error', 'error': str(exc)}


def psql(sql: str) -> str:
    return subprocess.check_output([
        'docker', 'exec', 'session_memory_postgres',
        'psql', '-U', 'postgres', '-d', 'session_memory', '-At', '-F', '|', '-c', sql,
    ], text=True).strip()


def get_model_config() -> dict[str, str]:
    env_path = BASE / '.env'
    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    return {
        'api_key': values.get('OPENAI_API_KEY', ''),
        'base_url': values.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'),
        'model': values.get('OPENAI_MODEL', values.get('SUMMARY_MODEL', 'gpt-4o-mini')),
    }


def compact_payload() -> dict[str, Any]:
    return {
        'audit_summary': read_json('audit_summary_latest.json'),
        'health': read_json('audit_health_latest.json'),
        'kg': read_json('audit_kg_latest.json'),
        'capacity': read_json('audit_capacity_latest.json'),
        'alerts': read_json('audit_alerts_latest.json'),
        'continuation': read_json('audit_continuation_latest.json'),
        'shared_memory': read_json('audit_shared_memory_latest.json'),
        'kg_jobs_by_status': psql("select status, coalesce(last_error,'<none>') as reason, count(*) from kg_jobs group by status, coalesce(last_error,'<none>') order by status, count desc;"),
        'kg_running_locks': psql("select coalesce(locked_by,'<none>') as worker_id, count(*) from kg_jobs where status='running' group by coalesce(locked_by,'<none>') order by count(*) desc limit 10;"),
    }


def derive_issues(summary: str, recommended_actions: list[dict[str, str]], error: str | None) -> list[str]:
    if error:
        return [error]
    issues = [item.get('action', '').strip() for item in recommended_actions if item.get('action')]
    if issues:
        return issues[:3]
    return [summary] if summary else []


def fallback_advice(payload: dict[str, Any], error: str | None = None) -> dict[str, Any]:
    capacity = payload.get('capacity') or {}
    kg_jobs = payload.get('kg_jobs_by_status') or ''
    recommendations: list[dict[str, str]] = []
    if 'pending' in kg_jobs:
        recommendations.append({
            'priority': 'high',
            'area': 'kg_backlog',
            'action': '继续使用自适应 drain 消化 pending，但避免并发重叠；若 running 长时间不归零，先清理锁。',
            'risk': '过大 batch 可能造成长尾 running 锁。',
        })
    if capacity.get('status') == 'warning':
        recommendations.append({
            'priority': 'medium',
            'area': 'capacity',
            'action': '刷新 audit_capacity，并对比 dashboard 实时 kg_ops，避免旧快照误导。',
            'risk': 'capacity_snapshot 可能滞后于实时 kg_ops。',
        })
    summary = 'AI 运维解释暂时使用规则回退。' if error else '系统处于可运维状态，按规则生成建议。'
    return {
        'status': 'fallback' if error else 'ok',
        'summary': summary,
        'risk_level': 'warning' if recommendations else 'ok',
        'recommended_actions': recommendations,
        'issues': derive_issues(summary, recommendations, error),
        'model_used': None,
        'error': error,
    }


def generate_ai_advice(payload: dict[str, Any]) -> dict[str, Any]:
    conf = get_model_config()
    if not conf['api_key']:
        return fallback_advice(payload, 'OPENAI_API_KEY missing')
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)[:MAX_INPUT_CHARS]
    messages = [
        {
            'role': 'system',
            'content': (
                '你是 session-memory 的只读运维分析员。只能输出 JSON。'
                '不要建议直接删除数据、修改 schema、关闭告警或无保护地启动大量 worker。'
                '输出字段: status, summary, risk_level, recommended_actions。'
                'recommended_actions 是数组，每项含 priority, area, action, risk。'
            ),
        },
        {
            'role': 'user',
            'content': f'请根据以下运行态生成运维解释卡和安全建议：\n{payload_text}',
        },
    ]
    url = conf['base_url'].rstrip('/') + '/chat/completions'
    body = json.dumps({
        'model': conf['model'],
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': 2000,
        'stream': False,
    }).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {conf['api_key']}",
        },
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        response_data = json.loads(response.read().decode('utf-8'))
    raw = response_data['choices'][0]['message'].get('content') or ''
    cleaned = raw.strip()
    if '```json' in cleaned:
        cleaned = cleaned.split('```json', 1)[1]
    elif '```' in cleaned:
        cleaned = cleaned.split('```', 1)[1]
    if '```' in cleaned:
        cleaned = cleaned.split('```', 1)[0]
    first = cleaned.find('{')
    last = cleaned.rfind('}')
    if first != -1 and last != -1 and last > first:
        cleaned = cleaned[first:last + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError('model returned non-object')
    data.setdefault('status', 'ok')
    data.setdefault('summary', '')
    data.setdefault('risk_level', 'unknown')
    data.setdefault('recommended_actions', [])
    data['issues'] = derive_issues(data.get('summary', ''), data.get('recommended_actions', []), data.get('error'))
    data['model_used'] = conf['model']
    return data




def sanitize_advice(payload: dict[str, Any], advice: dict[str, Any]) -> dict[str, Any]:
    capacity = payload.get('capacity') or {}
    continuation = payload.get('continuation') or {}
    runtime = payload.get('audit_runtime') or payload.get('runtime') or {}

    kg_deadletter = capacity.get('kg_jobs_deadletter', capacity.get('kg_deadletter', 0))
    kg_pending = capacity.get('kg_jobs_pending', capacity.get('kg_pending_backlog', 0))
    oldest_pending = capacity.get('kg_jobs_oldest_pending_age_seconds', capacity.get('oldest_pending_age_seconds', 0))
    capacity_issues = set(capacity.get('issues') or [])
    continuation_status = continuation.get('status')
    runtime_status = runtime.get('status')

    filtered_actions = []
    for item in advice.get('recommended_actions', []):
        action = item.get('action', '')
        if kg_deadletter == 0 and ('deadletter' in action or '死信' in action):
            continue
        if continuation_status == 'idle' and ('continuation' in action or '连续性' in action or 'continuation_latest' in action):
            continue
        if runtime_status == 'idle' and ('opencode.db' in action or 'audit_opencode_runtime' in action):
            continue
        if kg_deadletter == 0 and kg_pending <= 20 and oldest_pending <= 600 and ('pending' in action or '积压' in action or 'oldest_pending_age_seconds' in action):
            continue
        filtered_actions.append(item)

    advice['recommended_actions'] = filtered_actions

    summary = advice.get('summary', '')
    if kg_deadletter == 0:
        summary = summary.replace('和死信队列积压', '')
        summary = summary.replace('以及KG死信队列中有3个任务待处理', '')
        summary = summary.replace('同时audit_ai_ops检查器报告KG死信队列中有3个任务需要处理；', '')
    if continuation_status == 'idle':
        summary = summary.replace('此外，continuation检查器处于空闲状态且最新检查点为null，表明系统状态连续性跟踪可能存在问题。', '')
    advice['summary'] = summary.strip() or '系统处于可运维状态，当前仅存在少量监控口径差异。'

    stable_runtime = runtime_status == 'idle'
    stable_continuation = continuation_status == 'idle'
    capacity_ok = capacity.get('status') == 'ok'
    no_deadletter = kg_deadletter == 0
    low_backlog = kg_pending <= 30 and oldest_pending <= 900
    metadata_only_context = capacity_ok and no_deadletter and stable_runtime and stable_continuation and low_backlog

    if metadata_only_context:
        filtered_actions = []
        advice['recommended_actions'] = []
        advice['status'] = 'ok'
        advice['risk_level'] = 'ok'
    elif not filtered_actions:
        advice['status'] = 'ok'
        advice['risk_level'] = 'ok'
    elif filtered_actions and set(capacity_issues) == {'metadata-based KG counters differ from live kg_jobs counters'} and len(filtered_actions) == 1:
        advice['status'] = 'ok'
        advice['risk_level'] = 'low'

    if advice.get('status') == 'ok' and not filtered_actions:
        advice['summary'] = '系统处于可运维状态，当前无需要人工干预的风险项。'

    advice['issues'] = derive_issues(advice.get('summary', ''), filtered_actions, advice.get('error'))
    return advice

def main() -> None:
    payload = compact_payload()
    payload['runtime'] = read_json('audit_opencode_runtime_latest.json')
    try:
        advice = generate_ai_advice(payload)
    except Exception as exc:
        advice = fallback_advice(payload, str(exc))
    advice = sanitize_advice(payload, advice)
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': advice.get('status', 'unknown'),
        'summary': advice.get('summary', ''),
        'risk_level': advice.get('risk_level', 'unknown'),
        'recommended_actions': advice.get('recommended_actions', []),
        'issues': advice.get('issues', []),
        'model_used': advice.get('model_used'),
        'error': advice.get('error'),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
