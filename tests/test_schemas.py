"""Pydantic Schema 验证测试"""

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.schemas import (
    ChatRequest,
    ContextWindow,
    MessageCreate,
    MessageResponse,
    SessionCreate,
    SessionResponse,
)


class TestMessageCreate:
    """消息创建 schema 验证"""

    def test_valid_user_message(self) -> None:
        msg = MessageCreate(role="user", content="Hello!")
        assert msg.role == "user"
        assert msg.content == "Hello!"

    def test_valid_assistant_message(self) -> None:
        msg = MessageCreate(role="assistant", content="Hi there!")
        assert msg.role == "assistant"

    def test_valid_system_message(self) -> None:
        msg = MessageCreate(role="system", content="You are helpful.")
        assert msg.role == "system"

    def test_invalid_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MessageCreate(role="invalid", content="test")

    def test_empty_content_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MessageCreate(role="user", content="")


class TestSessionCreate:
    """会话创建 schema 验证"""

    def test_valid_session(self) -> None:
        session = SessionCreate(user_id="user-001", title="Test Session")
        assert session.user_id == "user-001"
        assert session.title == "Test Session"

    def test_empty_user_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SessionCreate(user_id="")

    def test_optional_fields(self) -> None:
        session = SessionCreate(user_id="user-001")
        assert session.title is None
        assert session.metadata_json is None


class TestChatRequest:
    """聊天请求 schema 验证"""

    def test_valid_request(self) -> None:
        req = ChatRequest(message="What is Python?")
        assert req.message == "What is Python?"
        assert req.system_prompt is None

    def test_with_system_prompt(self) -> None:
        req = ChatRequest(message="Hello", system_prompt="Be concise.")
        assert req.system_prompt == "Be concise."

    def test_empty_message_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatRequest(message="")


class TestContextWindow:
    """上下文窗口 schema 测试"""

    def test_empty_context(self) -> None:
        ctx = ContextWindow()
        assert ctx.system_prompt is None
        assert ctx.summary is None
        assert ctx.retrieved_messages == []
        assert ctx.recent_messages == []
        assert ctx.total_tokens == 0

    def test_full_context(self) -> None:
        msg = MessageResponse(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            role="user",
            content="test",
            tokens=5,
            created_at=datetime.now(timezone.utc),
        )
        ctx = ContextWindow(
            system_prompt="Be helpful",
            summary="Previous conversation about Python",
            recent_messages=[msg],
            total_tokens=100,
        )
        assert ctx.system_prompt == "Be helpful"
        assert len(ctx.recent_messages) == 1
        assert ctx.total_tokens == 100
