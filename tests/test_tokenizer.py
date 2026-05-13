"""Token 计数和上下文构建的单元测试"""

from src.tokenizer import count_tokens, estimate_messages_tokens, truncate_to_tokens


class TestCountTokens:
    """token 计数测试"""

    def test_empty_string(self) -> None:
        assert count_tokens("") == 0

    def test_english_text(self) -> None:
        tokens = count_tokens("Hello, world!")
        assert tokens > 0

    def test_chinese_text(self) -> None:
        tokens = count_tokens("你好世界")
        assert tokens > 0

    def test_longer_text_more_tokens(self) -> None:
        short = count_tokens("Hi")
        long = count_tokens("Hello, this is a much longer sentence with many words.")
        assert long > short


class TestTruncateToTokens:
    """截断测试"""

    def test_short_text_unchanged(self) -> None:
        text = "Hello"
        result = truncate_to_tokens(text, 100)
        assert result == text

    def test_long_text_truncated(self) -> None:
        text = "Hello world " * 100
        result = truncate_to_tokens(text, 10)
        assert count_tokens(result) <= 10


class TestEstimateMessagesTokens:
    """消息列表 token 估算测试"""

    def test_single_message(self) -> None:
        messages = [{"role": "user", "content": "Hello"}]
        tokens = estimate_messages_tokens(messages)
        assert tokens > 0

    def test_multiple_messages(self) -> None:
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        tokens = estimate_messages_tokens(messages)
        single = estimate_messages_tokens([messages[0]])
        assert tokens > single

    def test_empty_messages(self) -> None:
        tokens = estimate_messages_tokens([])
        assert tokens == 2  # 只有回复开始标记
