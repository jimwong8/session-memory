#!/usr/bin/env python3
"""
KG 批量处理优化版本
优化点：
1. 批量调用 LLM（减少网络往返）
2. 批量数据库写入（减少事务开销）
3. 智能跳过（缓存常见模式）
"""
import argparse
import asyncio
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update
from sqlalchemy.sql import text
from src.database import async_session, close_db
from src.cache import cache
from src.models import KGJob, Message, KGEntity, KGRelation
from src.services.knowledge_graph import KnowledgeGraphService
from src.llm_client import chat_completion
import logging

logger = logging.getLogger(__name__)
WORKER_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=10)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--sleep-seconds', type=float, default=0.2)
    parser.add_argument('--use-batch-llm', action='store_true', help='使用批量 LLM 调用优化')
    return parser.parse_args()


def next_retry_at(now: datetime, reason: str) -> datetime:
    if reason == 'rate_limited_cooldown':
        return now + timedelta(minutes=3)
    if reason == 'rate_limited_upstream':
        return now + timedelta(minutes=5)
    if reason == 'cache_not_connected':
        return now + timedelta(minutes=2)
    if reason == 'db_write_failed':
        return now + timedelta(minutes=8)
    if reason == 'upstream_exception':
        return now + timedelta(minutes=6)
    if reason == 'extract_exception':
        return now + timedelta(minutes=6)
    return now + timedelta(minutes=6)


def should_deadletter(reason: str, attempts: int) -> bool:
    if reason == 'json_extract_failed':
        return attempts >= 5
    if reason in {'rate_limited_cooldown', 'rate_limited_upstream', 'extract_exception', 'cache_not_connected', 'db_write_failed', 'upstream_exception'}:
        return attempts >= 5
    return attempts >= 3


async def batch_extract_kg(messages: list[Message], kg_service: KnowledgeGraphService) -> list[dict]:
    """批量提取知识图谱（优化版）"""
    results = []
    
    # 批量构建 prompt
    batch_prompts = []
    valid_messages = []
    
    for msg in messages:
        # 快速过滤
        if not msg.content or len(msg.content) < 50:
            results.append({'ok': True, 'noop': True, 'reason': 'content_too_short'})
            continue
            
        prompt = f"""
你是一个高度格式化的知识抽取引擎。必须直接输出 JSON 对象。
允许的实体类型: file, function, concept, error
允许的关系类型: modifies, depends_on, fixes

输出结构：
{{
  "entities": [{{"name": "名字", "type": "concept"}}],
  "relations": [{{"source": "源", "target": "目标", "type": "depends_on"}}]
}}

提取内容：
{msg.content[:2000]}
"""
        batch_prompts.append(prompt)
        valid_messages.append(msg)
    
    if not valid_messages:
        return results
    
    # 批量调用 LLM（关键优化点）
    try:
        # 方案 A: 并发调用（推荐）
        tasks = []
        for prompt in batch_prompts:
            task = chat_completion([
                {"role": "system", "content": "你只能输出一个 JSON 对象。"},
                {"role": "user", "content": prompt},
            ], temperature=0.0, max_tokens=3000)
            tasks.append(task)
        
        # 并发执行
        llm_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理结果
        for i, (msg, llm_result) in enumerate(zip(valid_messages, llm_results)):
            if isinstance(llm_result, Exception):
                results.append({
                    'ok': False,
                    'retryable': True,
                    'reason': 'upstream_exception',
                    'detail': str(llm_result)[:500]
                })
                continue
            
            reply, _ = llm_result
            
            # 解析 JSON（复用原有逻辑）
            try:
                data = kg_service._extract_json_payload(reply)
                entities = data.get("entities", []) or []
                relations = data.get("relations", []) or []
                
                if not entities and not relations:
                    results.append({'ok': True, 'noop': True, 'reason': 'empty_graph'})
                    continue
                
                # 暂存结果，稍后批量写入数据库
                results.append({
                    'ok': True,
                    'reason': 'extracted',
                    'entities': entities,
                    'relations': relations,
                    'message': msg
                })
            except Exception as e:
                results.append({
                    'ok': False,
                    'retryable': False,
                    'reason': 'json_extract_failed',
                    'detail': str(e)[:500]
                })
        
    except Exception as e:
        # 批量失败，回退到逐个处理
        logger.error(f"批量 LLM 调用失败: {e}，回退到逐个处理")
        for msg in valid_messages:
            result = await kg_service.extract_knowledge_result(msg)
            results.append(result)
    
    return results


