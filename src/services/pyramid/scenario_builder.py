"""场景块（L2）构建服务 - 从原子事实聚合为场景叙事"""

import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.embedding_service import create_embedding
from src.llm_client import chat_completion
from src.models import MemoryAtom, MemoryScenario, Persona, Session

logger = logging.getLogger(__name__)

_SCENARIO_PROMPT = """你是一个会话场景总结助手。请根据以下一组相关的记忆原子（事实/决定/偏好等），生成一个场景描述。

场景应该：
1. 标题：一句话概括（20字以内）
2. 叙事：一段markdown描述，说明发生了什么、做了什么决定、有什么结果
3. 只基于提供的原子内容，不做推测
4. 输出JSON格式：{"title": "...", "narrative_md": "..."}

只输出JSON，不要额外内容。
"""


class ScenarioBuilder:
    """将原子事实聚合为场景块"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_for_user(self, user_id: str) -> int:
        """为一个用户构建场景"""
        # 找到没有归属场景的 atoms，按天分桶
        ungrouped = await self._fetch_ungrouped_atoms(user_id)
        if not ungrouped:
            logger.debug("[%s] no ungrouped atoms", user_id)
            return 0

        # 按 (project_key, date) 分桶
        buckets: dict[str, list[MemoryAtom]] = {}
        for atom in ungrouped:
            date_str = atom.created_at.strftime("%Y-%m-%d")
            pk = atom.project_key or "_default"
            bucket_key = f"{user_id}|{pk}|{date_str}"
            if bucket_key not in buckets:
                buckets[bucket_key] = []
            buckets[bucket_key].append(atom)

        count = 0
        for bucket_key, atoms in buckets.items():
            if len(atoms) < 3:
                continue  # 少于 3 个 atom 不构场景
            scenario = await self._build_one(atoms, user_id)
            if scenario:
                self.db.add(scenario)
                count += 1

        if count > 0:
            await self.db.commit()
            logger.info("[%s] built %d scenarios", user_id, count)

        return count

    async def _fetch_ungrouped_atoms(self, user_id: str) -> Sequence[MemoryAtom]:
        """找未归入任何 scenario 的 atoms"""
        # 获取所有 scenario 中的 atom_ids
        scenario_stmt = select(MemoryScenario.atom_ids).where(
            MemoryScenario.user_id == user_id,
        )
        scenario_result = await self.db.execute(scenario_stmt)
        grouped_ids = set()
        for row in scenario_result:
            ids = row[0] or []
            for aid in ids:
                if aid:
                    grouped_ids.add(str(aid))

        # 找所有非 superseded 的 atom，排除已分组的
        stmt = (
            select(MemoryAtom)
            .where(
                MemoryAtom.user_id == user_id,
                MemoryAtom.superseded_by.is_(None),
            )
            .order_by(MemoryAtom.created_at.asc())
            .limit(500)
        )
        result = await self.db.execute(stmt)
        return [a for a in result.scalars().all() if str(a.id) not in grouped_ids]

    async def _build_one(
        self, atoms: list[MemoryAtom], user_id: str
    ) -> MemoryScenario | None:
        """调用 LLM 为一个 atom 桶生成场景"""
        atom_lines = []
        for a in atoms:
            atom_lines.append(f"- [{a.kind}] {a.content[:500]}")
        atom_text = "\n".join(atom_lines)

        session_ids = list({str(a.session_id) for a in atoms if a.session_id})
        atom_ids = [a.id for a in atoms]
        period_start = min(a.created_at for a in atoms)
        period_end = max(a.created_at for a in atoms)

        project_key = next(
            (a.project_key for a in atoms if a.project_key), None
        )

        try:
            reply, _ = await chat_completion(
                messages=[
                    {"role": "system", "content": _SCENARIO_PROMPT},
                    {"role": "user", "content": f"请根据以下 {len(atoms)} 个记忆原子总结场景：\n\n{atom_text}"},
                ],
                temperature=0.3,
                max_tokens=2048,
            )
        except Exception as exc:
            logger.warning("LLM scenario build failed: %s", exc)
            return None

        # 解析 JSON（去 think 块 + markdown 代码块 + 多 } 处理）
        reply = reply.strip()
        # 去掉 think 块
        while "<think>" in reply:
            reply = reply.split("</think>")[-1].strip()
        # 去掉 markdown 代码块
        if reply.startswith("```"):
            lines = reply.split("\n")
            reply = "\n".join(lines[1:-1]) if len(lines) >= 3 else reply

        # 尝试全量解析
        data = None
        try:
            data = json.loads(reply)
        except json.JSONDecodeError:
            pass

        if data is None:
            # 找第一个 { 然后逐字符找匹配的 }
            start = reply.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(reply)):
                    if reply[i] == "{":
                        depth += 1
                    elif reply[i] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                data = json.loads(reply[start:i+1])
                            except (json.JSONDecodeError, ValueError):
                                pass
                            break
                if data is None and depth > 0:
                    # 不匹配就截取到最后一个 }
                    end = reply.rfind("}")
                    if end > start:
                        try:
                            data = json.loads(reply[start:end+1])
                        except (json.JSONDecodeError, ValueError):
                            pass

        if data is None:
            logger.warning("scenario JSON parse failed: %s...", reply[:200])
            return None

        title = (data.get("title") or "未命名场景")[:500]
        narrative = (data.get("narrative_md") or "")[:10000]
        if not narrative:
            return None

        # 嵌入
        embedding = None
        try:
            embedding = await create_embedding(narrative[:2000])
        except Exception as exc:
            logger.warning("scenario embedding failed: %s", exc)

        return MemoryScenario(
            id=uuid.uuid4(),
            user_id=user_id,
            project_key=project_key,
            title=title,
            narrative_md=narrative,
            atom_ids=atom_ids,
            session_ids=[uuid.UUID(s) for s in session_ids],
            period_start=period_start,
            period_end=period_end,
            embedding=embedding,
        )
