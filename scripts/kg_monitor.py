#!/usr/bin/env python3
"""
KG Worker 监控脚本 - 显示验证统计和幻觉率
"""
import psycopg2
import redis
import json
from datetime import datetime, timezone

DB_URL = "postgresql://postgres:postgres@postgres:5432/session_memory"
REDIS_HOST = "redis"

def get_redis_stats():
    """获取 Redis 中的验证统计"""
    try:
        r = redis.Redis(host=REDIS_HOST, port=6379, db=0, socket_timeout=3)
        stats = {}
        for key in r.scan_iter("kg:verification:*"):
            key_str = key.decode()
            value = int(r.get(key) or 0)
            stats[key_str.replace("kg:verification:", "")] = value
        return stats
    except Exception as e:
        return {"error": str(e)}

def get_db_stats():
    """获取数据库统计"""
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    
    stats = {}
    
    # KG 任务状态
    cur.execute("SELECT status, COUNT(*) FROM kg_jobs GROUP BY STATUS")
    stats["kg_jobs"] = {row[0]: row[1] for row in cur.fetchall()}
    
    # 实体和关系统计
    cur.execute("SELECT COUNT(*) FROM kg_entities")
    stats["total_entities"] = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM kg_relations WHERE invalid_at IS NULL")
    stats["total_relations"] = cur.fetchone()[0]
    
    # 溯源记录统计
    cur.execute("SELECT COUNT(*) FROM kg_extraction_sources")
    stats["traceability_records"] = cur.fetchone()[0]
    
    # 最近的提取统计
    cur.execute("""
        SELECT COUNT(*) FROM kg_entities 
        WHERE created_at > NOW() - INTERVAL '1 hour'
    """)
    stats["entities_last_hour"] = cur.fetchone()[0]
    
    cur.execute("""
        SELECT COUNT(*) FROM kg_relations 
        WHERE created_at > NOW() - INTERVAL '1 hour' AND invalid_at IS NULL
    """)
    stats["relations_last_hour"] = cur.fetchone()[0]
    
    # 置信度分布
    cur.execute("""
        SELECT 
            CASE 
                WHEN confidence >= 0.9 THEN 'high'
                WHEN confidence >= 0.7 THEN 'medium'
                ELSE 'low'
            END as confidence_level,
            COUNT(*) as count
        FROM kg_extraction_sources
        GROUP BY confidence_level
    """)
    stats["confidence_distribution"] = {row[0]: row[1] for row in cur.fetchall()}
    
    conn.close()
    return stats

def main():
    print("=" * 60)
    print(f"KG Worker 监控报告 - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)
    
    # Redis 统计
    print("\n📊 验证统计 (Redis):")
    redis_stats = get_redis_stats()
    if "error" in redis_stats:
        print(f"  ❌ Redis 错误: {redis_stats['error']}")
    else:
        for key, value in redis_stats.items():
            print(f"  {key}: {value}")
    
    # 数据库统计
    print("\n💾 数据库统计:")
    db_stats = get_db_stats()
    
    print("\n  KG 任务状态:")
    for status, count in db_stats.get("kg_jobs", {}).items():
        emoji = "✅" if status == "completed" else "⏳" if status == "running" else "❌"
        print(f"    {emoji} {status}: {count}")
    
    print(f"\n  总实体数: {db_stats.get('total_entities', 0)}")
    print(f"  总关系数: {db_stats.get('total_relations', 0)}")
    print(f"  溯源记录: {db_stats.get('traceability_records', 0)}")
    
    print(f"\n  最近1小时:")
    print(f"    新实体: {db_stats.get('entities_last_hour', 0)}")
    print(f"    新关系: {db_stats.get('relations_last_hour', 0)}")
    
    # 置信度分布
    print("\n📈 置信度分布:")
    conf_dist = db_stats.get("confidence_distribution", {})
    if conf_dist:
        total = sum(conf_dist.values())
        for level, count in conf_dist.items():
            percentage = (count / total * 100) if total > 0 else 0
            print(f"    {level}: {count} ({percentage:.1f}%)")
    else:
        print("    暂无数据")
    
    # 计算幻觉率
    total_processed = redis_stats.get("total_processed", 0)
    total_verified = redis_stats.get("total_verified", 0)
    
    if total_processed > 0:
        hallucination_rate = 1 - (total_verified / total_processed)
        print(f"\n🎯 幻觉率: {hallucination_rate:.2%}")
        print(f"   (总处理: {total_processed}, 验证通过: {total_verified})")
    else:
        print(f"\n🎯 幻觉率: 暂无数据")
    
    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()
