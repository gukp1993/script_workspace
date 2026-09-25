"""时间轴查询 API（TRC-008）。

:func:`query_timeline` 在事件序列上做组合过滤，供 UI 时间轴与影响分析：

- ``since`` / ``until``：按 ``ts_monotonic`` 的闭区间过滤（含边界）；
- ``types``：事件类型（单个或集合，接受 ``TraceEventType`` 或其值字符串）；
- ``state``：state_transition 事件的 ``from`` / ``to`` 任一匹配；
- ``detector_id``：perception_snapshot 事件 ``payload.fields`` 的键包含
  该 ID（引擎写事件时 fields 键即检测器输出语义名）；
- ``policy_denied_only``：policy_decision 事件且
  ``payload.allowed is False``；
- ``anomalies_only``：仅 ``anomaly`` 类型事件（estop 请用 ``types``）；
- ``limit``：过滤后截取前 N 条（负数抛 ``ValueError``）。

所有过滤条件按 AND 组合；返回列表保持输入顺序（时间轴语义）。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from trace_format.events import TraceEvent, TraceEventType

__all__ = ["query_timeline"]


def _normalize_types(types: object) -> frozenset[str] | None:
    """把 types 参数归一为事件类型值字符串集合（None 表示不过滤）。"""
    if types is None:
        return None
    if isinstance(types, TraceEventType):
        return frozenset({types.value})
    if isinstance(types, str):
        return frozenset({types})
    if isinstance(types, Iterable):
        return frozenset(
            str(getattr(item, "value", item)) for item in types  # type: ignore[arg-type]
        )
    raise TypeError(f"types 只允许 str/TraceEventType/可迭代集合，得到 {type(types).__name__}")


def query_timeline(
    events: Sequence[TraceEvent],
    *,
    since: float | None = None,
    until: float | None = None,
    types: str | TraceEventType | Iterable[str | TraceEventType] | None = None,
    state: str | None = None,
    detector_id: str | None = None,
    policy_denied_only: bool = False,
    anomalies_only: bool = False,
    limit: int | None = None,
) -> list[TraceEvent]:
    """按时间/类型/状态/检测器/策略拒绝/异常过滤事件序列（保持顺序）。

    Raises:
        ValueError: ``limit`` 为负。
        TypeError:  ``types`` 类型不支持。
    """
    if limit is not None and limit < 0:
        raise ValueError(f"limit 不能为负，得到 {limit!r}")
    if limit == 0:
        return []
    type_filter = _normalize_types(types)
    out: list[TraceEvent] = []
    for event in events:
        ts = float(event.ts_monotonic)
        if since is not None and ts < float(since):
            continue
        if until is not None and ts > float(until):
            continue
        if type_filter is not None and event.type not in type_filter:
            continue
        payload = event.payload
        if state is not None:
            if event.type != TraceEventType.STATE_TRANSITION.value:
                continue
            if payload.get("from") != state and payload.get("to") != state:
                continue
        if detector_id is not None:
            if event.type != TraceEventType.PERCEPTION_SNAPSHOT.value:
                continue
            fields = payload.get("fields")
            if not isinstance(fields, dict) or detector_id not in fields:
                continue
        if policy_denied_only:
            if event.type != TraceEventType.POLICY_DECISION.value:
                continue
            if payload.get("allowed") is not False:
                continue
        if anomalies_only and event.type != TraceEventType.ANOMALY.value:
            continue
        out.append(event)
        if limit is not None and len(out) >= limit:
            break
    return out
