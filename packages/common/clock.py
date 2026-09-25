"""时钟抽象（FSM-005 的公共底座）。

- MonotonicClock：基于 time.perf_counter 的单调时钟，用于生产运行。
- FakeClock：可手动推进的时钟，单测无需真实等待即可验证超时/退避等定时行为。
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """所有运行时组件只依赖该协议，禁止直接调用 time.sleep 等待条件。"""

    def now(self) -> float:
        """返回单调时间（秒）。"""
        ...


class MonotonicClock:
    """生产用单调时钟。"""

    def now(self) -> float:
        return time.perf_counter()


class FakeClock:
    """可注入的确定性时钟：测试中用 advance() 无等待推进时间。

    记录每次 advance 调用，便于断言"定时行为可回放"（FSM-005）。
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self.advances: list[float] = []

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        """推进时钟。seconds 必须非负。"""
        if seconds < 0:
            raise ValueError("seconds 不能为负")
        self._now += seconds
        self.advances.append(seconds)
