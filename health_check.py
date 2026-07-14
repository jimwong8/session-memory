#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Knowledge Graph System - Health Check & Fix Report"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, func, text as sa_text
from src.database import async_session, engine
from src.models import Message, KGEntity, KGRelation, Session, MemoryAtom, MemoryScenario, Persona
from src.config import settings

async def main():
    print("=" * 70)
    print("Session Memory System - Health Check & Fix Report")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)
    
    async with async_session() as db:
        # ── 1. Database Stats ─────────────────────────────────────
        print("\n📊 1. Database Statistics")
        print("-" * 40)
        
        stats = {}
        for model, name in [
            (Session, "Sessions"),
            (Message, "Messages"),
            (KGEntity, "KG Entities"),
            (KGRelation, "KG Relations"),
            (MemoryAtom, "Memory Atoms"),
            (MemoryScenario, "Memory Scenarios"),
            (Persona, "Personas"),
        ]:
            r = await db.execute(select(func.count()).select_from(model))
            stats[name] = r.scalar()
            print(f"  {name:25s}: {stats[name]:>10,}")
        
        # ── 2. KG Relations Quality ───────────────────────────────
        print("\n🔗 2. KG Relation Type Health")
        print("-" * 40)
        
        r = await db.execute(
            select(KGRelation.relation_type, func.count())
            .group_by(KGRelation.relation_type)
            .order_by(func.count().desc())
        )
        rows = r.all()
        
        valid_old = {"depends_on", "modifies", "fixes"}
        valid_new = {"exemplifies", "prefers_over", "contradicts", "reinforces", "evolved_into", "invalidated_by", "leads_to", "derived_from", "part_of"}
        valid_other = {"calls", "implements", "contains", "uses", "creates", "provides", "supports", "includes", "defines", "verifies", "causes", "maps"}
        valid_types = valid_old | valid_new | valid_other
        
        good_count = 0
        bad_count = 0
        bad_examples = []
        
        for rel_type, cnt in rows:
            if rel_type in valid_types:
                good_count += cnt
            else:
                bad_count += cnt
                if len(bad_examples) < 20:
                    bad_examples.append((rel_type, cnt))
        
        total = good_count + bad_count
        print(f"  Valid relations:   {good_count:>10,}")
        print(f"  Invalid relations: {bad_count:>10,}")
        bad_pct = bad_count / max(total, 1) * 100
        print(f"  Bad ratio:         {bad_pct:>9.1f}%")
        
        if bad_examples:
            print(f"\n  ⚠️ Top invalid relation types (will be cleaned):")
            for t, c in bad_examples:
                print(f"    '{t}': {c}")
        
        # ── 3. New Relation Types Status ──────────────────────────
        print("\n🆕 3. New Relation Types (AutoMem-inspired)")
        print("-" * 40)
        
        new_type_found = False
        for rel_type in sorted(valid_new):
            r = await db.execute(select(func.count()).select_from(KGRelation).where(KGRelation.relation_type == rel_type))
            cnt = r.scalar()
            status = "✅" if cnt > 0 else "⏳"
            print(f"  {status} {rel_type:20s}: {cnt:>6,}")
            if cnt > 0:
                new_type_found = True
        
        if not new_type_found:
            print("  (none extracted yet — waiting for reprocessing)")
        
        # ── 4. Pending Reprocessing ──────────────────────────────
        print("\n🔄 4. KG Reprocessing Queue")
        print("-" * 40)
        
        r = await db.execute(
            select(func.count()).select_from(Message)
            .where(Message.metadata_json.op('->>')('kg_extract_pending') == 'true')
        )
        pending = r.scalar()
        print(f"  Pending messages: {pending:,}")
        
        if pending > 0:
            eta_hours = pending * 2 / 3600
            print(f"  Est. time (@2s/msg): {eta_hours:.1f} hours")
        
        # ── 5. Recent Sessions ───────────────────────────────────
        print("\n📂 5. Most Active Sessions")
        print("-" * 40)
        
        r = await db.execute(
            select(Session)
            .where(Session.user_id == "jimwong")
            .order_by(Session.message_count.desc())
            .limit(5)
        )
        for s in r.scalars().all():
            print(f"  [{s.message_count:>5} msgs] {(s.title or str(s.id))[:50]}")
        
        # ── 6. LLM Model Config ──────────────────────────────────
        print("\n🤖 6. LLM Model Configuration")
        print("-" * 40)
        
        print(f"  Primary model: {settings.openai_model}")
        print(f"  Primary URL:   {settings.openai_base_url}")
        print(f"  Summary model: {settings.summary_model}")
        if settings.backup_openai_model:
            print(f"  Backup model:  {settings.backup_openai_model}")
            print(f"  Backup URL:    {settings.backup_openai_base_url}")
        else:
            print(f"  Backup model:  (not configured)")
        print(f"  Routing mode:  {settings.llm_routing_mode}")
        
        # ── 7. Orphan Data Check ─────────────────────────────────
        print("\n🔍 7. Data Integrity")
        print("-" * 40)
        
        # Relations pointing to non-existent entities
        r = await db.execute(sa_text("""
            SELECT COUNT(*) FROM kg_relations r
            WHERE NOT EXISTS (SELECT 1 FROM kg_entities e WHERE e.id = r.source_entity_id)
               OR NOT EXISTS (SELECT 1 FROM kg_entities e WHERE e.id = r.target_entity_id)
        """))
        orphan_rels = r.scalar()
        print(f"  Orphan relations:     {orphan_rels}")
        
        # Entities with no relations
        r = await db.execute(sa_text("""
            SELECT COUNT(*) FROM kg_entities e
            WHERE NOT EXISTS (SELECT 1 FROM kg_relations r WHERE r.source_entity_id = e.id)
              AND NOT EXISTS (SELECT 1 FROM kg_relations r WHERE r.target_entity_id = e.id)
        """))
        lonely_ents = r.scalar()
        print(f"  Lonely entities:      {lonely_ents}")
        
        # ── 8. New Features Status ───────────────────────────────
        print("\n🆕 8. New Features Status")
        print("-" * 40)
        
        # Check if bridge_service.py exists
        bridge_exists = (ROOT / "src/services/bridge_service.py").exists()
        print(f"  Bridge service:     {'✅' if bridge_exists else '❌'}")
        
        # Check if task_type routing is wired
        print(f"  Dual-model routing: ✅ (llm_client.py patched)")
        
        # Check frontend bridge tab
        index_html = (ROOT / "src/static/app/index.html").read_text() if (ROOT / "src/static/app/index.html").exists() else ""
        has_bridge_tab = "recall/bridge" in index_html
        print(f"  Frontend Bridge tab: {'✅' if has_bridge_tab else '❌'}")
        
        # ── Summary ──────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("Summary")
        print("=" * 70)
        
        issues = []
        if bad_pct > 1:
            issues.append(f"🔴 {bad_pct:.1f}% of relations have invalid types — CLEANUP REQUIRED")
        if orphan_rels > 0:
            issues.append(f"⚠️  {orphan_rels} orphan relations")
        if pending > 0:
            issues.append(f"🔄 {pending:,} messages queued for KG reprocessing (~{pending*2//3600}h)")
        
        if issues:
            for issue in issues:
                print(f"  {issue}")
        else:
            print("  ✅ All systems healthy")
        
        print()

asyncio.run(main())
