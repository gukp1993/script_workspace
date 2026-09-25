"""trace_format——运行轨迹事件与版本化格式（E09，TRC-001/002 最小版）。

- events：TraceEvent 与链式哈希（sha256(上一事件 hash + 本事件规范化 JSON)）；
- writer：JsonlTraceWriter 追加写 / JsonlTraceReader 最长可信前缀读取；
- verify：哈希链断链报告。
"""

from trace_format.events import (
    GENESIS_HASH,
    TRACE_FORMAT_VERSION,
    TraceEvent,
    TraceEventType,
    canonical_json,
    compute_event_hash,
)
from trace_format.verify import verify
from trace_format.writer import JsonlTraceReader, JsonlTraceWriter

__all__ = [
    "GENESIS_HASH",
    "TRACE_FORMAT_VERSION",
    "JsonlTraceReader",
    "JsonlTraceWriter",
    "TraceEvent",
    "TraceEventType",
    "canonical_json",
    "compute_event_hash",
    "verify",
]
