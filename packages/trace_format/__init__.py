"""trace_format——运行轨迹事件、回放与差异分析（E09，TRC-001~008）。

- events：TraceEvent 与链式哈希（sha256(上一事件 hash + 本事件规范化 JSON)）；
- writer：JsonlTraceWriter 追加写 / JsonlTraceReader 最长可信前缀读取；
- verify：哈希链断链报告；
- frame_store：帧内容寻址存储与索引（TRC-003，隐私模式可关闭）；
- replay：固定感知回放（TRC-005）与原始帧回放（TRC-006）；
- diff：回放差异引擎与可读报告（TRC-007）；
- timeline：时间轴组合查询（TRC-008）。
"""

from trace_format.diff import (
    SEVERITY_ERROR,
    SEVERITY_NONE,
    SEVERITY_WARNING,
    FieldDiff,
    ReplayDiff,
    diff_perceptions,
    diff_replays,
)
from trace_format.events import (
    GENESIS_HASH,
    TRACE_FORMAT_VERSION,
    TraceEvent,
    TraceEventType,
    canonical_json,
    compute_event_hash,
)
from trace_format.frame_store import FrameStorageDisabled, FrameStore
from trace_format.replay import (
    FixedPerceptionReplayer,
    RawFrameReplayer,
    ReplayClock,
    ReplayIntent,
    ReplayResult,
    load_frame_refs,
    load_snapshots,
    perception_payload,
)
from trace_format.timeline import query_timeline
from trace_format.verify import verify
from trace_format.writer import JsonlTraceReader, JsonlTraceWriter

__all__ = [
    # TRC-001/002（既有）
    "GENESIS_HASH",
    "TRACE_FORMAT_VERSION",
    "JsonlTraceReader",
    "JsonlTraceWriter",
    "TraceEvent",
    "TraceEventType",
    "canonical_json",
    "compute_event_hash",
    "verify",
    # TRC-003 帧存储
    "FrameStore",
    "FrameStorageDisabled",
    # TRC-005/006 回放
    "FixedPerceptionReplayer",
    "RawFrameReplayer",
    "ReplayClock",
    "ReplayIntent",
    "ReplayResult",
    "load_snapshots",
    "load_frame_refs",
    "perception_payload",
    # TRC-007 差异
    "SEVERITY_NONE",
    "SEVERITY_WARNING",
    "SEVERITY_ERROR",
    "FieldDiff",
    "ReplayDiff",
    "diff_replays",
    "diff_perceptions",
    # TRC-008 时间轴
    "query_timeline",
]
