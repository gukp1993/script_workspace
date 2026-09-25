"""运行预算（POL-004/005 最小版）。

M0 覆盖三项预算（全部基于注入 Clock，可确定性测试）：
- 每分钟动作数（固定 60s 窗口，wait 不计数）；
- 会话总动作数；
- 运行时长硬上限（start_monotonic + max_runtime_minutes）。

到限即拒绝并给出原因；"脚本不能提高策略上限"由 BudgetTracker
只读策略字段保证（限制来自 PolicyInput，不接受运行期修改）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from common.clock import Clock, MonotonicClock

from policy_engine.models import PolicyInput, WAIT_KIND

#: 每分钟窗口长度（秒）。
MINUTE_SECONDS: float = 60.0


@dataclass(frozen=True)
class BudgetState:
    """预算快照（用于审计与测试断言）。"""

    minute_window_start: float
    actions_this_minute: int
    total_actions: int
    start_monotonic: float
    max_actions_per_minute: int
    max_total_actions: int
    max_runtime_minutes: float


class BudgetTracker:
    """会话预算跟踪器：限制只来自 PolicyInput，运行期不可上调。"""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        policy: PolicyInput | None = None,
        max_actions_per_minute: int | None = None,
        max_total_actions: int | None = None,
        max_runtime_minutes: float | None = None,
        start_monotonic: float | None = None,
    ) -> None:
        self._clock: Clock = clock if clock is not None else MonotonicClock()
        policy = policy if policy is not None else PolicyInput()
        self._max_per_minute = int(
            max_actions_per_minute
            if max_actions_per_minute is not None
            else policy.max_actions_per_minute
        )
        self._max_total = int(
            max_total_actions
            if max_total_actions is not None
            else policy.max_total_actions
        )
        self._max_runtime_minutes = float(
            max_runtime_minutes
            if max_runtime_minutes is not None
            else policy.max_runtime_minutes
        )
        self._start = float(
            start_monotonic if start_monotonic is not None else self._clock.now()
        )
        self._window_start = self._start
        self._minute_actions = 0
        self._total_actions = 0

    # ------------------------------------------------------------------ 查询
    def is_runtime_exceeded(self, *, now: float | None = None) -> bool:
        """运行时长是否达到硬上限（达到即超，进入不可自动恢复停止态）。"""
        t = self._now_or_clock(now)
        return (t - self._start) >= self._max_runtime_minutes * MINUTE_SECONDS

    def state(self) -> BudgetState:
        """当前预算快照。"""
        return BudgetState(
            minute_window_start=self._window_start,
            actions_this_minute=self._minute_actions,
            total_actions=self._total_actions,
            start_monotonic=self._start,
            max_actions_per_minute=self._max_per_minute,
            max_total_actions=self._max_total,
            max_runtime_minutes=self._max_runtime_minutes,
        )

    # ------------------------------------------------------------------ 校验
    def preview_reasons(
        self, action_count: int = 1, *, now: float | None = None
    ) -> list[str]:
        """预检 action_count 个动作是否会超预算；返回机器可读原因（空 = 通过）。"""
        t = self._now_or_clock(now)
        self._roll_window(t)
        reasons: list[str] = []
        if self.is_runtime_exceeded(now=t):
            reasons.append("runtime_limit_reached")
        if self._minute_actions + action_count > self._max_per_minute:
            reasons.append("budget_exhausted:actions_per_minute")
        if self._total_actions + action_count > self._max_total:
            reasons.append("budget_exhausted:total_actions")
        return reasons

    def consume(
        self, action_count: int = 1, *, now: float | None = None
    ) -> tuple[bool, str | None]:
        """扣减预算；超限返回 (False, 原因) 且不产生任何计数变化。"""
        reasons = self.preview_reasons(action_count, now=now)
        if reasons:
            return False, reasons[0]
        t = self._now_or_clock(now)
        self._roll_window(t)
        self._minute_actions += action_count
        self._total_actions += action_count
        return True, None

    def consume_intent(
        self, intent: Any, *, now: float | None = None
    ) -> tuple[bool, str | None]:
        """按意图扣减预算：wait 不消耗动作数预算。"""
        if getattr(intent, "kind", "") == WAIT_KIND:
            return True, None
        return self.consume(1, now=now)

    # ------------------------------------------------------------------ 内部
    def _now_or_clock(self, now: float | None) -> float:
        return float(now) if now is not None else float(self._clock.now())

    def _roll_window(self, now: float) -> None:
        """固定 60s 窗口滚动：跨窗即清零本窗计数（窗口锚定启动时刻）。"""
        if not math.isfinite(now):
            return
        elapsed = now - self._window_start
        if elapsed >= MINUTE_SECONDS:
            self._window_start += math.floor(elapsed / MINUTE_SECONDS) * MINUTE_SECONDS
            self._minute_actions = 0
