"""金字塔 L0-L3 模型和 Schema 单元测试"""

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.schemas import (
    MemoryAtomCreate,
    MemoryAtomResponse,
    MemoryScenarioResponse,
    PersonaResponse,
    PersonaUpdateRequest,
    CanvasUpdateRequest,
    CanvasResponse,
)


class TestMemoryAtomCreate:
    """记忆原子创建 schema 验证"""

    def test_valid_atom(self) -> None:
        atom = MemoryAtomCreate(
            session_id=str(uuid.uuid4()),
            content="用户偏好简洁中文输出",
            kind="preference",
        )
        assert atom.kind == "preference"
        assert atom.content == "用户偏好简洁中文输出"
        assert atom.confidence_score == 0.8
        assert atom.sensitivity_level == "normal"

    def test_valid_atom_with_sources(self) -> None:
        msg_ids = [str(uuid.uuid4()) for _ in range(3)]
        atom = MemoryAtomCreate(
            session_id=str(uuid.uuid4()),
            content="修复了一个 collector 重试 bug",
            kind="decision",
            tags=["collector", "bugfix"],
            source_message_ids=msg_ids,
        )
        assert len(atom.source_message_ids) == 3
        assert "bugfix" in atom.tags

    def test_default_kind(self) -> None:
        atom = MemoryAtomCreate(
            session_id=str(uuid.uuid4()),
            content="测试事实",
        )
        assert atom.kind == "fact"

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MemoryAtomCreate(
                session_id=str(uuid.uuid4()),
                content="test",
                kind="invalid_kind_xyz",
            )

    def test_empty_content_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MemoryAtomCreate(
                session_id=str(uuid.uuid4()),
                content="",
            )


class TestMemoryAtomResponse:
    """记忆原子响应 schema 测试"""

    def test_from_attributes(self) -> None:
        now = datetime.now(timezone.utc)
        atom = MemoryAtomResponse(
            id=str(uuid.uuid4()),
            session_id=str(uuid.uuid4()),
            user_id="test-user",
            kind="fact",
            content="测试原子",
            source_message_ids=[str(uuid.uuid4())],
            created_at=now,
        )
        assert atom.user_id == "test-user"
        assert atom.kind == "fact"
        assert atom.created_at == now


class TestMemoryScenarioResponse:
    """场景块响应 schema 测试"""

    def test_valid_scenario(self) -> None:
        scenario = MemoryScenarioResponse(
            id=str(uuid.uuid4()),
            user_id="test-user",
            title="5月 cutover 演练总结",
            narrative_md="## 做了什么\n完成了 collector 安装和回归测试。",
            created_at=datetime.now(timezone.utc),
        )
        assert "cutover" in scenario.title
        assert len(scenario.atom_ids) == 0


class TestPersonaResponse:
    """用户画像响应 schema 测试"""

    def test_default_persona(self) -> None:
        now = datetime.now(timezone.utc)
        p = PersonaResponse(
            user_id="test-user",
            profile_md="",
            version=1,
            created_at=now,
            updated_at=now,
        )
        assert p.user_id == "test-user"
        assert p.version == 1
        assert p.preferences_json == {}

    def test_with_preferences(self) -> None:
        now = datetime.now(timezone.utc)
        p = PersonaResponse(
            user_id="dev",
            profile_md="喜欢简洁响应",
            preferences_json={"language": "zh", "verbosity": "concise"},
            version=3,
            created_at=now,
            updated_at=now,
        )
        assert p.preferences_json["language"] == "zh"
        assert p.version == 3


class TestCanvasSchema:
    """Mermaid 画布 schema 测试"""

    def test_canvas_update(self) -> None:
        req = CanvasUpdateRequest(canvas_mermaid="graph LR\n  A --> B")
        assert "graph LR" in req.canvas_mermaid

    def test_canvas_clear(self) -> None:
        req = CanvasUpdateRequest(canvas_mermaid=None)
        assert req.canvas_mermaid is None


class TestPersonaUpdate:
    """画像更新 schema 测试"""

    def test_partial_update(self) -> None:
        req = PersonaUpdateRequest(profile_md="新画像")
        assert req.profile_md == "新画像"
        assert req.preferences_json is None
