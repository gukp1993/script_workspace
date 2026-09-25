"""轨迹回放：固定感知回放（TRC-005）与原始帧回放（TRC-006）。

事件 payload 契约（与运行时引擎约定，双方一致）：

- ``perception_snapshot``：
  ``{"fields": {"<字段名>": {"present": bool, "confidence": float,
  "value": float|str|null}}, "frame_ref": "<sha256>|null", "ts": float}``
- ``state_transition``：``{"from": str, "to": str, "decision_hash": str}``
- ``intent_issued``：
  ``{"batch_id": str, "intents": [{"kind": str, "payload": {...}, "cause": str}]}``
- ``executed``：``{"batch_id": str, "accepted": int, "rejected": int, "real": bool}``

回放语义：

- :func:`load_snapshots` 从 perception_snapshot 事件重建
  :class:`PerceptionSnapshot` 序列（逐项校验 payload 结构，坏结构抛
  ``ValueError`` 并带事件 seq）；``frame_seq`` 按 perception 事件的出现
  顺序重编号（0 起）——引擎每 tick 恰好写一条 perception 事件时与原
  运行编号一致，保证 decision_hash 可复现（哈希含 frame_seq/ts）。
- :class:`FixedPerceptionReplayer`（TRC-005）：跳过视觉直接重放快照
  序列，逐帧 ``MachineRuntime.tick`` 驱动；状态/意图/哈希序列应与原
  运行完全一致（确定性证据，AC-P0-08 的回放侧）。
- :class:`RawFrameReplayer`（TRC-006）：从事件的 ``frame_ref`` 取回
  原始帧，按 detectors 字典的声明顺序重跑视觉管线（感知聚合确定：
  字典序 = 项目声明序；可选 ``stable`` 逐检测器稳定帧聚合），
  再驱动状态机。隐私模式轨迹（``frame_ref=null``）无法做原始帧回放，
  明确抛 ``ValueError``。

时钟约定：两个 replayer 的 ``run`` 都接受注入 Clock（默认用内置
:class:`ReplayClock` 跟随快照时间戳单调推进）。注入的时钟若提供
``advance_to(ts)`` 或 ``advance(delta)``（如 ``common.FakeClock``），
replayer 会在每 tick 前按相邻快照时间差推进——状态超时/退避等定时
路径由此可测；无这类方法的 Clock 原样使用、不被推进。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from capture_api.frames import Frame, FrameMeta
from common.clock import Clock
from domain_model.models import FieldObservation, PerceptionSnapshot
from trace_format.events import TraceEvent, TraceEventType
from trace_format.frame_store import FrameStore
from vision_core.base import Detector, PerceptionBuilder
from vision_core.stability import StableFrameAggregator, StabilityConfig

if TYPE_CHECKING:  # 仅类型标注（避免 state_machine -> input_broker -> trace_format 导入环）
    from state_machine import MachineRuntime

__all__ = [
    "ReplayClock",
    "ReplayIntent",
    "ReplayResult",
    "FixedPerceptionReplayer",
    "RawFrameReplayer",
    "load_snapshots",
    "load_frame_refs",
    "perception_payload",
]


# ---------------------------------------------------------------------------
# payload 契约：构造与解析
# ---------------------------------------------------------------------------


def perception_payload(
    snapshot: PerceptionSnapshot, frame_ref: str | None = None
) -> dict[str, Any]:
    """把快照序列化为 perception_snapshot 事件 payload（契约见模块 docstring）。

    ``frame_ref`` 为该帧在 FrameStore 中的内容寻址引用；隐私模式传
    ``None``。
    """
    return {
        "fields": {
            name: {
                "present": bool(obs.present),
                "confidence": float(obs.confidence),
                "value": obs.value,
            }
            for name, obs in snapshot.values.items()
        },
        "frame_ref": frame_ref,
        "ts": float(snapshot.ts_monotonic),
    }


def _iter_perception_payloads(events: Sequence[TraceEvent]):
    """按序产出 (事件 seq, payload)；非 perception 事件跳过，结构非法抛 ValueError。"""
    for event in events:
        if event.type != TraceEventType.PERCEPTION_SNAPSHOT.value:
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            raise ValueError(f"seq={event.seq}: perception payload 必须是对象")
        fields = payload.get("fields")
        if not isinstance(fields, dict):
            raise ValueError(f"seq={event.seq}: payload 缺少 fields 对象")
        yield event.seq, fields, payload


def _build_observation(seq: int, name: str, entry: Any) -> FieldObservation:
    """校验并构造单字段观测（契约：present/confidence/value）。"""
    if not isinstance(entry, dict):
        raise ValueError(f"seq={seq}: 字段 {name!r} 的观测必须是对象")
    present = entry.get("present")
    if not isinstance(present, bool):
        raise ValueError(f"seq={seq}: 字段 {name!r} 的 present 必须是布尔值，得到 {present!r}")
    confidence = entry.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise ValueError(f"seq={seq}: 字段 {name!r} 的 confidence 必须在 0~1，得到 {confidence!r}")
    value = entry.get("value")
    valid_value = value is None or (
        isinstance(value, (str, int, float)) and not isinstance(value, bool)
    )
    if not valid_value:
        raise ValueError(f"seq={seq}: 字段 {name!r} 的 value 只允许数值/字符串/null，得到 {value!r}")
    return FieldObservation(name=name, present=present, confidence=float(confidence), value=value)


def load_snapshots(events: Sequence[TraceEvent]) -> list[PerceptionSnapshot]:
    """从 perception_snapshot 事件重建快照序列（TRC-005）。

    - ``frame_seq`` = 该事件在 perception 事件中的出现序号（0 起）；
    - ``ts_monotonic`` = payload 的 ``ts``；
    - payload 结构非法（缺 fields / present 非布尔 / confidence 越界 /
      value 类型非法 / ts 非数字 / frame_ref 类型非法）抛 ``ValueError``。
    """
    out: list[PerceptionSnapshot] = []
    for seq, fields, payload in _iter_perception_payloads(events):
        ts = payload.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise ValueError(f"seq={seq}: payload 的 ts 必须是数字，得到 {ts!r}")
        frame_ref = payload.get("frame_ref")
        if frame_ref is not None and not isinstance(frame_ref, str):
            raise ValueError(f"seq={seq}: payload 的 frame_ref 只允许字符串或 null，得到 {frame_ref!r}")
        values = {name: _build_observation(seq, name, entry) for name, entry in fields.items()}
        out.append(
            PerceptionSnapshot(frame_seq=len(out), ts_monotonic=float(ts), values=values)
        )
    return out


def load_frame_refs(events: Sequence[TraceEvent]) -> tuple[str | None, ...]:
    """按序提取 perception 事件的 frame_ref（与 :func:`load_snapshots` 对齐）。"""
    refs: list[str | None] = []
    for seq, _fields, payload in _iter_perception_payloads(events):
        frame_ref = payload.get("frame_ref")
        if frame_ref is not None and not isinstance(frame_ref, str):
            raise ValueError(f"seq={seq}: payload 的 frame_ref 只允许字符串或 null，得到 {frame_ref!r}")
        refs.append(frame_ref)
    return tuple(refs)


# ---------------------------------------------------------------------------
# 回放时钟与结果结构
# ---------------------------------------------------------------------------


class ReplayClock:
    """回放默认时钟：``advance_to(ts)`` 单调推进到给定时间戳（不回退）。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance_to(self, ts: float) -> None:
        """推进到 ts（仅当 ts 大于当前时刻；保证单调）。"""
        ts = float(ts)
        if ts > self._now:
            self._now = ts


