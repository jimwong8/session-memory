#!/usr/bin/env python3
import json
import subprocess
from datetime import datetime, timezone


def psql(sql: str) -> str:
    return subprocess.check_output([
        'docker', 'exec', 'session_memory_postgres',
        'psql', '-U', 'postgres', '-d', 'session_memory', '-At', '-F', '\t', '-c', sql,
    ], text=True).strip()


def to_int(v: str) -> int:
    return int(v.strip() or '0')


def main():
    project_session_count = to_int(psql("select count(*) from sessions where metadata_json ? 'project_id';"))
    distinct_project_count = to_int(psql("select count(distinct metadata_json->>'project_id') from sessions where metadata_json ? 'project_id';"))
    summary_sources = to_int(psql("select count(*) from summaries s join sessions ss on s.session_id = ss.id where ss.metadata_json ? 'project_id';"))
    kg_entity_sources = to_int(psql("select count(*) from kg_entities e join sessions ss on e.session_id = ss.id where ss.metadata_json ? 'project_id';"))
    project_samples_raw = psql("select metadata_json->>'project_id', count(*) from sessions where metadata_json ? 'project_id' group by 1 order by count(*) desc limit 5;")

    samples = []
    for line in project_samples_raw.splitlines():
        if not line.strip():
            continue
        project_id, cnt = line.split('\t')
        sample_summaries = to_int(psql(f"select count(*) from summaries s join sessions ss on s.session_id = ss.id where ss.metadata_json->>'project_id' = '{project_id}';"))
        sample_entities = to_int(psql(f"select count(*) from kg_entities e join sessions ss on e.session_id = ss.id where ss.metadata_json->>'project_id' = '{project_id}';"))
        samples.append({
            'project_id': project_id,
            'session_count': int(cnt),
            'summary_count': sample_summaries,
            'kg_entity_count': sample_entities,
        })

    status = 'ok'
    issues = []
    if project_session_count > 0 and summary_sources == 0:
        status = 'warning'
        issues.append('project_id sessions exist but no shared summaries are available yet')
    if project_session_count > 0 and kg_entity_sources == 0:
        status = 'warning'
        issues.append('project_id sessions exist but no shared KG entities are available yet')

    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': status,
        'project_session_count': project_session_count,
        'distinct_project_count': distinct_project_count,
        'summary_sources': summary_sources,
        'kg_entity_sources': kg_entity_sources,
        'project_samples': samples,
        'issues': issues,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
