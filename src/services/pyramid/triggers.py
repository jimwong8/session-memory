"""金字塔触发策略 - 决定何时抽取记忆"""

import logging
from dataclasses import dataclass

from src.config import settings

logger = logging.getLogger(__name__)


@dataclass
class TriggerDecision:
    """触发决策结果"""
    should_run: bool
    reason: str = ""
    capped_pass_count: int = 5  # warmup 后实际应处理的消息数


class TriggerEvaluator:
    """评估是否应该触发记忆抽取"""

    def __init__(self, session_message_count: int, existing_atom_count: int):
        self.session_message_count = session_message_count
        self.existing_atom_count = existing_atom_count

    def evaluate(self) -> TriggerDecision:
        """综合判断是否触发抽取"""
        if not settings.pyramid_enabled:
            return TriggerDecision(should_run=False, reason="pyramid disabled")

        every_n = settings.pipeline_every_n_conversations
        threshold = self._compute_threshold()

        # 检查是否达到触发阈值
        expected_messages = threshold * every_n
        if self.session_message_count < expected_messages:
            reason = (
                f"not enough messages: have {self.session_message_count}, "
                f"need {expected_messages} (threshold={threshold} × every_n={every_n})"
            )
            return TriggerDecision(should_run=False, reason=reason)

        # 计算本轮可处理的消息数（从 last_atom 到当前）
        processed_msgs = self.existing_atom_count * every_n
        unprocessed = self.session_message_count - processed_msgs

        if unprocessed < every_n:
            reason = f"unprocessed={unprocessed} < every_n={every_n}, skip"
            return TriggerDecision(should_run=False, reason=reason)

        capped = min(unprocessed, self.existing_atom_count * 2 + every_n)
        return TriggerDecision(
            should_run=True,
            reason=f"triggered: unprocessed={unprocessed}, capped={capped}",
            capped_pass_count=capped,
        )

    def _compute_threshold(self) -> int:
        """Warmup: 从 1→2→4→8→16 指数增长直到稳定"""
        if not settings.pipeline_enable_warmup:
            return 1
        # 用已有 atom 数决定 warmup 阶段
        stage = self.existing_atom_count + 1
        threshold = 1
        for _ in range(stage):
            threshold = min(threshold * 2, 16)
        return threshold
