#!/usr/bin/env python3
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta


def recent_logs() -> str:
    cmd = ['docker', 'logs', 'session_memory_api', '--tail', '1200']
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)


def parse_ts_prefix(line: str):
    try:
        prefix = line[:19]
        return datetime.strptime(prefix, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except Exception:
        return None


def main():
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    text = recent_logs()
    lines = []
    for line in text.splitlines():
        ts = parse_ts_prefix(line)
        if ts is not None and ts < cutoff:
            continue
        lines.append(line)

    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'window_minutes': minutes,
        'status': 'ok',
        'window_lines': len(lines),
        'kg_fail_json_extract': 0,
        'kg_fail_429': 0,
        'kg_fail_502': 0,
        'kg_raw_think': 0,
        'kg_success': 0,
        'kg_skipped_cooldown': 0,
        'recent_failure_samples': [],
        'issues': [],
    }

    for line in lines:
        if '成功为消息' in line and '提取知识图谱信息' in line:
            report['kg_success'] += 1
        if '跳过知识图谱提取：429 冷却窗口中' in line:
            report['kg_skipped_cooldown'] += 1
        if '提取知识图谱信息失败: 无法从模型返回中提取 JSON' in line:
            report['kg_fail_json_extract'] += 1
            if len(report['recent_failure_samples']) < 10:
                report['recent_failure_samples'].append(line)
        if '提取知识图谱信息失败: Error code: 429' in line:
            report['kg_fail_429'] += 1
            if len(report['recent_failure_samples']) < 10:
                report['recent_failure_samples'].append(line)
        if 'HTTP/1.1 502 Bad Gateway' in line or 'Error code: 502' in line:
            report['kg_fail_502'] += 1
            if len(report['recent_failure_samples']) < 10:
                report['recent_failure_samples'].append(line)
        if '知识图谱原始返回:' in line and '<think>' in line:
            report['kg_raw_think'] += 1

    if report['kg_fail_json_extract'] > 0:
        report['status'] = 'degraded'
        report['issues'].append('knowledge graph JSON extraction failures detected')
    if report['kg_fail_429'] > 0:
        report['status'] = 'degraded'
        report['issues'].append('knowledge graph rate limiting (429) detected')
    if report['kg_fail_502'] > 0:
        report['status'] = 'degraded'
        report['issues'].append('knowledge graph upstream 502 detected')
    if report['kg_raw_think'] > 0:
        report['status'] = 'degraded'
        report['issues'].append('knowledge graph raw responses contain <think> contamination')
    if report['kg_skipped_cooldown'] > 0:
        report['issues'].append('knowledge graph cooldown skips detected')

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
