"""嵌入服务 - 支持本地和OpenAI两种方式"""

import logging
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

# 全局模型实例
_local_model: Optional[object] = None
_openai_client: Optional[object] = None


def _get_local_model():
    """获取本地嵌入模型(懒加载)"""
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer
        logger.info(f"加载本地嵌入模型: {settings.embedding_model}")
        _local_model = SentenceTransformer(settings.embedding_model)
        logger.info("本地嵌入模型加载完成")
    return _local_model


def _get_openai_client():
    """获取OpenAI客户端(懒加载)"""
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI
        _openai_client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
    return _openai_client


async def warmup_embedding() -> None:
    """启动时预热嵌入模型或客户端，避免首个 health 检查冷启动超时"""
    if settings.embedding_provider == "local":
        _get_local_model()
    elif settings.embedding_provider == "openai":
        _get_openai_client()


async def create_embedding(text: str) -> list[float]:
    """生成文本的向量嵌入"""
    if settings.embedding_provider == "local":
        return _create_local_embedding(text)
    elif settings.embedding_provider == "openai":
        return await _create_openai_embedding(text)
    else:
        raise ValueError(f"不支持的嵌入提供商: {settings.embedding_provider}")


async def create_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """批量生成向量嵌入"""
    if not texts:
        return []

    if settings.embedding_provider == "local":
        return _create_local_embeddings_batch(texts)
    elif settings.embedding_provider == "openai":
        return await _create_openai_embeddings_batch(texts)
    else:
        raise ValueError(f"不支持的嵌入提供商: {settings.embedding_provider}")


def _create_local_embedding(text: str) -> list[float]:
    """使用本地模型生成嵌入"""
    model = _get_local_model()
    embedding = model.encode(text, convert_to_numpy=True)
    return embedding.tolist()


def _create_local_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """使用本地模型批量生成嵌入"""
    model = _get_local_model()
    embeddings = model.encode(texts, convert_to_numpy=True)
    return [emb.tolist() for emb in embeddings]


async def _create_openai_embedding(text: str) -> list[float]:
    """使用OpenAI API生成嵌入"""
    client = _get_openai_client()
    response = await client.embeddings.create(
        model=settings.embedding_model,
        input=text,
        dimensions=settings.embedding_dimensions,
    )
    return response.data[0].embedding


async def _create_openai_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """使用OpenAI API批量生成嵌入"""
    client = _get_openai_client()
    response = await client.embeddings.create(
        model=settings.embedding_model,
        input=texts,
        dimensions=settings.embedding_dimensions,
    )
    return [item.embedding for item in response.data]
