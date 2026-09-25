"""test_kit——测试套件公共 fake（TST-001 的 M0 部分）。

目标：单测无需真实桌面即可模拟时间、前台焦点与输入结果。
- FakeClock：来自 common 包的确定性时钟（原样 re-export）；
- FakeForegroundContext：可编程前台上下文，模拟聚焦/失焦/错窗/换绑；
- make_fake_sink / make_click_batch：FakeInputSink 与意图批次的便捷构造。
"""

from typing import Any

from common import FakeClock  # noqa: F401  (re-export)

from input_broker import (  # noqa: F401  (re-export)
    FakeInputSink,
    InputBatch,
    InputIntent,
    make_batch,
    make_intent,
)
from policy_engine import ForegroundContext


class FakeForegroundContext(ForegroundContext):
    """可编程前台上下文：用方法切换前台状态，模拟失焦/错窗/进程换绑。"""

    def focus(
        self, target_id: str, *, pid: int | None = None, hwnd: int | None = None
    ) -> None:
        """模拟某个目标窗口获得前台焦点。"""
        self.target_id = target_id
        self.pid = pid
        self.hwnd = hwnd

    def blur(self) -> None:
        """模拟失焦：前台无目标（evaluator 应拒绝任何真实输入）。"""
        self.target_id = None
        self.pid = None
        self.hwnd = None

    def switch_to(self, target_id: str, *, pid: int | None = None) -> None:
        """模拟切到（可能错误的）其他窗口。"""
        self.focus(target_id, pid=pid)

    def bind(
        self,
        *,
        target_id: str | None = None,
        pid: int | None = None,
        session_id: str | None = None,
    ) -> None:
        """设置会话绑定的期望值（evaluator 逐项比对）。"""
        if target_id is not None:
            self.expected_target_id = target_id
        if pid is not None:
            self.expected_pid = pid
        if session_id is not None:
            self.expected_session_id = session_id


def make_fake_sink(**kwargs: Any) -> FakeInputSink:
    """FakeInputSink 便捷构造（参数原样透传）。"""
    return FakeInputSink(**kwargs)


def make_click_batch(
    session_id: str,
    target_id: str,
    count: int = 1,
    *,
    clock: Any,
    x: int = 0,
    y: int = 0,
    ttl_ms: float = 500.0,
    batch_ttl_ms: float = 1000.0,
    cause: str = "test",
) -> InputBatch:
    """构造包含 count 个 click 意图的批次（测试常用捷径）。"""
    intents = [
        make_intent(
            session_id,
            target_id,
            "click",
            {"button": "left", "x": x, "y": y},
            clock=clock,
            ttl_ms=ttl_ms,
            cause=cause,
        )
        for _ in range(count)
    ]
    return make_batch(
        session_id, target_id, intents, clock=clock, ttl_ms=batch_ttl_ms, cause=cause
    )
