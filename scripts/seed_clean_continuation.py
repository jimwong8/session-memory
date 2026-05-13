#!/usr/bin/env python3
import json
import urllib.request
from datetime import datetime, timezone

BASE = 'http://127.0.0.1:8000/api/v1'
source_session_id = 'ses_controlled_continuation_validation'
payload = {
    'source_session_id': source_session_id,
    'source_session_memory_id': 'sm_controlled_continuation_validation',
    'failure_code': 'context_overflow_unrecoverable',
    'failure_message': '受控验证：上一轮会话因上下文溢出停止。',
    'reason_detail': 'context_overflow',
    'error_preview': 'controlled validation payload',
    'last_user_message': '请继续完成 session-memory 自动续接链路的最终验收。',
    'recent_messages': [
        {
            'role': 'user',
            'content': '请继续完成 session-memory 自动续接链路的最终验收。',
            'created_at': '2026-04-25T19:00:00Z',
        },
        {
            'role': 'assistant',
            'content': '我会先验证 latest continuation，再确认新会话自动消费。',
            'created_at': '2026-04-25T19:00:10Z',
        },
    ],
    'captured_message_count': 2,
    'created_at': datetime.now(timezone.utc).isoformat(),
    'host': 'controlled-seed',
}
req = urllib.request.Request(
    f'{BASE}/plugin-state/continuation',
    data=json.dumps(payload).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
    method='POST',
)
with urllib.request.urlopen(req, timeout=20) as resp:
    print(resp.read().decode('utf-8'))
