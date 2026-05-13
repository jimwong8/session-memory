"""测试配置和共享 fixtures"""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def mock_cache() -> AsyncGenerator[AsyncMock, None]:
    """模拟 Redis 缓存"""
    mock = AsyncMock()
    mock.get_session_meta.return_value = None
    mock.get_recent_messages.return_value = []
    mock.get_summary.return_value = None
    mock.acquire_lock.return_value = True

    with patch("src.services.session_service.cache", mock), \
         patch("src.services.summary_service.cache", mock), \
         patch("src.services.context_builder.cache", mock):
        yield mock


@pytest_asyncio.fixture
async def mock_llm() -> AsyncGenerator[None, None]:
    """模拟 LLM 调用"""
    with patch("src.llm_client._get_client") as mock_client:
        client = AsyncMock()

        # 模拟 embeddings
        embedding_response = AsyncMock()
        embedding_data = AsyncMock()
        embedding_data.embedding = [0.1] * 1536
        embedding_response.data = [embedding_data]
        client.embeddings.create.return_value = embedding_response

        # 模拟 chat completion
        chat_response = AsyncMock()
        choice = AsyncMock()
        choice.message.content = "这是一个测试回复"
        chat_response.choices = [choice]
        usage = AsyncMock()
        usage.total_tokens = 150
        chat_response.usage = usage
        client.chat.completions.create.return_value = chat_response

        mock_client.return_value = client
        yield


@pytest.fixture
def sample_session_id() -> uuid.UUID:
    """示例会话 ID"""
    return uuid.uuid4()


@pytest.fixture
def sample_user_id() -> str:
    """示例用户 ID"""
    return "test-user-001"