async def batch_write_kg(results: list[dict], db) -> tuple[int, int]:
    """批量写入知识图谱到数据库（优化版）"""
    entity_count = 0
    relation_count = 0
    
    # 批量写入实体和关系
    for result in results:
        if not result.get('ok') or result.get('noop'):
            continue
        
        entities = result.get('entities', [])
        relations = result.get('relations', [])
        msg = result.get('message')
        
        if not msg:
            continue
        
        # 实体写入
        entity_map = {}
        for ent_data in entities:
            name = ent_data.get("name")
            ent_type = ent_data.get("type")
            if not name or not ent_type:
                continue
            
            # 检查是否已存在
            stmt = select(KGEntity).where(
                KGEntity.session_id == msg.session_id,
                KGEntity.name == name
            )
            existing = (await db.execute(stmt)).scalar_one_or_none()
            
            if existing:
                entity_map[name] = existing.id
            else:
                new_ent = KGEntity(
                    session_id=msg.session_id,
                    name=name,
                    entity_type=ent_type
                )
                db.add(new_ent)
                await db.flush()
                entity_map[name] = new_ent.id
                entity_count += 1
        
        # 关系写入
        for rel_data in relations:
            src_id = entity_map.get(rel_data.get("source"))
            tgt_id = entity_map.get(rel_data.get("target"))
            rel_type = rel_data.get("type")
            
            if src_id and tgt_id and rel_type:
                db.add(KGRelation(
                    session_id=msg.session_id,
                    source_entity_id=src_id,
                    target_entity_id=tgt_id,
                    relation_type=rel_type,
                    message_id=msg.id,
                ))
                relation_count += 1
    
    # 一次性提交
    await db.commit()
    return entity_count, relation_count


