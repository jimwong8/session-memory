"""Token 计数和文本处理工具"""

import tiktoken

from src.config import settings

_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    """获取 tiktoken 编码器（懒加载单例）"""
    global _encoder
    if _encoder is None:
        try:
            _encoder = tiktoken.encoding_for_model(settings.openai_model)
        except KeyError:
            _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str) -> int:
    """计算文本的 token 数量"""
    return len(_get_encoder().encode(text))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """将文本截断到指定 token 数"""
    encoder = _get_encoder()
    tokens = encoder.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoder.decode(tokens[:max_tokens])


def estimate_messages_tokens(messages: list[dict[str, str]]) -> int:
    """估算消息列表的 token 总数（含格式开销）"""
    total = 0
    for msg in messages:
        total += 4  # 每条消息的格式开销
        total += count_tokens(msg.get("role", ""))
        total += count_tokens(msg.get("content", ""))
    total += 2  # 回复开始标记
    return total
