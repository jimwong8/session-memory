"""消息内容压缩工具 - A1 实现"""

import re
from typing import Tuple


def compress_message_content(content: str, max_length: int = 10240) -> dict:
    """
    压缩消息内容，返回包含压缩后内容和统计信息的字典
    
    策略：
    1. 工具输出截断（保留前2KB+后1KB）
    2. 代码块压缩（保留签名+前10行+后5行）
    3. 简单截断（超过10KB直接截断）
    
    Args:
        content: 原始消息内容
        max_length: 最大长度限制（字节）
        
    Returns:
        {
            "content": 压缩后的内容,
            "stats": {
                "compressed": bool,
                "original_size": int,
                "compressed_size": int,
                "compression_ratio": float,
                "strategy": str
            }
        }
    """
    original_length = len(content)
    
    # 如果内容本身就不超长，直接返回
    if original_length <= max_length:
        return {
            "content": content,
            "stats": {
                "compressed": False,
                "original_size": original_length,
                "compressed_size": original_length,
                "compression_ratio": 1.0,
                "strategy": "none"
            }
        }
    
    # 策略1: 检测工具输出模式（<function_results>...</function_results>）
    tool_output_pattern = r'<function_results>(.*?)</function_results>'
    tool_matches = list(re.finditer(tool_output_pattern, content, re.DOTALL))
    
    if tool_matches:
        compressed = content
        compressed_any = False
        for match in tool_matches:
            tool_content = match.group(1)
            if len(tool_content) > 3072:  # 3KB阈值
                # 保留前2KB + 后1KB
                omitted_bytes = len(tool_content) - 3072
                compressed_tool = (
                    tool_content[:2048] + 
                    "\n\n... [compressed: {} bytes omitted] ...\n\n".format(omitted_bytes) +
                    tool_content[-1024:]
                )
                compressed = compressed.replace(match.group(0), f'<function_results>{compressed_tool}</function_results>')
                compressed_any = True
        
        if compressed_any and len(compressed) <= max_length:
            return compressed, {
                "original_length": original_length,
                "compressed_length": len(compressed),
                "compression_ratio": len(compressed) / original_length,
                "method": "tool_output_truncation"
            }
        if compressed_any:
            content = compressed  # 继续下一步压缩
    
    # 策略2: 代码块压缩（```...```）
    code_block_pattern = r'```[\w]*\n(.*?)\n```'
    code_matches = list(re.finditer(code_block_pattern, content, re.DOTALL))
    
    if code_matches:
        compressed = content
        compressed_any = False
        for match in code_matches:
            code_content = match.group(1)
            lines = code_content.split('\n')
            if len(lines) > 15:  # 超过15行才压缩
                # 保留前10行 + 后5行
                compressed_code = '\n'.join(lines[:10]) + \
                    f"\n\n... [compressed: {len(lines) - 15} lines omitted] ...\n\n" + \
                    '\n'.join(lines[-5:])
                lang = match.group(0).split('\n')[0].replace('```', '')
                compressed = compressed.replace(match.group(0), f'```{lang}\n{compressed_code}\n```')
                compressed_any = True
        
        if compressed_any and len(compressed) <= max_length:
            return compressed, {
                "original_length": original_length,
                "compressed_length": len(compressed),
                "compression_ratio": len(compressed) / original_length,
                "method": "code_block_compression"
            }
        if compressed_any:
            content = compressed  # 继续下一步压缩
    
    # 策略3: 简单截断（保留前80% + 后20%）
    if len(content) > max_length:
        # 为截断标记预留空间（约 50 字节）
        marker_allowance = 100
        safe_max_length = max_length - marker_allowance
        
        keep_start = int(safe_max_length * 0.8)
        keep_end = safe_max_length - keep_start
        truncated_bytes = len(content) - safe_max_length
        compressed = (
            content[:keep_start] +
            f"\n\n... [truncated: {truncated_bytes} bytes omitted] ...\n\n" +
            content[-keep_end:]
        )
        return {
            "content": compressed,
            "stats": {
                "compressed": True,
                "original_size": original_length,
                "compressed_size": len(compressed),
                "compression_ratio": len(compressed) / original_length,
                "strategy": "simple_truncation"
            }
        }


# 测试用例
if __name__ == "__main__":
    # 测试1: 短内容
    short = "Hello, world!"
    result, stats = compress_message_content(short)
    print(f"Test 1 - Short content: {stats}")
    assert result == short
    
    # 测试2: 工具输出（超过10KB才会触发压缩）
    tool_output = "<function_results>" + "x" * 15000 + "</function_results>"
    result, stats = compress_message_content(tool_output)
    print(f"Test 2 - Tool output: {stats}")
    assert len(result) < len(tool_output)
    assert "[compressed:" in result
    assert stats["method"] == "tool_output_truncation"
    
    # 测试3: 代码块（需要超过10KB才触发压缩）
    code_block = "Some context\n" + "```python\n" + "\n".join([f"line {i} with some padding text to make it longer" for i in range(300)]) + "\n```\n" + "x" * 5000
    result, stats = compress_message_content(code_block)
    print(f"Test 3 - Code block: {stats}")
    assert len(result) < len(code_block)
    assert "[compressed:" in result
    
    # 测试4: 超长纯文本
    long_text = "a" * 20000
    result, stats = compress_message_content(long_text)
    print(f"Test 4 - Long text: {stats}")
    assert len(result) <= 10240 + 100  # 允许一点误差
    assert "[truncated:" in result
    
    print("\n✅ All tests passed!")