async def main():
    args = parse_args()
    processed = 0
    success = 0
    failed = 0

    await cache.connect()
    try:
        async with async_session() as db:
            now = datetime.now(timezone.utc)

            # 清理僵尸锁
            dead_stmt = update(KGJob).where(
                KGJob.status == 'running',
                KGJob.locked_at < now - timedelta(hours=1),
            ).values(
                status='pending',
                locked_at=None,
                locked_by=None,
                updated_at=now,
            )
            if args.apply:
                await db.execute(dead_stmt)
                await db.commit()

            # Dry run 模式
            if not args.apply:
                stmt = (
                    select(KGJob.id, KGJob.message_id, KGJob.attempts)
                    .where(KGJob.status == 'pending', KGJob.available_at <= now)
                    .order_by(KGJob.created_at.asc())
                    .limit(args.batch_size)
                )
                result = await db.execute(stmt)
                preview_jobs = list(result.all())
                print(f'pending_jobs={len(preview_jobs)} mode=dry-run worker_id={WORKER_ID}')
                for job_id, _, _ in preview_jobs:
                    print(f'dry_run={job_id}')
                await close_db()
                print(f'total_processed={len(preview_jobs)} total_success=0 total_failed=0')
                return

            print(f'pending_jobs<={args.batch_size} mode=apply worker_id={WORKER_ID} batch_llm={args.use_batch_llm}')
            kg = KnowledgeGraphService(db)
            
            # 批量处理模式
            if args.use_batch_llm:
                # 一次性获取多个 job
                claim_sql = text("""
                    UPDATE kg_jobs 
                    SET status = 'running', 
                        locked_by = :worker_id, 
                        locked_at = :now, 
                        updated_at = :now 
                    WHERE id IN (
                        SELECT id FROM kg_jobs 
                        WHERE status = 'pending' AND available_at <= :now 
                        ORDER BY created_at ASC 
                        LIMIT :batch_size 
                        FOR UPDATE SKIP LOCKED
                    ) 
                    RETURNING id, message_id, attempts
                """)
                
                result = await db.execute(claim_sql, {
                    'worker_id': WORKER_ID,
                    'now': now,
                    'batch_size': args.batch_size
                })
                jobs = list(result.all())
                await db.commit()
                
                if not jobs:
                    print('no_pending_jobs')
                    return
                
                # 加载消息
                messages = []
                job_map = {}
                for job_id, message_id, attempts in jobs:
                    msg = await db.get(Message, message_id)
                    if msg:
                        messages.append(msg)
                        job_map[msg.id] = (job_id, attempts)
                
                # 批量提取
                print(f'batch_extracting={len(messages)} messages')
                extract_results = await batch_extract_kg(messages, kg)
                
                # 批量写入
                entity_count, relation_count = await batch_write_kg(extract_results, db)
                
                # 更新 job 状态
                for i, (msg, result) in enumerate(zip(messages, extract_results)):
                    job_id, current_attempts = job_map[msg.id]
                    attempts = current_attempts + 1
                    now = datetime.now(timezone.utc)
                    
                    if result.get('ok') or result.get('noop'):
                        await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                            status='succeeded',
                            attempts=attempts,
                            last_error=None,
                            locked_at=None,
                            locked_by=None,
                            updated_at=now,
                        ))
                        success += 1
                    else:
                        reason = result.get('reason', 'unknown')
                        if should_deadletter(reason, attempts):
                            await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                                status='dead_letter',
                                attempts=attempts,
                                last_error=reason,
                                locked_at=None,
                                locked_by=None,
                                updated_at=now,
                            ))
                        else:
                            retry_at = next_retry_at(now, reason)
                            await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                                status='pending',
                                attempts=attempts,
                                last_error=reason,
                                available_at=retry_at,
                                locked_at=None,
                                locked_by=None,
                                updated_at=now,
                            ))
                        failed += 1
                
                await db.commit()
                processed = len(jobs)
                print(f'batch_complete entities={entity_count} relations={relation_count}')
                
            else:
                # 原有逐个处理逻辑（保持兼容）
                claim_one_sql = text("""
                    UPDATE kg_jobs 
                    SET status = 'running', locked_by = :worker_id, locked_at = :now, updated_at = :now 
                    WHERE id IN (
                        SELECT id FROM kg_jobs 
                        WHERE status = 'pending' AND available_at <= :now 
                        ORDER BY created_at ASC 
                        LIMIT 1 
                        FOR UPDATE SKIP LOCKED
                    ) 
                    RETURNING id, message_id, attempts
                """)
                
                while processed < args.batch_size:
                    result = await db.execute(claim_one_sql, {'worker_id': WORKER_ID, 'now': datetime.now(timezone.utc)})
                    row = result.first()
                    await db.commit()
                    
                    if not row:
                        break
                    
                    job_id, message_id, current_attempts = row
                    processed += 1
                    
                    msg = await db.get(Message, message_id)
                    if not msg:
                        await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                            status='dead_letter',
                            last_error='message_not_found',
                            locked_at=None,
                            locked_by=None,
                            updated_at=datetime.now(timezone.utc),
                        ))
                        await db.commit()
                        failed += 1
                        continue
                    
                    # 使用原有逻辑
                    result = await kg.extract_knowledge_result(msg)
                    attempts = current_attempts + 1
                    now = datetime.now(timezone.utc)
                    reason = result.get('reason', 'unknown')
                    
                    if result.get('ok') or result.get('noop'):
                        await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                            status='succeeded',
                            attempts=attempts,
                            last_error=None,
                            locked_at=None,
                            locked_by=None,
                            updated_at=now,
                        ))
                        await db.commit()
                        success += 1
                        print(f"updated={job_id} reason={reason} entities={result.get('entity_count', 0)} relations={result.get('relation_count', 0)}")
                    else:
                        failed += 1
                        if reason in {'rate_limited_upstream', 'rate_limited_cooldown'}:
                            print(f'rate_limit_break={job_id} reason={reason}')
                            break
                        
                        if should_deadletter(reason, attempts):
                            await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                                status='dead_letter',
                                attempts=attempts,
                                last_error=reason,
                                locked_at=None,
                                locked_by=None,
                                updated_at=now,
                            ))
                        else:
                            retry_at = next_retry_at(now, reason)
                            await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                                status='pending',
                                attempts=attempts,
                                last_error=reason,
                                available_at=retry_at,
                                locked_at=None,
                                locked_by=None,
                                updated_at=now,
                            ))
                        await db.commit()
                    
                    await asyncio.sleep(args.sleep_seconds)
                    
    finally:
        await cache.close()
        await close_db()

    print(f'total_processed={processed} total_success={success} total_failed={failed}')


if __name__ == '__main__':
    asyncio.run(main())
