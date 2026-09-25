"""TRC-003/005/006/007/008 单测：帧存储、轨迹回放、差异引擎与时间轴查询。

覆盖：
- FrameStore：内容寻址/去重/取回/缺失引用/隐私模式/非法引用/索引；
- load_snapshots：payload 契约校验（结构非法逐项拒绝）；
- FixedPerceptionReplayer：序列复现、终态停止、注入 Clock 的超时路径；
- RawFrameReplayer：视觉重跑与固定感知回放一致、确定性、稳定帧聚合、
  缺帧/隐私轨迹错误；
- diff_replays / diff_perceptions：分类、严重度、影响面、报告上下文；
- query_timeline：各过滤参数与组合。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import pytest

from common.clock import Clock, FakeClock
from domain_model import CompiledMachine, FieldObservation, PerceptionSnapshot, compile_machine_dict
from state_machine import MachineRuntime, TickResult
from trace_format import (
    GENESIS_HASH,
    FixedPerceptionReplayer,
    FrameStorageDisabled,
    FrameStore,
    JsonlTraceWriter,
    RawFrameReplayer,
    ReplayClock,
    ReplayIntent,
    ReplayResult,
    TraceEventType,
    diff_perceptions,
    diff_replays,
    load_frame_refs,
    load_snapshots,
    perception_payload,
    query_timeline,
)
from vision_core.base import DetectorResult, NormRoi
from vision_core.stability import StabilityConfig

SESSION = "sess-replay"
CID = "cid-replay"


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


def snap(frame_seq: int, ts: float, **fields: tuple) -> PerceptionSnapshot:
    """构造感知快照：字段值元组为 (present, value[, confidence])。"""
    values = {
        name: FieldObservation(
            name=name,
            present=spec[0],
            value=spec[1],
            confidence=spec[2] if len(spec) > 2 else 0.9,
        )
        for name, spec in fields.items()
    }
    return PerceptionSnapshot(frame_seq=frame_seq, ts_monotonic=ts, values=values)


def compile_ok(data: dict[str, Any]) -> CompiledMachine:
    """编译状态机 dict（失败直接让用例报错）。"""
    return compile_machine_dict(data, file="<unit>")


SIMPLE_MACHINE: dict[str, Any] = {
    "schema_version": 1,
    "machine_id": "u1",
    "initial": "a",
    "states": {
        "a": {
            "entry": [{"kind": "press_key", "key": "go"}],
            "transitions": [{"when": "go.present", "to": "b"}],
        },
        "b": {"terminal": True},
    },
}

TIMEOUT_MACHINE: dict[str, Any] = {
    "schema_version": 1,
    "machine_id": "tmo",
    "initial": "wait",
    "states": {
        "wait": {
            "timeout_seconds": 0.5,
            "transitions": [{"when": "go.present", "to": "done", "on_timeout_to": "rescue"}],
        },
        "rescue": {"terminal": True},
        "done": {"terminal": True},
    },
}

LIT_MACHINE: dict[str, Any] = {
    "schema_version": 1,
    "machine_id": "lit",
    "initial": "dark_wait",
    "states": {
        "dark_wait": {"transitions": [{"when": "bright.present", "to": "lit"}]},
        "lit": {"terminal": True},
    },
}


def factory(machine: CompiledMachine) -> Callable[[Clock], MachineRuntime]:
    """运行时工厂：每次 run 一个全新 MachineRuntime。"""
    return lambda clock: MachineRuntime(machine, clock)


def run_original(machine: CompiledMachine, snapshots: Sequence[PerceptionSnapshot]) -> ReplayResult:
    """按回放同口径跑一次"原运行"（用于逐序列对照）。"""
    rt = MachineRuntime(machine, FakeClock())
    states: list[str] = []
    intents: list[tuple[ReplayIntent, ...]] = []
    hashes: list[str] = []
    stopped = False
    reason: str | None = None
    for s in snapshots:
        r = rt.tick(s)
        states.append(r.to_state if r.to_state is not None else r.from_state)
        intents.append(tuple(ReplayIntent.from_intent(i) for i in r.emitted_intents))
        hashes.append(r.decision_hash)
        if r.stopped:
            stopped = True
            reason = r.stop_reason
            break
    return ReplayResult(
        state_seq=tuple(states),
        intent_seq=tuple(intents),
        hash_seq=tuple(hashes),
        stopped=stopped,
        stop_reason=reason,
        snapshot_seq=tuple(snapshots[: len(states)]),
        frame_refs=(None,) * len(states),
    )


class BrightnessDetector:
    """测试检测器：按平均亮度判定（确定性，无内部状态）。"""

    version = "test-1"

    def __init__(self, detector_id: str = "bright", field_name: str = "bright", threshold: float = 100.0) -> None:
        self.detector_id = detector_id
        self.field_name = field_name
        self.threshold = float(threshold)

    def detect(self, frame: Any, roi: NormRoi | None = None) -> DetectorResult:
        mean = float(frame.pixels.mean())
        value = round(mean / 255.0, 6)
        present = mean > self.threshold
        return DetectorResult(
            detector_id=self.detector_id,
            present=present,
            confidence=value if present else 0.0,
            value=value,
            bbox=None,
            elapsed_ms=0.0,
            version=self.version,
            error=None,
        )


def bright_pixels() -> np.ndarray:
    return np.full((4, 6, 3), 200, dtype=np.uint8)


def dark_pixels() -> np.ndarray:
    return np.full((4, 6, 3), 10, dtype=np.uint8)


def write_perception_trace(
    path: Path, snapshots: Sequence[PerceptionSnapshot], refs: Sequence[str | None]
) -> list:
    """按 payload 契约把快照写为 trace 事件，返回事件列表。"""
    with JsonlTraceWriter(path) as writer:
        for s, ref in zip(snapshots, refs):
            writer.append(
                TraceEventType.PERCEPTION_SNAPSHOT,
                ts_monotonic=float(s.ts_monotonic),
                correlation_id=CID,
                session_id=SESSION,
                payload=perception_payload(s, frame_ref=ref),
            )
        return list(writer.events)


# ---------------------------------------------------------------------------
# TRC-003 FrameStore
# ---------------------------------------------------------------------------


class TestFrameStore:
    def test_ref_is_sha256_of_pixel_bytes(self, tmp_path: Path) -> None:
        """引用 = sha256(像素字节)（内容寻址，与逐帧哈希同口径）。"""
        store = FrameStore(tmp_path / "fs")
        pixels = bright_pixels()
        ref = store.put(pixels)
        assert ref == hashlib.sha256(pixels.tobytes()).hexdigest()
        assert len(ref) == 64 and store.has(ref)

    def test_put_then_get_roundtrip(self, tmp_path: Path) -> None:
        """存取往返：形状、dtype 与像素逐字节一致。"""
        store = FrameStore(tmp_path / "fs")
        pixels = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
        ref = store.put(pixels)
        out = store.get(ref)
        assert out.shape == pixels.shape and out.dtype == pixels.dtype
        assert bool((out == pixels).all())

    def test_duplicate_content_is_deduplicated(self, tmp_path: Path) -> None:
        """重复内容：同引用、只落一个 blob、索引仍追加（记录去重标记）。"""
        store = FrameStore(tmp_path / "fs")
        ref1 = store.put(bright_pixels())
        ref2 = store.put(bright_pixels())
        assert ref1 == ref2
        blobs = list((tmp_path / "fs" / "frames").iterdir())
        assert len(blobs) == 1
        records = store.read_index()
        assert [r["deduplicated"] for r in records] == [False, True]
        assert store.refs() == [ref1]

    def test_get_missing_reference_raises_key_error(self, tmp_path: Path) -> None:
        """引用不存在：KeyError 消息带引用原文。"""
        store = FrameStore(tmp_path / "fs")
        missing = "ab" * 32
        with pytest.raises(KeyError) as excinfo:
            store.get(missing)
        assert missing in str(excinfo.value)

    def test_privacy_mode_put_returns_none_and_get_disabled(self, tmp_path: Path) -> None:
        """隐私模式：put 返回 None、不写任何文件、get 抛 FrameStorageDisabled。"""
        root = tmp_path / "private"
        store = FrameStore(root, enabled=False)
        assert store.put(bright_pixels()) is None
        assert not root.exists()  # 隐私模式零落盘
        with pytest.raises(FrameStorageDisabled):
            store.get("ab" * 32)
        assert store.has("ab" * 32) is False

    def test_malformed_reference_rejected(self, tmp_path: Path) -> None:
        """非法引用（含路径穿越形态）一律 ValueError。"""
        store = FrameStore(tmp_path / "fs")
        for bad in ("../escape", "XYZ", "ab" * 31, "", "ab" * 32 + "x"):
            with pytest.raises(ValueError):
                store.blob_path(bad)

    def test_index_jsonl_is_appended_json_lines(self, tmp_path: Path) -> None:
        """索引为合法 JSONL，逐 put 追加一行。"""
        store = FrameStore(tmp_path / "fs")
        store.put(bright_pixels())
        store.put(dark_pixels())
        lines = (tmp_path / "fs" / "index.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert set(first) == {"ref", "shape", "dtype", "deduplicated"}
        assert first["shape"] == [4, 6, 3] and first["dtype"] == "uint8"

    def test_put_rejects_non_array(self, tmp_path: Path) -> None:
        store = FrameStore(tmp_path / "fs")
        with pytest.raises(TypeError):
            store.put([[1, 2], [3, 4]])  # type: ignore[arg-type]

    def test_blob_path_layout(self, tmp_path: Path) -> None:
        """blob 布局：<root>/frames/<ref>.npy。"""
        store = FrameStore(tmp_path / "fs")
        ref = "cd" * 32
        assert store.blob_path(ref) == tmp_path / "fs" / "frames" / f"{ref}.npy"


# ---------------------------------------------------------------------------
# payload 契约与 load_snapshots
# ---------------------------------------------------------------------------


def perception_event_payload(**overrides: Any) -> dict[str, Any]:
    """合法 perception payload（可逐项破坏）。"""
    return {
        "fields": {"go": {"present": True, "confidence": 0.9, "value": None}},
        "frame_ref": None,
        "ts": 1.5,
        **overrides,
    }


def append_perception(writer: JsonlTraceWriter, payload: dict[str, Any], ts: float = 0.0) -> None:
    writer.append(
        TraceEventType.PERCEPTION_SNAPSHOT,
        ts_monotonic=ts,
        correlation_id=CID,
        session_id=SESSION,
        payload=payload,
    )


class TestLoadSnapshots:
    def test_roundtrip_via_writer(self, tmp_path: Path) -> None:
        """契约往返：payload -> 快照，字段/ts/frame_ref 一致。"""
        path = tmp_path / "trace.jsonl"
        with JsonlTraceWriter(path) as writer:
            append_perception(writer, perception_event_payload(), ts=1.5)
        events = writer.events
        snapshots = load_snapshots(events)
        assert len(snapshots) == 1
        assert snapshots[0].frame_seq == 0
        assert snapshots[0].ts_monotonic == 1.5
        obs = snapshots[0].values["go"]
        assert obs.present is True and obs.confidence == 0.9 and obs.value is None

    def test_ignores_other_event_types(self, tmp_path: Path) -> None:
        """非 perception 事件被忽略；frame_seq 按 perception 出现序重编号。"""
        path = tmp_path / "trace.jsonl"
        with JsonlTraceWriter(path) as writer:
            append_perception(writer, perception_event_payload())
            writer.append(
                TraceEventType.STATE_TRANSITION,
                ts_monotonic=0.1,
                payload={"from": "a", "to": "b", "decision_hash": "h"},
            )
            append_perception(writer, perception_event_payload(ts=2.0), ts=2.0)
        snapshots = load_snapshots(writer.events)
        assert [s.frame_seq for s in snapshots] == [0, 1]
        assert [s.ts_monotonic for s in snapshots] == [1.5, 2.0]

    def test_missing_fields_key_rejected(self, tmp_path: Path) -> None:
        with JsonlTraceWriter(tmp_path / "t.jsonl") as writer:
            append_perception(writer, perception_event_payload(fields=None))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="fields"):
            load_snapshots(writer.events)

    def test_bad_present_type_rejected(self, tmp_path: Path) -> None:
        payload = perception_event_payload()
        payload["fields"]["go"]["present"] = 1  # type: ignore[assignment]
        with JsonlTraceWriter(tmp_path / "t.jsonl") as writer:
            append_perception(writer, payload)
        with pytest.raises(ValueError, match="present"):
            load_snapshots(writer.events)

    def test_confidence_out_of_range_rejected(self, tmp_path: Path) -> None:
        payload = perception_event_payload()
        payload["fields"]["go"]["confidence"] = 1.5
        with JsonlTraceWriter(tmp_path / "t.jsonl") as writer:
            append_perception(writer, payload)
        with pytest.raises(ValueError, match="confidence"):
            load_snapshots(writer.events)

    def test_bad_value_type_rejected(self, tmp_path: Path) -> None:
        payload = perception_event_payload()
        payload["fields"]["go"]["value"] = ["not", "scalar"]
        with JsonlTraceWriter(tmp_path / "t.jsonl") as writer:
            append_perception(writer, payload)
        with pytest.raises(ValueError, match="value"):
            load_snapshots(writer.events)

    def test_bad_ts_rejected(self, tmp_path: Path) -> None:
        with JsonlTraceWriter(tmp_path / "t.jsonl") as writer:
            append_perception(writer, perception_event_payload(ts="now"))
        with pytest.raises(ValueError, match="ts"):
            load_snapshots(writer.events)

    def test_bad_frame_ref_type_rejected(self, tmp_path: Path) -> None:
        with JsonlTraceWriter(tmp_path / "t.jsonl") as writer:
            append_perception(writer, perception_event_payload(frame_ref=123))
        with pytest.raises(ValueError, match="frame_ref"):
            load_snapshots(writer.events)

    def test_load_frame_refs_aligns_with_snapshots(self, tmp_path: Path) -> None:
        """frame_ref 提取与快照一一对应（含 null）。"""
        payload_a = perception_event_payload(frame_ref="ab" * 32)
        payload_b = perception_event_payload(ts=2.0, frame_ref=None)
        with JsonlTraceWriter(tmp_path / "t.jsonl") as writer:
            append_perception(writer, payload_a)
            append_perception(writer, payload_b, ts=2.0)
        assert load_frame_refs(writer.events) == ("ab" * 32, None)


# ---------------------------------------------------------------------------
# TRC-005 FixedPerceptionReplayer
# ---------------------------------------------------------------------------


class TestFixedPerceptionReplayer:
    def test_reproduces_original_run_sequences(self, tmp_path: Path) -> None:
        """重放：状态/意图/哈希序列与原运行完全一致。"""
        machine = compile_ok(SIMPLE_MACHINE)
        snapshots = [
            snap(0, 0.0, go=(False, None)),
            snap(1, 0.1, go=(True, None)),
            snap(2, 0.2, go=(True, None)),
        ]
        expected = run_original(machine, snapshots)
        result = FixedPerceptionReplayer(factory(machine)).run(snapshots, FakeClock())
        assert result.state_seq == expected.state_seq == ("a", "b", "b")
        assert result.hash_seq == expected.hash_seq
        assert result.intent_seq == expected.intent_seq
        assert result.intent_seq[0] == (
            ReplayIntent(kind="key_down", payload={"key": "go"}, cause="a"),
            ReplayIntent(kind="key_up", payload={"key": "go"}, cause="a"),
        )
        assert result.stopped is True and result.stop_reason == "terminal"

    def test_stops_at_terminal_and_reports_reason(self) -> None:
        machine = compile_ok(SIMPLE_MACHINE)
        snapshots = [snap(0, 0.0), snap(1, 0.1, go=(True, None)), snap(2, 0.2, go=(True, None))]
        result = FixedPerceptionReplayer(factory(machine)).run(snapshots)
        assert result.stopped and result.stop_reason == "terminal"
        assert result.ticks == 3

    def test_timeout_fires_with_replay_clock(self) -> None:
        """默认 ReplayClock 跟随快照时间戳：状态超时路径被触发。"""
        machine = compile_ok(TIMEOUT_MACHINE)
        snapshots = [snap(0, 0.0), snap(1, 0.3), snap(2, 1.0)]
        result = FixedPerceptionReplayer(factory(machine)).run(snapshots)
        assert result.state_seq == ("wait", "wait", "rescue")

    def test_timeout_fires_with_injected_fake_clock(self) -> None:
        """注入 FakeClock：按相邻快照时间差推进，超时路径一致。"""
        machine = compile_ok(TIMEOUT_MACHINE)
        snapshots = [snap(0, 0.0), snap(1, 0.3), snap(2, 1.0)]
        clock = FakeClock()
        result = FixedPerceptionReplayer(factory(machine)).run(snapshots, clock)
        assert result.state_seq == ("wait", "wait", "rescue")
        assert clock.now() == pytest.approx(1.0)

    def test_empty_snapshots_give_empty_result(self) -> None:
        machine = compile_ok(SIMPLE_MACHINE)
        result = FixedPerceptionReplayer(factory(machine)).run([])
        assert result.ticks == 0 and result.stopped is False
        assert result.state_seq == () and result.hash_seq == ()

    def test_rejects_non_snapshot_input(self) -> None:
        machine = compile_ok(SIMPLE_MACHINE)
        with pytest.raises(TypeError):
            FixedPerceptionReplayer(factory(machine)).run(["not-a-snapshot"])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# TRC-006 RawFrameReplayer
# ---------------------------------------------------------------------------


def make_lit_scene(store: FrameStore, tmp_path: Path, pattern: Sequence[bool]):
    """构造亮/暗帧场景：返回 (snapshots, events, refs, detector)。"""
    detector = BrightnessDetector()
    frames = [bright_pixels() if on else dark_pixels() for on in pattern]
    builder_refs = [store.put(f) for f in frames]
    from vision_core.base import PerceptionBuilder

    builder = PerceptionBuilder.from_detectors([detector])
    snapshots = []
    for i, pixels in enumerate(frames):
        frame = _frame(pixels, i)
        result = detector.detect(frame)
        snapshots.append(builder.build([result], frame_seq=i, ts_monotonic=i * 0.1))
    events = write_perception_trace(tmp_path / "trace.jsonl", snapshots, builder_refs)
    return snapshots, events, builder_refs, detector


def _frame(pixels: np.ndarray, seq: int):
    from capture_api.frames import Frame, FrameMeta

    return Frame(
        pixels,
        FrameMeta(seq=seq, ts_monotonic=seq * 0.1, adapter="test",
                  source_width=int(pixels.shape[1]), source_height=int(pixels.shape[0])),
    )


class TestRawFrameReplayer:
    def test_matches_fixed_perception_replay(self, tmp_path: Path) -> None:
        """重跑视觉后的序列与固定感知回放完全一致（感知确定性证据）。"""
        machine = compile_ok(LIT_MACHINE)
        store = FrameStore(tmp_path / "fs")
        snapshots, events, _refs, detector = make_lit_scene(store, tmp_path, [False, True, True])
        raw = RawFrameReplayer(factory(machine), {"bright": detector}, store).run(events)
        fixed = FixedPerceptionReplayer(factory(machine)).run(snapshots)
        assert raw.state_seq == fixed.state_seq == ("dark_wait", "lit", "lit")
        assert raw.hash_seq == fixed.hash_seq
        assert raw.intent_seq == fixed.intent_seq
        assert raw.frame_refs == tuple(_refs) and all(r is not None for r in raw.frame_refs)

    def test_deterministic_across_runs(self, tmp_path: Path) -> None:
        machine = compile_ok(LIT_MACHINE)
        store = FrameStore(tmp_path / "fs")
        _snapshots, events, _refs, detector = make_lit_scene(store, tmp_path, [True, True])
        replayer = RawFrameReplayer(factory(machine), {"bright": detector}, store)
        first = replayer.run(events)
        second = replayer.run(events, FakeClock())
        assert first.hash_seq == second.hash_seq
        assert first.state_seq == second.state_seq

    def test_privacy_trace_rejected(self, tmp_path: Path) -> None:
        """隐私轨迹（frame_ref=null）明确拒绝原始帧回放。"""
        machine = compile_ok(LIT_MACHINE)
        store = FrameStore(tmp_path / "fs")
        snapshots = [snap(0, 0.0, bright=(True, 0.8))]
        events = write_perception_trace(tmp_path / "t.jsonl", snapshots, [None])
        with pytest.raises(ValueError, match="frame_ref"):
            RawFrameReplayer(factory(machine), {"bright": BrightnessDetector("bright", "bright")}, store).run(events)

    def test_missing_blob_raises_key_error_with_ref(self, tmp_path: Path) -> None:
        machine = compile_ok(LIT_MACHINE)
        store = FrameStore(tmp_path / "fs")  # 空 store
        snapshots = [snap(0, 0.0, bright=(True, 0.8))]
        events = write_perception_trace(tmp_path / "t.jsonl", snapshots, ["ef" * 32])
        with pytest.raises(KeyError) as excinfo:
            RawFrameReplayer(factory(machine), {"bright": BrightnessDetector("bright", "bright")}, store).run(events)
        assert "ef" * 32 in str(excinfo.value)

    def test_stable_aggregation_delays_transition(self, tmp_path: Path) -> None:
        """stable 选项：稳定帧聚合使命中翻转延后（enter_frames=2）。"""
        machine = compile_ok(LIT_MACHINE)
        store = FrameStore(tmp_path / "fs")
        snapshots, events, _refs, detector = make_lit_scene(store, tmp_path, [False, True, True])
        stable = {"bright": StabilityConfig(enter_frames=2)}
        result = RawFrameReplayer(
            factory(machine), {"bright": detector}, store, stable=stable
        ).run(events)
        assert result.state_seq == ("dark_wait", "dark_wait", "lit")
        assert result.snapshot_seq[1].values["bright"].present is False
        assert result.snapshot_seq[2].values["bright"].present is True

    def test_unknown_stable_detector_rejected(self, tmp_path: Path) -> None:
        machine = compile_ok(LIT_MACHINE)
        store = FrameStore(tmp_path / "fs")
        with pytest.raises(ValueError, match="detector_id"):
            RawFrameReplayer(
                factory(machine),
                {"bright": BrightnessDetector("bright", "bright")},
                store,
                stable={"nope": StabilityConfig()},
            )

    def test_empty_events_replay(self, tmp_path: Path) -> None:
        machine = compile_ok(LIT_MACHINE)
        store = FrameStore(tmp_path / "fs")
        result = RawFrameReplayer(factory(machine), {"bright": BrightnessDetector()}, store).run([])
        assert result.ticks == 0 and result.stopped is False


# ---------------------------------------------------------------------------
# TRC-007 diff_replays / diff_perceptions
# ---------------------------------------------------------------------------


def make_result(
    states: Sequence[str],
    intents: Sequence[Sequence[ReplayIntent]],
    hashes: Sequence[str],
    *,
    snapshots: Sequence[PerceptionSnapshot] = (),
    stopped: bool = True,
    reason: str | None = "terminal",
    refs: Sequence[str | None] | None = None,
) -> ReplayResult:
    """手工构造 ReplayResult（差异单测用）。"""
    return ReplayResult(
        state_seq=tuple(states),
        intent_seq=tuple(tuple(items) for items in intents),
        hash_seq=tuple(hashes),
        stopped=stopped,
        stop_reason=reason,
        snapshot_seq=tuple(snapshots)
        if snapshots
        else tuple(snap(i, float(i)) for i in range(len(states))),
        frame_refs=tuple(refs) if refs is not None else (None,) * len(states),
    )


class TestDiffReplays:
    def test_identical_replays_report_no_divergence(self) -> None:
        base = make_result(("a", "b"), [(), ()], ("h1", "h2"))
        diff = diff_replays(base, base)
        assert diff.has_divergence is False
        assert diff.severity == "none"
        assert diff.kinds == () and diff.first_divergence_tick is None
        assert "无分歧" in diff.to_report()

    def test_state_divergence_is_error_with_affected_states(self) -> None:
        base = make_result(("a", "b"), [(), ()], ("h1", "h2"))
        other = make_result(("a", "c"), [(), ()], ("h1", "h2"))
        diff = diff_replays(base, other)
        assert diff.severity == "error"
        assert "state" in diff.kinds
        assert diff.first_divergence_tick == 2
        assert diff.affected_states == ("b", "c")
        assert diff.state_divergent_ticks == (2,)

    def test_intent_divergence_is_error_and_lists_kinds(self) -> None:
        base = make_result(("a", "b"), [(), (ReplayIntent("key_down", {}, "x"),)], ("h1", "h2"))
        other = make_result(("a", "b"), [(), ()], ("h1", "h2"))
        diff = diff_replays(base, other)
        assert diff.severity == "error"
        assert "intent" in diff.kinds and "state" not in diff.kinds
        assert diff.affected_intents == ("key_down",)

    def test_hash_only_divergence_is_warning(self) -> None:
        base = make_result(("a", "b"), [(), ()], ("h1", "h2"))
        other = make_result(("a", "b"), [(), ()], ("h1", "zz"))
        diff = diff_replays(base, other)
        assert diff.severity == "warning"
        assert diff.kinds == ("hash",)

    def test_perception_only_divergence_is_warning(self) -> None:
        base = make_result(("a", "a"), [(), ()], ("h1", "h2"),
                           snapshots=[snap(0, 0.0, hp=(True, 0.9)), snap(1, 0.1, hp=(True, 0.9))])
        other = make_result(("a", "a"), [(), ()], ("h1", "h2"),
                            snapshots=[snap(0, 0.0, hp=(True, 0.9)), snap(1, 0.1, hp=(True, 0.1))])
        diff = diff_replays(base, other)
        assert diff.severity == "warning"
        assert diff.kinds == ("perception",)
        assert diff.perception_diffs[0].field == "hp"
        assert diff.perception_diffs[0].tick == 2

    def test_length_mismatch_treated_as_state_error(self) -> None:
        base = make_result(("a", "b", "b"), [(), (), ()], ("h1", "h2", "h3"))
        other = make_result(("a", "b"), [(), ()], ("h1", "h2"))
        diff = diff_replays(base, other)
        assert diff.length_mismatch is True
        assert diff.severity == "error"
        assert "state" in diff.kinds
        assert diff.first_divergence_tick == 3

    def test_perception_diff_carries_detector_and_sample_ref(self) -> None:
        ref = "ab" * 32
        base = make_result(("a",), [()], ("h1",), snapshots=[snap(0, 0.0, hp=(True, 0.9))], refs=[ref])
        other = make_result(("a",), [()], ("h1",), snapshots=[snap(0, 0.0, hp=(False, None))], refs=[ref])
        diff = diff_replays(base, other, detector_ids={"hp": "hp_detector"})
        first = diff.perception_diffs[0]
        assert first.detector_id == "hp_detector"
        assert first.before == {"present": True, "confidence": 0.9, "value": 0.9}
        assert diff.frame_refs_base == (ref,)
        report = diff.to_report()
        assert "hp_detector" in report and ref in report

    def test_affected_transitions_extracted_from_divergence_window(self) -> None:
        base = make_result(("a", "b", "b"), [(), (), ()], ("h1", "h2", "h3"))
        other = make_result(("a", "a", "b"), [(), (), ()], ("h1", "h2", "h3"))
        diff = diff_replays(base, other)
        assert "a→b" in diff.affected_transitions

    def test_report_contains_context_window_and_stats(self) -> None:
        base = make_result(("a", "b", "b"), [(), (), ()], ("h1", "h2", "h3"))
        other = make_result(("a", "c", "c"), [(), (), ()], ("h1", "h2", "h3"))
        report = diff_replays(base, other).to_report()
        assert "回放差异报告" in report
        assert "首个分歧 tick: 2" in report
        assert "上下文（tick 1~3" in report  # 2±3 夹到序列范围
        assert "tick 1:" in report and "tick 3:" in report
        assert "---- 统计 ----" in report
        assert "严重度 error" in report

    def test_diff_perceptions_value_tolerance(self) -> None:
        a = make_result(("a",), [()], ("h",), snapshots=[snap(0, 0.0, hp=(True, 0.5))])
        tiny = make_result(("a",), [()], ("h",), snapshots=[snap(0, 0.0, hp=(True, 0.5 + 1e-12))])
        big = make_result(("a",), [()], ("h",), snapshots=[snap(0, 0.0, hp=(True, 0.6))])
        assert diff_perceptions(a, tiny) == ()
        diffs = diff_perceptions(a, big)
        assert len(diffs) == 1 and diffs[0].field == "hp"

    def test_diff_perceptions_orders_by_tick_then_field(self) -> None:
        base = make_result(
            ("a", "a"), [(), ()], ("h1", "h2"),
            snapshots=[snap(0, 0.0, zeta=(True, 1.0)), snap(1, 0.1, alpha=(True, 1.0), zeta=(True, 1.0))],
        )
        other = make_result(
            ("a", "a"), [(), ()], ("h1", "h2"),
            snapshots=[
                snap(0, 0.0, zeta=(True, 1.0), alpha=(False, None)),
                snap(1, 0.1, alpha=(True, 0.0), zeta=(False, None)),
            ],
        )
        diffs = diff_perceptions(base, other)
        assert [(d.tick, d.field) for d in diffs] == [(1, "alpha"), (2, "alpha"), (2, "zeta")]

    def test_diff_perceptions_missing_field_reported(self) -> None:
        base = make_result(("a",), [()], ("h",), snapshots=[snap(0, 0.0, hp=(True, 0.5))])
        other = make_result(("a",), [()], ("h",), snapshots=[snap(0, 0.0)])
        diffs = diff_perceptions(base, other)
        assert len(diffs) == 1
        assert diffs[0].before is not None and diffs[0].after is None


# ---------------------------------------------------------------------------
# TRC-008 query_timeline
# ---------------------------------------------------------------------------


def build_timeline_events(tmp_path: Path) -> list:
    """构造覆盖全部事件类型的样例轨迹。"""
    with JsonlTraceWriter(tmp_path / "trace.jsonl") as writer:
        writer.append("frame_captured", ts_monotonic=0.0, session_id=SESSION, payload={"frame_ref": None})
        writer.append(
            "perception_snapshot", ts_monotonic=0.1, session_id=SESSION,
            payload={"fields": {"health_bar": {"present": True, "confidence": 0.9, "value": 0.8}},
                     "frame_ref": None, "ts": 0.1},
        )
        writer.append(
            "state_transition", ts_monotonic=0.2, session_id=SESSION,
            payload={"from": "idle", "to": "run", "decision_hash": "h1"},
        )
        writer.append(
            "state_transition", ts_monotonic=0.3, session_id=SESSION,
            payload={"from": "run", "to": "stopped", "decision_hash": "h2"},
        )
        writer.append(
            "policy_decision", ts_monotonic=0.4, session_id=SESSION,
            payload={"allowed": False, "reason": "budget_exceeded"},
        )
        writer.append("policy_decision", ts_monotonic=0.5, session_id=SESSION, payload={"allowed": True})
        writer.append("anomaly", ts_monotonic=0.6, session_id=SESSION, payload={"kind": "detector_error"})
        writer.append("estop", ts_monotonic=0.7, session_id=SESSION, payload={"reason": "hotkey"})
        return list(writer.events)


class TestQueryTimeline:
    def test_filter_by_single_type(self, tmp_path: Path) -> None:
        events = build_timeline_events(tmp_path)
        result = query_timeline(events, types="state_transition")
        assert [e.seq for e in result] == [2, 3]

    def test_filter_by_type_set_and_enum(self, tmp_path: Path) -> None:
        events = build_timeline_events(tmp_path)
        by_set = query_timeline(events, types={"perception_snapshot", "anomaly"})
        assert [e.seq for e in by_set] == [1, 6]
        by_enum = query_timeline(events, types=TraceEventType.ESTOP)
        assert [e.seq for e in by_enum] == [7]

    def test_time_window_inclusive_bounds(self, tmp_path: Path) -> None:
        events = build_timeline_events(tmp_path)
        result = query_timeline(events, since=0.2, until=0.5)
        assert [e.seq for e in result] == [2, 3, 4, 5]

    def test_filter_by_state_matches_from_and_to(self, tmp_path: Path) -> None:
        events = build_timeline_events(tmp_path)
        assert [e.seq for e in query_timeline(events, state="run")] == [2, 3]
        assert [e.seq for e in query_timeline(events, state="idle")] == [2]
        assert query_timeline(events, state="nope") == []

    def test_filter_by_detector_id_on_fields_keys(self, tmp_path: Path) -> None:
        events = build_timeline_events(tmp_path)
        result = query_timeline(events, detector_id="health_bar")
        assert [e.seq for e in result] == [1]
        assert query_timeline(events, detector_id="nope") == []

    def test_policy_denied_only(self, tmp_path: Path) -> None:
        events = build_timeline_events(tmp_path)
        result = query_timeline(events, policy_denied_only=True)
        assert [e.seq for e in result] == [4]
        assert result[0].payload["reason"] == "budget_exceeded"

    def test_anomalies_only_excludes_estop(self, tmp_path: Path) -> None:
        events = build_timeline_events(tmp_path)
        result = query_timeline(events, anomalies_only=True)
        assert [e.seq for e in result] == [6]

    def test_limit_truncates_after_filtering(self, tmp_path: Path) -> None:
        events = build_timeline_events(tmp_path)
        assert [e.seq for e in query_timeline(events, limit=2)] == [0, 1]
        assert [e.seq for e in query_timeline(events, types="policy_decision", limit=1)] == [4]
        assert query_timeline(events, limit=0) == []
        with pytest.raises(ValueError):
            query_timeline(events, limit=-1)

    def test_combined_filters_and_order_preserved(self, tmp_path: Path) -> None:
        events = build_timeline_events(tmp_path)
        result = query_timeline(events, since=0.0, until=0.6, state="run", types="state_transition")
        assert [e.seq for e in result] == [2, 3]
        seqs = [e.seq for e in query_timeline(events)]
        assert seqs == sorted(seqs)

    def test_bad_types_argument_rejected(self, tmp_path: Path) -> None:
        events = build_timeline_events(tmp_path)
        with pytest.raises(TypeError):
            query_timeline(events, types=123)  # type: ignore[arg-type]
