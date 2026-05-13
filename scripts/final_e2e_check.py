#!/usr/bin/env python3
"""最终端到端验收脚本"""
import json
import subprocess
import time
import urllib.request
import urllib.error
from uuid import uuid4

BASE_URL = 'http://localhost:8000'
USER_ID = 'e2e_final_check'
TITLE = 'e2e-final-' + str(uuid4())[:8]
MESSAGE = '最终端到端验收消息：必须稳定落库，不阻塞主请求。'

def wait_for_health(max_attempts=30):
    for i in range(max_attempts):
        try:
            with urllib.request.urlopen(BASE_URL + '/health', timeout=5) as resp:
                if resp.status == 200:
                    print('[OK] API健康检查通过')
                    return True
        except Exception:
            time.sleep(2)
    print('[FAIL] API健康检查超时')
    return False

def post_json(url, payload, timeout=30):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

def query_db(session_id):
    sql = f"""
    SELECT s.id as session_id, s.message_count, m.id as message_id, m.role, 
           left(m.content,80) as preview, (m.embedding is not null) as has_embedding 
    FROM sessions s 
    LEFT JOIN messages m ON m.session_id = s.id 
    WHERE s.id = '{session_id}'
    ORDER BY m.created_at;
    """
    result = subprocess.run(
        ['docker', 'exec', 'session_memory_postgres', 'psql', '-U', 'postgres', 
         '-d', 'session_memory', '-t', '-A', '-F', '|', '-c', sql],
        capture_output=True, text=True
    )
    return result.stdout.strip()

def main():
    print('=== 最终端到端验收 ===')
    
    # 1. 等待健康
    if not wait_for_health():
        return False
    
    # 2. 创建会话
    try:
        session = post_json(BASE_URL + '/api/v1/sessions', {
            'user_id': USER_ID,
            'title': TITLE,
            'metadata_json': {'source': 'e2e-final'}
        })
        session_id = session['id']
        print(f'[OK] 会话创建成功: {session_id}')
    except Exception as e:
        print(f'[FAIL] 会话创建失败: {e}')
        return False
    
    # 3. 写入消息
    try:
        message = post_json(BASE_URL + f'/api/v1/sessions/{session_id}/messages', {
            'role': 'user',
            'content': MESSAGE,
            'metadata_json': {'source': 'e2e-final'}
        }, timeout=30)
        message_id = message['id']
        print(f'[OK] 消息写入成功: {message_id}')
    except Exception as e:
        print(f'[FAIL] 消息写入失败: {e}')
        return False
    
    # 4. 查询数据库
    try:
        db_result = query_db(session_id)
        if not db_result:
            print('[FAIL] 数据库中未找到记录')
            return False
        
        lines = db_result.split('\n')
        print(f'[OK] 数据库验证通过，找到 {len(lines)} 条记录')
        for line in lines:
            parts = line.split('|')
            if len(parts) >= 6:
                msg_id, role, preview, has_emb = parts[2], parts[3], parts[4], parts[5]
                print(f'  - 消息: {msg_id[:8]}... | {role} | embedding={has_emb}')
                print(f'    内容: {preview[:60]}...')
    except Exception as e:
        print(f'[FAIL] 数据库查询失败: {e}')
        return False
    
    print('\n=== 验收结果: PASS ===')
    return True

if __name__ == '__main__':
    success = main()
    exit(0 if success else 1)