def _sync_clock(clock: Clock, ts: float, prev_ts: float | None) -> None:
    """tick 前把时钟同步到快照时间戳（可推进的时钟才被推进）。"""
    advance_to = getattr(clock, "advance_to", None)
    if callable(advance_to):
        advance_to(float(ts))
        return
    advance = getattr(clock, "advance", None)
    if callable(advance) and prev_ts is not None:
        delta = float(ts) - float(prev_ts)
        if delta > 0.0:
            advance(delta)


@dataclass(frozen=True)
class ReplayIntent:
    """回放记录的意图（剔除 intent_id/时间戳等易变字段，保证可比较）。"""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    cause: str = ""

    @classmethod
    def from_intent(cls, intent: Any) -> "ReplayIntent":
        """从 InputIntent（或等价对象）提取 (kind, payload, cause)。"""
        return cls(kind=str(intent.kind), payload=dict(intent.payload), cause=str(intent.cause))


@dataclass(frozen=True)
class ReplayResult:
    """一次回放的完整决策序列（TRC-005/006 的输出，TRC-007 的输入）。

    Attributes:
        state_seq:    每 tick 结束时的状态名（迁移帧 = to_state，否则保持）。
        intent_seq:   每 tick 产出的意图（kind/payload/cause）元组的元组。
        hash_seq:     每 tick 的 decision_hash（含快照与时序信息，逐 tick 可比）。
        stopped:      回放是否停止（终态/取消/错误）。
        stop_reason:  停止原因；未停止为 None。
        snapshot_seq: 每 tick 驱动状态机所用的感知快照（感知差异定位用）。
        frame_refs:   每 tick 的帧引用（原始帧回放填实；固定感知回放为 None）。
    """

    state_seq: tuple[str, ...]
    intent_seq: tuple[tuple[ReplayIntent, ...], ...]
    hash_seq: tuple[str, ...]
    stopped: bool
    stop_reason: str | None
    snapshot_seq: tuple[PerceptionSnapshot, ...] = ()
    frame_refs: tuple[str | None, ...] = ()

    @property
    def ticks(self) -> int:
        """回放的 tick 数。"""
        return len(self.state_seq)


