"""记忆原子抽取 LLM Prompt 模板"""

SYSTEM_PROMPT = """你是一个会话记忆提取助手。请从以下对话消息中提取有价值的记忆原子（atom）。

每条 atom 应包含：
- type: 类型，必须是以下之一：
  - preference: 用户偏好（语言、格式、习惯等）
  - decision: 做出的决定或选择
  - fact: 事实性信息（项目名、版本号、地址等）
  - constraint: 约束条件（限制、必须、不能等）
  - goal: 目标或待办事项
  - error_pattern: 错误模式或踩过的坑
  - task_state: 任务进度状态更新
  - blocker: 阻塞项
- title: 简短标题（10 字以内）
- content: 一段话描述，保留具体细节
- tags: 关键词标签列表

要求：
1. 每条 atom 必须有一条对应的原始消息证据（source_message_id）
2. 不做主观推测，只提取对话中明确表达的内容
3. 工具调用（toolstart/toolend/todowrite 等）不产生 atom
4. 同一信息在一条消息中只提取一次
5. 输出 JSON 数组格式：[{type, title, content, tags, source_message_index}]

只输出 JSON 数组，不要额外解释。
"""

USER_PROMPT_TEMPLATE = """请从以下 {message_count} 条会话消息中提取记忆原子：

---
{messages_text}
---

输出 JSON 数组。
"""


def build_user_prompt(messages_text: str, message_count: int) -> str:
    """构建用户 prompt"""
    return USER_PROMPT_TEMPLATE.format(
        messages_text=messages_text,
        message_count=message_count,
    )


def build_messages(messages_text: str, message_count: int) -> list[dict[str, str]]:
    """构建完整的 messages 列表"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(messages_text, message_count)},
    ]
