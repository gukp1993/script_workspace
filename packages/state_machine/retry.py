"""有界重试与退避（FSM-008）。

- 退避基于注入 Clock（FakeClock 推进即可验证，无真实 sleep）；
- ``fixed``：固定退避；``exponential``：base * 2^(attempt-1)，封顶 max_seconds；
- 尝试次数达到上限即视为耗尽（exhausted），由运行时路由到 on_error_to
  状态——绝不无限重试；编译期 ``infinite_retry`` 规则保证 max_attempts
  必须存在且 >=1。
"""

from __future__ import annotations

from common.clock import Clock
from domain_model.dsl import BACKOFF_EXPONENTIAL, BACKOFF_FIXED, RetrySpec

__all__ = ["backoff_seconds", "RetryController"]


def backoff_seconds(spec: RetrySpec, attempt: int) -> float:
    """计算第 ``attempt`` 次失败后的退避秒数（不超过 max_seconds）。"""
    if spec.backoff == BACKOFF_EXPONENTIAL:
        value = spec.base_seconds * (2 ** max(0, attempt - 1))
    elif spec.backoff == BACKOFF_FIXED:
        value = spec.base_seconds
    else:  # pragma: no cover - 编译期已拒绝非法 backoff，防御
        value = spec.base_seconds
    return min(value, spec.max_seconds)


class RetryController:
    """单个状态驻留期的重试计数与退避调度。"""

    def __init__(self, spec: RetrySpec | None, clock: Clock) -> None:
        self.spec = spec
        self._clock = clock
        self.attempts: int = 0
        self.next_attempt_at: float = float("-inf")

    def reset(self) -> None:
        """清零计数与退避（进入新状态时由运行时重建控制器，无需手动调用）。"""
        self.attempts = 0
        self.next_attempt_at = float("-inf")

    @property
    def effective_max_attempts(self) -> int:
        """有效上限：spec 缺失或未配置上限时按 1 防御（绝不无限重试）。"""
        if self.spec is None or self.spec.max_attempts is None:
            return 1
        return self.spec.max_attempts

    @property
    def exhausted(self) -> bool:
        """尝试次数是否已达到上限。"""
        return self.attempts >= self.effective_max_attempts

    def ready(self) -> bool:
        """退避时间是否已到（可以再次尝试）。"""
        return self._clock.now() >= self.next_attempt_at

    def register_failure(self) -> float:
        """登记一次失败：计数 +1，按策略计算退避并返回秒数。"""
        self.attempts += 1
        wait = backoff_seconds(self.spec, self.attempts) if self.spec is not None else 0.0
        self.next_attempt_at = self._clock.now() + wait
        return wait
