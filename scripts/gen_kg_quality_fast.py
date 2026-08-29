#!/usr/bin/env python3
"""Generate kg_quality JSON file for Dashboard (fast version, no heavy NOT EXISTS)."""
import json
import psycopg2
from datetime import datetime, timezone
from pathlib import Path

conn = psycopg2.connect("postgresql://postgres:***@postgres:5432/session_memory")
cur = conn.cursor()

cur.execute("SELECT count(*) FROM kg_entities")
ent_total = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM kg_relations")
rel_total = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM kg_relations WHERE invalid_at IS NULL")
rel_valid = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM kg_entities WHERE invalid_at IS NOT NULL")
ent_invalid = cur.fetchone()[0]

# Orphan entities: use reltuples estimate of kg_relations to avoid heavy NOT EXISTS scan
# Count distinct entities referenced by relations, then orphan = total - referenced
cur.execute("""
    SELECT count(DISTINCT e.id) FROM kg_entities e
    WHERE e.invalid_at IS NULL
      AND (e.id IN (SELECT source_entity_id FROM kg_relations)
           OR e.id IN (SELECT target_entity_id FROM kg_relations))
""")
referenced = cur.fetchone()[0]
orphan = max(0, ent_total - ent_invalid - referenced)

avg_rel = round(rel_total / max(ent_total, 1), 1) if ent_total > 0 else 0
quality = min(1.0, max(0.0, 1.0 - (orphan / max(ent_total, 1)) - (ent_invalid / max(ent_total * 3, 1))))

data = {
    "status": "ok",
    "total_entities": ent_total,
    "total_relations": rel_total,
    "valid_relations": rel_valid,
    "invalidated_relations": rel_total - rel_valid,
    "orphan_entities": orphan,
    "invalid_entities": ent_invalid,
    "average_relations_per_entity": avg_rel,
    "quality_score": round(quality, 2),
    "generated_at": datetime.now(timezone.utc).isoformat(),
}

Path("/app/logs/audits/kg_quality_latest.json").write_text(json.dumps(data, indent=2))
print(f"kg_quality: entities={ent_total} relations={rel_total} orphan={orphan} score={quality}")
conn.close()
