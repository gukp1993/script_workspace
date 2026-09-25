"""状态机定时器（FSM-005）：可注入 Clock，FakeClock 推进即可触发。

- 状态进入时 :meth:`StateTimer.arm` 装载该状态的 timeout_seconds；
- :attr:`StateTimer.expired` 判定是否到达超时（elapsed >= timeout）；
- 全程不调用 time.sleep——单测用 FakeClock.advance 无等待推进。
"""

from __future__ import annotations

from common.clock import Clock

__all__ = ["StateTimer"]


class StateTimer:
    """当前状态的驻留计时器（基于注入 Clock 的单调时间）。"""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._armed_at: float | None = None
        self._timeout: float | None = None

    def arm(self, timeout_seconds: float | None) -> None:
        """装载超时并记录当前时刻；``timeout_seconds=None`` 表示不设超时。"""
        self._armed_at = self._clock.now()
        self._timeout = timeout_seconds

    def disarm(self) -> None:
        """拆除计时器。"""
        self._armed_at = None
        self._timeout = None

    @property
    def armed(self) -> bool:
        """是否已装载。"""
        return self._armed_at is not None

    def elapsed(self) -> float:
        """当前状态驻留秒数（未装载时为 0）。"""
        if self._armed_at is None:
            return 0.0
        return self._clock.now() - self._armed_at

    @property
    def expired(self) -> bool:
        """是否已超时（elapsed >= timeout）。"""
        if self._armed_at is None or self._timeout is None:
            return False
        return self.elapsed() >= self._timeout
