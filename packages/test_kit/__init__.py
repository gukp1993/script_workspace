"""test_kit——单测公共 fake 集合（TST-001：M0 基础 + M1 整合单一入口）。

约定：本包只 re-export / 组合各包已有 fake，不新增功能、不改被测包。
"""

from capture_api.fake import FakeCaptureSource  # noqa: F401  (re-export)
from common import FakeClock  # noqa: F401  (re-export)
from window_service.models import SessionBinding, WindowInfo  # noqa: F401  (re-export)

from test_kit.fakes import (
    FakeForegroundContext,
    FakeInputSink,
    FakeWindowSource,
    InputBatch,
    InputIntent,
    make_batch,
    make_click_batch,
    make_fake_sink,
    make_frame,
    make_intent,
    make_session_binding,
)

__all__ = [
    "FakeCaptureSource",
    "FakeClock",
    "FakeForegroundContext",
    "FakeInputSink",
    "FakeWindowSource",
    "InputBatch",
    "InputIntent",
    "SessionBinding",
    "WindowInfo",
    "make_batch",
    "make_click_batch",
    "make_fake_sink",
    "make_frame",
    "make_intent",
    "make_session_binding",
]
