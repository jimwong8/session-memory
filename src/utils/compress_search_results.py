"""搜索结果内容压缩 - A2 实现"""


def compress_search_result(content: str, max_length: int = 2048) -> dict:
    """
    压缩搜索结果中的单条消息内容
    
    策略：
    - 保留前 1.5KB + 后 0.5KB
    - 添加截断标记
    
    Args:
        content: 原始消息内容
        max_length: 最大长度限制（字节）
        
    Returns:
        {
            "content": 压缩后的内容,
            "truncated": bool,
            "original_size": int,
            "compressed_size": int
        }
    """
    original_size = len(content)
    
    if original_size <= max_length:
        return {
            "content": content,
            "truncated": False,
            "original_size": original_size,
            "compressed_size": original_size
        }
    
    # 保留前 75% + 后 25%
    keep_start = int(max_length * 0.75)
    keep_end = max_length - keep_start
    truncated_bytes = original_size - max_length
    
    compressed = (
        content[:keep_start] +
        f"\n\n... [搜索结果截断: {truncated_bytes} 字节省略] ...\n\n" +
        content[-keep_end:]
    )
    
    return {
        "content": compressed,
        "truncated": True,
        "original_size": original_size,
        "compressed_size": len(compressed)
    }


if __name__ == "__main__":
    # 测试
    short = "短消息"
    result = compress_search_result(short)
    assert not result["truncated"]
    assert result["content"] == short
    
    long = "x" * 5000
    result = compress_search_result(long)
    assert result["truncated"]
    assert result["compressed_size"] <= 2048 + 100
    assert "[搜索结果截断:" in result["content"]
    
    print("✅ 搜索结果压缩测试通过")