def _drive(
    machine_factory: Callable[[Clock], MachineRuntime],
    snapshots: Sequence[PerceptionSnapshot],
    frame_refs: Sequence[str | None],
    initial_clock: Clock | None,
) -> ReplayResult:
    """公共驱动循环：逐帧 tick，直到快照耗尽或运行停止。"""
    if snapshots:
        clock: Clock = (
            initial_clock
            if initial_clock is not None
            else ReplayClock(snapshots[0].ts_monotonic)
        )
    else:
        clock = initial_clock if initial_clock is not None else ReplayClock(0.0)
    runtime = machine_factory(clock)
    states: list[str] = []
    intents: list[tuple[ReplayIntent, ...]] = []
    hashes: list[str] = []
    prev_ts: float | None = None
    for snapshot in snapshots:
        _sync_clock(clock, snapshot.ts_monotonic, prev_ts)
        prev_ts = snapshot.ts_monotonic
        result = runtime.tick(snapshot)
        states.append(result.to_state if result.to_state is not None else result.from_state)
        intents.append(tuple(ReplayIntent.from_intent(i) for i in result.emitted_intents))
        hashes.append(result.decision_hash)
        if result.stopped:
            return ReplayResult(
                state_seq=tuple(states),
                intent_seq=tuple(intents),
                hash_seq=tuple(hashes),
                stopped=True,
                stop_reason=result.stop_reason,
                snapshot_seq=tuple(snapshots),
                frame_refs=tuple(frame_refs),
            )
    return ReplayResult(
        state_seq=tuple(states),
        intent_seq=tuple(intents),
        hash_seq=tuple(hashes),
        stopped=False,
        stop_reason=None,
        snapshot_seq=tuple(snapshots),
        frame_refs=tuple(frame_refs),
    )


# ---------------------------------------------------------------------------
# TRC-005：固定感知回放
# ---------------------------------------------------------------------------


class FixedPerceptionReplayer:
    """固定感知回放：跳过视觉，直接用 trace 里的快照序列驱动状态机（TRC-005）。

    Args:
        machine_factory: 接收 Clock 返回新 :class:`MachineRuntime` 的工厂
                        （每次 ``run`` 一个全新运行时，回放互不污染）。
    """

    def __init__(self, machine_factory: Callable[[Clock], MachineRuntime]) -> None:
        self._machine_factory = machine_factory

    def run(
        self,
        snapshots: Sequence[PerceptionSnapshot],
        initial_clock: Clock | None = None,
    ) -> ReplayResult:
        """重放快照序列；超时等定时路径可通过注入可推进 Clock 触发。"""
        for snapshot in snapshots:
            if not isinstance(snapshot, PerceptionSnapshot):
                raise TypeError(f"快照必须是 PerceptionSnapshot，得到 {type(snapshot).__name__}")
        return _drive(
            self._machine_factory,
            tuple(snapshots),
            (None,) * len(snapshots),
            initial_clock,
        )


# ---------------------------------------------------------------------------
# TRC-006：原始帧回放
# ---------------------------------------------------------------------------


