"""Mermaid 画布构建器 - 从关键原子生成任务流程图"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models import MemoryAtom, Session

logger = logging.getLogger(__name__)

# Mermaid 节点颜色按 kind 区分
_KIND_STYLES = {
    "goal": "fill:#dbeafe,stroke:#3b82f6",
    "task_state": "fill:#d1fae5,stroke:#10b981",
    "decision": "fill:#fef3c7,stroke:#f59e0b",
    "blocker": "fill:#fee2e2,stroke:#ef4444",
}


class CanvasBuilder:
    """根据 session 的关键原子生成/更新 Mermaid 流程图"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_for_session(self, session_id: uuid.UUID) -> str | None:
        """为一个 session 生成 Mermaid 画布"""
        atoms = await self._fetch_key_atoms(session_id)
        if not atoms:
            return None

        lines = ["graph LR"]
        node_ids: list[str] = []
        prev_id: str | None = None

        for i, atom in enumerate(atoms):
            node_id = f"N{i}"
            label = (atom.title or atom.content)[:30].replace('"', "'")
            style = _KIND_STYLES.get(atom.kind, "fill:#f8fafc,stroke:#94a3b8")

            # 节点定义
            node_text = f"{node_id}[\"{label}\"]"
            lines.append(f"    {node_text}")

            # 样式
            lines.append(f"    style {node_id} {style}")

            # 连接线（从上一个节点）
            if prev_id:
                # 根据 kind 决定箭头类型
                arrow = "-.->" if atom.kind == "blocker" else "-->"
                lines.append(f"    {prev_id} {arrow} {node_id}")

            node_ids.append(node_id)
            prev_id = node_id

        if len(lines) <= 3:
            return None

        canvas = "\n".join(lines)
        logger.info("[%s] canvas built: %d nodes", session_id, len(node_ids))
        return canvas

    async def update_session_canvas(self, session_id: uuid.UUID) -> bool:
        """生成画布并更新到 session 记录"""
        canvas = await self.build_for_session(session_id)
        if not canvas:
            return False

        stmt = select(Session).where(Session.id == session_id)
        result = await self.db.execute(stmt)
        session = result.scalar_one_or_none()
        if not session:
            return False

        session.canvas_mermaid = canvas
        await self.db.commit()
        return True

    async def _fetch_key_atoms(self, session_id: uuid.UUID) -> list[MemoryAtom]:
        """获取一个 session 中用于画布的关键 atom（按时间排序，最多 15 个）"""
        stmt = (
            select(MemoryAtom)
            .where(
                MemoryAtom.session_id == session_id,
                MemoryAtom.superseded_by.is_(None),
                MemoryAtom.kind.in_([
                    "goal", "task_state", "decision", "blocker",
                ]),
            )
            .order_by(MemoryAtom.created_at.asc())
            .limit(settings.canvas_max_nodes)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
