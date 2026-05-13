#!/usr/bin/env python3
import json
import subprocess
from datetime import datetime, timezone


def run_psql(sql: str) -> str:
    cmd = [
        'docker', 'exec', 'session_memory_postgres',
        'psql', '-U', 'postgres', '-d', 'session_memory', '-At', '-F', '\t', '-c', sql,
    ]
    return subprocess.check_output(cmd, text=True).strip()


def main():
    sql = """
    select metadata_json->>'opencode_session_id' as opencode_session_id,
           id,
           coalesce(title,''),
           created_at,
           updated_at,
           coalesce(message_count,0),
           coalesce(total_tokens,0),
           archived
    from sessions
    where metadata_json ? 'opencode_session_id'
      and metadata_json->>'opencode_session_id' in (
        select metadata_json->>'opencode_session_id'
        from sessions
        where metadata_json ? 'opencode_session_id'
        group by 1
        having count(*) > 1
      )
    order by metadata_json->>'opencode_session_id', created_at;
    """
    raw = run_psql(sql)
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        op_id, sid, title, created_at, updated_at, message_count, total_tokens, archived = line.split('\t')
        rows.append({
            'opencode_session_id': op_id,
            'session_id': sid,
            'title': title,
            'created_at': created_at,
            'updated_at': updated_at,
            'message_count': int(message_count),
            'total_tokens': int(total_tokens),
            'archived': archived == 't',
        })
    group_count = len({r['opencode_session_id'] for r in rows})
    result = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': 'ok' if group_count == 0 else 'degraded',
        'duplicate_group_count': group_count,
        'duplicate_rows': rows,
        'issues': [] if group_count == 0 else ['duplicate opencode_session_id groups remain'],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
