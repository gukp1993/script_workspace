"""test_kit——单测公共 fake 集合（TST-001 的 M0 部分）。"""

from test_kit.fakes import (
    FakeClock,
    FakeForegroundContext,
    FakeInputSink,
    InputBatch,
    InputIntent,
    make_batch,
    make_click_batch,
    make_fake_sink,
    make_intent,
)

__all__ = [
    "FakeClock",
    "FakeForegroundContext",
    "FakeInputSink",
    "InputBatch",
    "InputIntent",
    "make_batch",
    "make_click_batch",
    "make_fake_sink",
    "make_intent",
]