class RawFrameReplayer:
    """原始帧回放：按 frame_ref 取帧重跑视觉管线，再驱动状态机（TRC-006）。

    Args:
        machine_factory: 接收 Clock 返回新 :class:`MachineRuntime` 的工厂。
        detectors:       detector_id -> 检测器实例；**字典声明序即感知聚合
                         顺序**（与项目 detectors 声明序一致，确定性）。
        frame_store:     帧存储（TRC-003）；引用缺失抛带引用的 KeyError。
        stable:          可选的 detector_id -> :class:`StabilityConfig`
                         稳定帧聚合选项；每次 ``run`` 新建聚合器（状态隔离）。
    """

    def __init__(
        self,
        machine_factory: Callable[[Clock], MachineRuntime],
        detectors: Mapping[str, Detector],
        frame_store: FrameStore,
        *,
        stable: Mapping[str, StabilityConfig] | None = None,
    ) -> None:
        if not detectors:
            raise ValueError("detectors 不能为空")
        self._machine_factory = machine_factory
        # dict/Mapping 的插入序 = 项目 detectors 声明序（确定性聚合顺序）。
        self._detectors: tuple[Detector, ...] = tuple(detectors.values())
        self._builder = PerceptionBuilder.from_detectors(self._detectors)
        self._frame_store = frame_store
        self._stable: dict[str, StabilityConfig] | None = None
        if stable is not None:
            known = set(detectors)
            unknown = sorted(set(stable) - known)
            if unknown:
                raise ValueError(f"stable 引用了未知 detector_id: {unknown}")
            self._stable = dict(stable)

    def run(
        self,
        events: Sequence[TraceEvent],
        initial_clock: Clock | None = None,
    ) -> ReplayResult:
        """重放：快照结构校验 -> 取帧 -> 重跑检测 -> 聚合 -> 逐帧 tick。"""
        snapshots = load_snapshots(events)
        refs = load_frame_refs(events)
        for index, ref in enumerate(refs):
            if ref is None:
                raise ValueError(
                    f"第 {index} 个 perception 事件的 frame_ref 为空"
                    "（隐私模式轨迹无法做原始帧回放，请用 FixedPerceptionReplayer）"
                )
        if snapshots:
            clock: Clock = (
                initial_clock
                if initial_clock is not None
                else ReplayClock(snapshots[0].ts_monotonic)
            )
        else:
            clock = initial_clock if initial_clock is not None else ReplayClock(0.0)
        runtime = self._machine_factory(clock)
        aggregators: dict[str, StableFrameAggregator] = {}
        if self._stable is not None:
            for detector in self._detectors:  # 每次 run 新建聚合器（状态隔离，确定性归零）
                config = self._stable.get(str(detector.detector_id))
                if config is not None:
                    aggregators[str(detector.detector_id)] = StableFrameAggregator(
                        str(detector.detector_id), config
                    )

        states: list[str] = []
        intents: list[tuple[ReplayIntent, ...]] = []
        hashes: list[str] = []
        rebuilt: list[PerceptionSnapshot] = []
        prev_ts: float | None = None
        for snapshot, ref in zip(snapshots, refs):
            _sync_clock(clock, snapshot.ts_monotonic, prev_ts)
            prev_ts = snapshot.ts_monotonic
            pixels = self._frame_store.get(ref)  # 缺失 -> KeyError(带引用)
            frame = Frame(
                pixels,
                FrameMeta(
                    seq=snapshot.frame_seq,
                    ts_monotonic=snapshot.ts_monotonic,
                    adapter="replay",
                    source_width=int(pixels.shape[1]),
                    source_height=int(pixels.shape[0]),
                ),
            )
            results = []
            for detector in self._detectors:  # 声明序，确定性
                raw = detector.detect(frame)
                aggregator = aggregators.get(str(detector.detector_id))
                results.append(aggregator.update(raw) if aggregator is not None else raw)
            current = self._builder.build(
                results, frame_seq=snapshot.frame_seq, ts_monotonic=snapshot.ts_monotonic
            )
            rebuilt.append(current)
            result = runtime.tick(current)
            states.append(result.to_state if result.to_state is not None else result.from_state)
            intents.append(tuple(ReplayIntent.from_intent(i) for i in result.emitted_intents))
            hashes.append(result.decision_hash)
            if result.stopped:
                return ReplayResult(
                    state_seq=tuple(states),
                    intent_seq=tuple(intents),
                    hash_seq=tuple(hashes),
                    stopped=True,
                    stop_reason=result.stop_reason,
                    snapshot_seq=tuple(rebuilt),
                    frame_refs=refs,
                )
        return ReplayResult(
            state_seq=tuple(states),
            intent_seq=tuple(intents),
            hash_seq=tuple(hashes),
            stopped=False,
            stop_reason=None,
            snapshot_seq=tuple(rebuilt),
            frame_refs=refs,
        )
