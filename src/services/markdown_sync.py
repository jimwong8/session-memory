"""Markdown 异构同步 - 将 L2/L3/canvas/atoms 落地为文件系统可读结构"""

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models import MemoryAtom, MemoryScenario, Persona, Session

logger = logging.getLogger(__name__)


class MarkdownSyncer:
    """将金字塔记忆同步到 Markdown 文件"""

    def __init__(self, db: AsyncSession, root: str | None = None) -> None:
        self.db = db
        self.root = Path(root or settings.markdown_sync_root)

    async def sync_all(self, user_id: str) -> int:
        """同步一个用户的所有记忆到文件"""
        if not settings.markdown_sync_enabled:
            return 0

        base = self.root / f"user={user_id}"
        base.mkdir(parents=True, exist_ok=True)

        (base / "scenarios").mkdir(exist_ok=True)
        (base / "atoms").mkdir(exist_ok=True)
        (base / "canvas").mkdir(exist_ok=True)
        (base / "refs").mkdir(exist_ok=True)

        count = 0
        count += await self._sync_persona(base, user_id)
        count += await self._sync_scenarios(base, user_id)
        count += await self._sync_atoms(base, user_id)
        count += await self._sync_canvases(base, user_id)
        count += await self._clean_orphans(base, user_id)

        return count

    async def _sync_persona(self, base: Path, user_id: str) -> int:
        """同步 L3 画像"""
        stmt = select(Persona).where(Persona.user_id == user_id)
        result = await self.db.execute(stmt)
        persona = result.scalar_one_or_none()
        if not persona:
            return 0

        content = f"""# Persona: {user_id}

Version: {persona.version}
Updated: {persona.updated_at.isoformat() if persona.updated_at else 'N/A'}

## Profile

{persona.profile_md}

## Preferences

```json
{json.dumps(persona.preferences_json or {}, ensure_ascii=False, indent=2)}
```

## Source Scenarios

{chr(10).join(f'- {s}' for s in (persona.scenario_ids or []))}
"""
        (base / "persona.md").write_text(content)

        # 历史版本
        from src.models import PersonaHistory
        hist_stmt = (
            select(PersonaHistory)
            .where(PersonaHistory.user_id == user_id)
            .order_by(PersonaHistory.version.desc())
        )
        hist_result = await self.db.execute(hist_stmt)
        histories = hist_result.scalars().all()

        hist_dir = base / "persona.history"
        hist_dir.mkdir(exist_ok=True)
        for h in histories:
            h_path = hist_dir / f"v{h.version}.md"
            if not h_path.exists():
                h_path.write_text(
                    f"# Persona v{h.version}\n\n{h.profile_md}\n\n## Preferences\n\n"
                    f"```json\n{json.dumps(h.preferences_json or {}, ensure_ascii=False, indent=2)}\n```\n"
                )

        logger.info("synced persona v%s for %s", persona.version, user_id)
        return 1

    async def _sync_scenarios(self, base: Path, user_id: str) -> int:
        """同步 L2 场景"""
        stmt = (
            select(MemoryScenario)
            .where(MemoryScenario.user_id == user_id)
            .order_by(MemoryScenario.created_at.desc())
        )
        result = await self.db.execute(stmt)
        scenarios = result.scalars().all()

        count = 0
        for s in scenarios:
            # 文件名: yyyy-mm-{slug}.md
            date_str = (s.created_at or datetime.utcnow()).strftime("%Y-%m")
            slug = "".join(c for c in (s.title or "")[:40] if c.isalnum() or c in " -_").strip()
            if not slug:
                slug = str(s.id)[:8]
            fname = f"{date_str}-{slug}.md"
            fpath = base / "scenarios" / fname

            atom_ids = "\n".join(f"- {a}" for a in (s.atom_ids or []))
            session_ids = "\n".join(f"- {se}" for se in (s.session_ids or []))

            content = f"""# {s.title}

**Created**: {s.created_at.isoformat() if s.created_at else 'N/A'}
**User**: {s.user_id}
**Project**: {s.project_key or 'N/A'}

## Narrative

{s.narrative_md}

## Source Atoms

{atom_ids or 'N/A'}

## Source Sessions

{session_ids or 'N/A'}

## Period

{s.period_start.isoformat() if s.period_start else 'N/A'} — {s.period_end.isoformat() if s.period_end else 'N/A'}
"""
            fpath.write_text(content)
            count += 1

        logger.info("synced %d scenarios for %s", count, user_id)
        return count

    async def _sync_atoms(self, base: Path, user_id: str) -> int:
        """同步 L1 atoms 为按天 JSONL"""
        stmt = (
            select(MemoryAtom)
            .where(
                MemoryAtom.user_id == user_id,
                MemoryAtom.superseded_by.is_(None),
            )
            .order_by(MemoryAtom.created_at.asc())
        )
        result = await self.db.execute(stmt)
        atoms = result.scalars().all()

        # 按天分组
        daily: dict[str, list[dict]] = {}
        for atom in atoms:
            day = (atom.created_at or datetime.utcnow()).strftime("%Y-%m-%d")
            if day not in daily:
                daily[day] = []
            daily[day].append({
                "id": str(atom.id),
                "kind": atom.kind,
                "title": atom.title,
                "content": atom.content,
                "tags": atom.tags or [],
                "source_message_ids": [str(m) for m in (atom.source_message_ids or [])],
                "confidence_score": atom.confidence_score,
                "sensitivity_level": atom.sensitivity_level,
                "dedup_signature": atom.dedup_signature,
                "session_id": str(atom.session_id),
                "created_at": atom.created_at.isoformat() if atom.created_at else None,
            })

        for day, items in sorted(daily.items()):
            fpath = base / "atoms" / f"{day}.jsonl"
            with open(fpath, "w") as f:
                for item in items:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")

        logger.info("synced %d atoms across %d days for %s", len(atoms), len(daily), user_id)
        return len(atoms)

    async def _sync_canvases(self, base: Path, user_id: str) -> int:
        """同步 Mermaid canvas"""
        stmt = (
            select(Session)
            .where(
                Session.user_id == user_id,
                Session.canvas_mermaid.isnot(None),
            )
        )
        result = await self.db.execute(stmt)
        sessions = result.scalars().all()

        count = 0
        for s in sessions:
            if not s.canvas_mermaid:
                continue
            fname = f"session={s.id}.mmd"
            (base / "canvas" / fname).write_text(
                f"# Canvas for session {s.id}\n# Updated: {datetime.utcnow().isoformat()}\n\n{s.canvas_mermaid}\n"
            )
            count += 1

        return count

    async def _clean_orphans(self, base: Path, user_id: str) -> int:
        """清理不在 DB 中的孤立文件（仅 canvas 和 scenarios）"""
        count = 0
        # 获取有效 session IDs
        stmt = select(Session.id).where(
            Session.user_id == user_id,
            Session.canvas_mermaid.isnot(None),
        )
        result = await self.db.execute(stmt)
        valid_session_ids = {str(row[0]) for row in result}

        # 获取有效 scenario IDs
        sc_stmt = select(MemoryScenario.id).where(MemoryScenario.user_id == user_id)
        sc_result = await self.db.execute(sc_stmt)
        valid_scenario_ids = {str(row[0]) for row in sc_result}

        # canvas 目录清理
        canvas_dir = base / "canvas"
        if canvas_dir.exists():
            for f in canvas_dir.iterdir():
                if f.suffix == ".mmd" and f.name.startswith("session="):
                    sid = f.name.replace("session=", "").replace(".mmd", "")
                    if sid not in valid_session_ids:
                        f.unlink()
                        count += 1

        return count


async def sync_user_memory(db: AsyncSession, user_id: str) -> int:
    """便捷入口：同步一个用户的所有记忆"""
    syncer = MarkdownSyncer(db)
    return await syncer.sync_all(user_id)
