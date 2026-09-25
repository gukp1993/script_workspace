"""common——全仓公共基础包（ENG-002/ENG-005）。

提供结构化日志与关联 ID、单调时钟与可注入 FakeClock、ID 生成。
所有包（domain_model / policy_engine / trace_format / input_broker /
arena_lab …）都应复用本包，避免各处自造时间与日志设施。
"""

from common.clock import Clock, FakeClock, MonotonicClock
from common.ids import new_correlation_id, new_id, new_session_id
from common.logging import (
    configure_json_logging,
    correlation_scope,
    get_correlation_id,
    get_logger,
    log_event,
)

__all__ = [
    "Clock",
    "FakeClock",
    "MonotonicClock",
    "new_correlation_id",
    "new_id",
    "new_session_id",
    "configure_json_logging",
    "correlation_scope",
    "get_correlation_id",
    "get_logger",
    "log_event",
]
