"""用户画像（L3）构建服务 - 从原子和场景提炼用户画像"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.llm_client import chat_completion
from src.models import MemoryAtom, MemoryScenario, Persona, PersonaHistory

logger = logging.getLogger(__name__)

_PERSONA_PROMPT = """你是一个用户画像构建助手。请根据以下用户的记忆原子和场景，构建一个简洁的用户画像。

输出 JSON 格式：
{
  "profile_md": "一段 markdown 描述此用户的偏好、工作方式、常见模式、常用技术栈等",
  "preferences": {
    "language": "zh/en 等用户常用语言",
    "communication_style": "简洁/详细/技术/文档化 等",
    "work_focus": "后端/前端/运维/全栈/数据 等",
    "tool_preferences": "用户偏好的工具或框架列表",
    "common_patterns": "用户工作中反复出现的模式"
  }
}

要求：
1. 只基于提供的原子和场景，不做推测
2. profile_md 在 200-500 字之间
3. preferences 的具体字段名称可以灵活
4. 如果信息不足以推断某个字段，设为 null

只输出 JSON，不要额外内容。
"""


class PersonaBuilder:
    """从 L1/L2 构建 L3 用户画像"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_for_user(self, user_id: str) -> bool:
        """为指定用户构建/更新画像"""
        # 获取最近 atoms 和 scenarios
        atoms = await self._fetch_recent_atoms(user_id)
        scenarios = await self._fetch_recent_scenarios(user_id)

        total_items = len(atoms) + len(scenarios)
        if total_items < settings.persona_trigger_every_n_atoms:
            logger.debug(
                "[%s] not enough material: %d items < %d trigger",
                user_id, total_items, settings.persona_trigger_every_n_atoms,
            )
            return False

        # 构建 LLM 输入
        material = self._build_material(atoms, scenarios)

        try:
            reply, _ = await chat_completion(
                messages=[
                    {"role": "system", "content": _PERSONA_PROMPT},
                    {"role": "user", "content": f"请根据以下材料构建用户画像：\n\n{material}"},
                ],
                temperature=0.4,
                max_tokens=4096,
            )
        except Exception as exc:
            logger.error("[%s] LLM persona build failed: %s", user_id, exc)
            return False

        data = self._parse_response(reply)
        if not data:
            return False

        profile_md = data.get("profile_md", "")
        preferences = data.get("preferences", {})

        # 获取或创建 persona
        stmt = select(Persona).where(Persona.user_id == user_id)
        result = await self.db.execute(stmt)
        persona = result.scalar_one_or_none()

        scenario_ids = [s.id for s in scenarios[:50]]
        now = datetime.now(timezone.utc)

        if persona:
            # 保存旧版本
            history = PersonaHistory(
                id=uuid.uuid4(),
                user_id=user_id,
                version=persona.version,
                profile_md=persona.profile_md,
                preferences_json=persona.preferences_json,
            )
            self.db.add(history)

            # 更新
            persona.profile_md = profile_md
            persona.preferences_json = preferences
            persona.scenario_ids = scenario_ids
            persona.version += 1
            persona.updated_at = now
        else:
            persona = Persona(
                user_id=user_id,
                profile_md=profile_md,
                preferences_json=preferences,
                scenario_ids=scenario_ids,
                version=1,
                created_at=now,
                updated_at=now,
            )
            self.db.add(persona)

        await self.db.commit()
        logger.info("[%s] persona v%d built (%d atoms + %d scenarios)",
                     user_id, persona.version, len(atoms), len(scenarios))
        return True

    async def _fetch_recent_atoms(self, user_id: str, limit: int = 200) -> list[MemoryAtom]:
        stmt = (
            select(MemoryAtom)
            .where(
                MemoryAtom.user_id == user_id,
                MemoryAtom.superseded_by.is_(None),
            )
            .order_by(MemoryAtom.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _fetch_recent_scenarios(self, user_id: str, limit: int = 50) -> list[MemoryScenario]:
        stmt = (
            select(MemoryScenario)
            .where(MemoryScenario.user_id == user_id)
            .order_by(MemoryScenario.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    def _build_material(self, atoms: list[MemoryAtom], scenarios: list[MemoryScenario]) -> str:
        parts = [f"## 记忆原子（共 {len(atoms)} 条）"]
        for a in atoms[:100]:
            parts.append(f"- [{a.kind}] {a.content[:300]}")
        if scenarios:
            parts.append(f"\n## 场景（共 {len(scenarios)} 条）")
            for s in scenarios[:20]:
                parts.append(f"- {s.title}: {s.narrative_md[:200]}")
        return "\n".join(parts)

    def _parse_response(self, reply: str) -> dict | None:
        reply = reply.strip()
        if reply.startswith("```"):
            lines = reply.split("\n")
            reply = "\n".join(lines[1:-1]) if len(lines) >= 3 else reply
        try:
            data = json.loads(reply)
        except json.JSONDecodeError:
            start = reply.find("{")
            end = reply.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(reply[start:end+1])
                except (json.JSONDecodeError, ValueError):
                    logger.error("persona JSON parse failed: %s...", reply[:200])
                    return None
            else:
                logger.error("persona JSON parse failed: %s...", reply[:200])
                return None
        if not isinstance(data, dict) or "profile_md" not in data:
            return None
        return data
