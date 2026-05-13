"""KnowledgeGraphService 的规则测试"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.models import Message
from src.services.knowledge_graph import KnowledgeGraphService


@pytest.mark.asyncio
async def test_skip_execution_plan_message() -> None:
    service = KnowledgeGraphService(AsyncMock())
    message = Message(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        role="assistant",
        content="**Execution Plan**\n\n## 1. Scope Lock: Separate Local Validation From External-Service Validation\n\nStart with these repository-local sources:",
        tokens=0,
        metadata_json={},
    )

    with patch("src.services.knowledge_graph.chat_completion", new=AsyncMock()) as mock_chat:
        result = await service.extract_knowledge_result(message)

    assert result["ok"] is True
    assert result["noop"] is True
    assert result["reason"] == "non_kg_analysis_message"
    mock_chat.assert_not_called()


@pytest.mark.asyncio
async def test_skip_chinese_analysis_message() -> None:
    service = KnowledgeGraphService(AsyncMock())
    message = Message(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        role="assistant",
        content="先快速扫描本地目录结构，定位包含 OpenCode 桌面端实现的仓库候选，然后只基于文件证据继续下钻入口与性能相关模块。",
        tokens=0,
        metadata_json={},
    )

    with patch("src.services.knowledge_graph.chat_completion", new=AsyncMock()) as mock_chat:
        result = await service.extract_knowledge_result(message)

    assert result["ok"] is True
    assert result["noop"] is True
    assert result["reason"] == "non_kg_analysis_message"
    mock_chat.assert_not_called()


@pytest.mark.asyncio
async def test_short_plan_word_message_still_skips_short_content() -> None:
    service = KnowledgeGraphService(AsyncMock())
    message = Message(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        role="assistant",
        content="Here is a short plan for the fix with one idea.",
        tokens=0,
        metadata_json={},
    )

    mock_chat = AsyncMock(return_value=('{"entities": [], "relations": []}', 0))
    with patch("src.services.knowledge_graph.chat_completion", new=mock_chat):
        result = await service.extract_knowledge_result(message)

    assert result["ok"] is True
    assert result["noop"] is True
    assert result["reason"] == "content_too_short"
    mock_chat.assert_not_called()


@pytest.mark.asyncio
async def test_non_skip_message_keeps_json_extract_failed() -> None:
    service = KnowledgeGraphService(AsyncMock())
    message = Message(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        role="assistant",
        content="The application failed after deployment because nginx.conf changed and app.py now depends on a missing SECRET_KEY environment variable in production. This is a concrete failure summary with entities and relations that should still go through KG extraction.",
        tokens=0,
        metadata_json={},
    )

    mock_chat = AsyncMock(return_value=("plain text without json", 0))
    with patch("src.services.knowledge_graph.chat_completion", new=mock_chat):
        result = await service.extract_knowledge_result(message)

    assert result["ok"] is False
    assert result["reason"] == "json_extract_failed"
    mock_chat.assert_awaited_once()
