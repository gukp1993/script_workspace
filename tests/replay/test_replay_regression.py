"""端到端回放回归（TST-006 / TRC-003/005/006/007，AC-P0-08/09 回放侧证据）。

完整链路：
    arena_lab happy_path 渲染帧序列
    -> 真实检测器（color_bar_ratio 血条 + color_region 目标标记）产出感知快照
    -> FrameStore 内容寻址存帧（TRC-003）
    -> JsonlTraceWriter 按 payload 契约写事件（perception/state_transition/intent_issued）
    -> FixedPerceptionReplayer 重放：state/intent/hash 三序列与原运行完全一致
    -> RawFrameReplayer 重跑视觉：三序列仍一致（确定性证据）
    -> 篡改帧像素 / 修改状态机守卫 -> 差异报告正确定位与分类（AC-P0-09）。

确定性：arena_lab 场景是 (name, seed, resolution, fps, duration) 的纯函数，
检测器与状态机无内部随机性，全部断言不依赖时间容差。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pytest

from arena_lab import run_scenario
from arena_lab.render import Renderer, Rect
from capture_api.frames import Frame, FrameMeta
from common import FakeClock
from detector_opencv.color_bar import ColorBarRatioDetector
from detector_opencv.color_region import ColorRegionDetector
from domain_model import CompiledMachine, Detector, PerceptionSnapshot, compile_machine_dict
from state_machine import MachineRuntime
from trace_format import (
    FixedPerceptionReplayer,
    FrameStore,
    JsonlTraceReader,
    JsonlTraceWriter,
    RawFrameReplayer,
    ReplayIntent,
    ReplayResult,
    TraceEventType,
    diff_replays,
    load_snapshots,
    perception_payload,
    verify,
)
from vision_core.base import PerceptionBuilder

# ---------------------------------------------------------------------------
# 场景与状态机定义
# ---------------------------------------------------------------------------

SEED = 7
RESOLUTION = (640, 360)
FPS = 30.0
DURATION_S = 2.0
SESSION = "sess-replay-regression"
CID = "cid-replay-regression"

#: 目标标记特征色的 RGB 范围（arena_lab 渲染的 COLOR_TARGET_FILL=(255,238,88)）
TARGET_RGB_RANGES = [((238.0, 225.0, 60.0), (255.0, 255.0, 130.0))]

#: 基线状态机：loading --target.present--> engage --health<0.50--> hurt --health>0.70--> recovered
BASE_MACHINE: dict[str, Any] = {
    "schema_version": 1,
    "machine_id": "replay-regression",
    "initial": "loading",
    "states": {
        "loading": {"transitions": [{"when": "target.present", "to": "engage"}]},
        "engage": {
            "entry": [{"kind": "press_key", "key": "e"}],
            "transitions": [{"when": "health_ratio.value < 0.50", "to": "hurt"}],
        },
        "hurt": {
            "entry": [{"kind": "press_key", "key": "h"}],
            "transitions": [{"when": "health_ratio.value > 0.70", "to": "recovered"}],
        },
        "recovered": {"terminal": True},
    },
}

KNOWN_FIELDS = {"health_ratio", "target"}

#: AC-P0-09 对照组：仅修改 engage 的守卫阈值（0.50 -> 0.65，提前一拍受击）
GUARD_MODIFIED_MACHINE: dict[str, Any] = {
    **BASE_MACHINE,
    "states": {
        **BASE_MACHINE["states"],
        "engage": {
            **BASE_MACHINE["states"]["engage"],
            "transitions": [{"when": "health_ratio.value < 0.65", "to": "hurt"}],
        },
    },
}


@dataclass
class Harness:
    """一次完整生成的端到端产物（模块级共享）。"""

    run: Any                                    # ScenarioRun（确定性帧序列）
    renderer: Renderer
    detectors: dict[str, Any]
    machine: CompiledMachine
    frame_store: FrameStore
    trace_path: Path
    events: list                                # 完整轨迹事件
    snapshots: list[PerceptionSnapshot]
    frame_refs: list[str]
    tick_results: list                          # 原运行逐 tick 决策
    first_target_frame: int
    first_hit1_frame: int
    first_hit2_frame: int

    @property
    def expected(self) -> ReplayResult:
        """原运行的三序列（对照基准）。"""
        ticks = self.tick_results
        stopped = ticks[-1].stopped
        return ReplayResult(
            state_seq=tuple(r.to_state if r.to_state is not None else r.from_state for r in ticks),
            intent_seq=tuple(tuple(ReplayIntent.from_intent(i) for i in r.emitted_intents) for r in ticks),
            hash_seq=tuple(r.decision_hash for r in ticks),
            stopped=stopped,
            stop_reason=ticks[-1].stop_reason if stopped else None,
            snapshot_seq=tuple(self.snapshots),
            frame_refs=tuple(self.frame_refs),
        )

    @property
    def detector_field_map(self) -> dict[str, str]:
        """字段名 -> 检测器 ID（差异报告标注检测器）。"""
        return {d.field_name: d.detector_id for d in self.detectors.values()}

    def read_trace(self) -> list:
        """从磁盘读取完整可信轨迹（端到端口径）。"""
        return JsonlTraceReader(self.trace_path).read()

    def fixed_replay(self, machine: CompiledMachine | None = None) -> ReplayResult:
        """固定感知回放（默认基线机器）。"""
        replayer = FixedPerceptionReplayer(lambda clock: MachineRuntime(machine or self.machine, clock))
        return replayer.run(load_snapshots(self.read_trace()), FakeClock())

    def raw_replay(self, machine: CompiledMachine | None = None) -> ReplayResult:
        """原始帧回放（重跑视觉管线）。"""
        replayer = RawFrameReplayer(
            lambda clock: MachineRuntime(machine or self.machine, clock),
            self.detectors,
            self.frame_store,
        )
        return replayer.run(self.read_trace(), FakeClock())


def _normalized(rect: Rect, width: int, height: int) -> list[float]:
    """像素矩形 -> 归一化 [x, y, w, h]。"""
    return [rect[0] / width, rect[1] / height, (rect[2] - rect[0]) / width, (rect[3] - rect[1]) / height]


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Harness:
    """端到端生成一次：渲染 -> 检测 -> 存帧 -> 写轨迹 -> 驱动状态机。"""
    root = tmp_path_factory.mktemp("replay_regression")
    run = run_scenario(
        "happy_path", SEED, resolution=RESOLUTION, fps=FPS, duration_s=DURATION_S, with_frames=True
    )
    renderer = Renderer(run.config)
    width, height = RESOLUTION

    # 检测器：血条（颜色条占比）+ 目标标记（颜色区域），ROI 直接取自渲染器布局。
    health_cfg = Detector(
        detector_id="health_bar",
        type="color_bar_ratio",
        roi=_normalized(renderer.health_bar_inner_rect(), width, height),
        threshold=0.2,
        stable_frames=1,
        field_name="health_ratio",
    )
    target_cfg = Detector(
        detector_id="target_marker",
        type="color_region",
        roi=_normalized(renderer.target_rect(), width, height),
        threshold=0.5,
        stable_frames=1,
        field_name="target",
    )
    detectors = {
        "health_bar": ColorBarRatioDetector(health_cfg, axis="horizontal"),
        "target_marker": ColorRegionDetector(target_cfg, ranges=TARGET_RGB_RANGES, color_space="rgb"),
    }
    machine = compile_machine_dict(BASE_MACHINE, known_fields=KNOWN_FIELDS, file="main.machine.yaml")

    frame_store = FrameStore(root / "frames")
    trace_path = root / "trace.jsonl"
    builder = PerceptionBuilder.from_detectors(list(detectors.values()))
    runtime = MachineRuntime(machine, FakeClock())

    snapshots: list[PerceptionSnapshot] = []
    frame_refs: list[str] = []
    tick_results: list = []
    events: list = []
    with JsonlTraceWriter(trace_path) as writer:
        for index, pixels in enumerate(run.frames):
            frame = Frame(
                pixels,
                FrameMeta(seq=index, ts_monotonic=index / FPS, adapter="arena",
                          source_width=width, source_height=height),
            )
            # 1) 视觉 -> 快照；2) 原始帧入库（内容寻址）；3) 按契约写事件；4) 驱动状态机。
            results = [detector.detect(frame) for detector in detectors.values()]
            snapshot = builder.build(results, frame_seq=index, ts_monotonic=index / FPS)
            frame_ref = frame_store.put(pixels)
            ts = index / FPS
            events.append(
                writer.append(
                    TraceEventType.PERCEPTION_SNAPSHOT,
                    ts_monotonic=ts, correlation_id=CID, session_id=SESSION,
                    payload=perception_payload(snapshot, frame_ref=frame_ref),
                )
            )
            snapshots.append(snapshot)
            frame_refs.append(frame_ref)
            tick = runtime.tick(snapshot)
            tick_results.append(tick)
            if tick.to_state is not None:
                events.append(
                    writer.append(
                        TraceEventType.STATE_TRANSITION,
                        ts_monotonic=ts, correlation_id=CID, session_id=SESSION,
                        payload={
                            "from": tick.from_state,
                            "to": tick.to_state,
                            "decision_hash": tick.decision_hash,
                        },
                    )
                )
            if tick.emitted_intents:
                events.append(
                    writer.append(
                        TraceEventType.INTENT_ISSUED,
                        ts_monotonic=ts, correlation_id=CID, session_id=SESSION,
                        payload={
                            "batch_id": f"batch-{index:04d}",
                            "intents": [
                                {"kind": i.kind, "payload": dict(i.payload), "cause": i.cause}
                                for i in tick.emitted_intents
                            ],
                        },
                    )
                )
            if tick.stopped:
                break

    return Harness(
        run=run,
        renderer=renderer,
        detectors=detectors,
        machine=machine,
        frame_store=frame_store,
        trace_path=trace_path,
        events=events,
        snapshots=snapshots,
        frame_refs=frame_refs,
        tick_results=tick_results,
        first_target_frame=next(i for i, s in enumerate(run.states) if s.target_present),
        first_hit1_frame=next(i for i, s in enumerate(run.states) if abs(s.health_ratio - 0.6) < 1e-9),
        first_hit2_frame=next(i for i, s in enumerate(run.states) if abs(s.health_ratio - 0.35) < 1e-9),
    )


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class TestFixedPerceptionReplay:
    def test_trace_chain_valid_and_load_snapshots_match(self, harness: Harness) -> None:
        """轨迹哈希链完整；快照重建与原运行逐字段一致（payload 契约）。"""
        events = harness.read_trace()
        assert verify(events) == []
        assert JsonlTraceReader(harness.trace_path).truncated_tail is False
        snapshots = load_snapshots(events)
        assert len(snapshots) == len(harness.snapshots)
        for rebuilt, original in zip(snapshots, harness.snapshots):
            assert rebuilt.ts_monotonic == original.ts_monotonic
            assert set(rebuilt.values) == set(original.values)
            for name, obs in original.values.items():
                got = rebuilt.values[name]
                assert (got.present, got.value) == (obs.present, obs.value)
                assert got.confidence == pytest.approx(obs.confidence)

    def test_fixed_replay_reproduces_original_run(self, harness: Harness) -> None:
        """TRC-005 / AC-P0-08：固定感知回放三序列与原运行完全一致。"""
        result = harness.fixed_replay()
        expected = harness.expected
        assert result.state_seq == expected.state_seq
        assert result.intent_seq == expected.intent_seq
        assert result.hash_seq == expected.hash_seq
        assert (result.stopped, result.stop_reason) == (expected.stopped, expected.stop_reason)
        # 场景走完 loading -> engage -> hurt -> recovered 并正常停止。
        assert result.stopped and result.stop_reason == "terminal"
        assert set(result.state_seq) == {"loading", "engage", "hurt", "recovered"}

    def test_state_transition_events_align_with_replay(self, harness: Harness) -> None:
        """契约交叉验证：state_transition 事件的 decision_hash/from/to 与回放对齐。"""
        result = harness.fixed_replay()
        tick = 0
        checked = 0
        for event in harness.read_trace():
            if event.type == TraceEventType.PERCEPTION_SNAPSHOT.value:
                tick += 1
                continue
            if event.type != TraceEventType.STATE_TRANSITION.value:
                continue
            assert tick >= 1
            payload = event.payload
            assert payload["decision_hash"] == result.hash_seq[tick - 1]
            assert payload["to"] == result.state_seq[tick - 1]
            previous = harness.machine.initial if tick == 1 else result.state_seq[tick - 2]
            assert payload["from"] == previous
            checked += 1
        assert checked == 3  # loading→engage / engage→hurt / hurt→recovered


class TestFrameStoreContentAddressing:
    def test_frame_refs_are_content_addressed_frame_hashes(self, harness: Harness) -> None:
        """TRC-003：frame_ref = sha256(帧字节)，与 arena_lab 逐帧哈希同口径。"""
        refs = harness.frame_refs
        assert refs == list(harness.run.frame_hashes[: len(refs)])
        assert len(set(refs)) == len(refs)  # 每帧内容唯一 -> 各自独立 blob
        for ref in refs:
            assert harness.frame_store.has(ref)
            stored = harness.frame_store.get(ref)
            assert stored.shape == (RESOLUTION[1], RESOLUTION[0], 3)
            assert stored.dtype == np.uint8


class TestRawFrameReplay:
    def test_raw_frame_replay_matches_original_run(self, harness: Harness) -> None:
        """TRC-006：重跑视觉后三序列仍与原运行一致（确定性证据）。"""
        result = harness.raw_replay()
        expected = harness.expected
        assert result.state_seq == expected.state_seq
        assert result.intent_seq == expected.intent_seq
        assert result.hash_seq == expected.hash_seq
        assert result.frame_refs == tuple(harness.frame_refs)
        assert result.stopped and result.stop_reason == "terminal"

    def test_tampered_frame_report_points_at_first_visual_divergence(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        """AC-P0-09（单测版）：篡改帧像素 -> 报告指出首个视觉分歧 tick/字段/检测器/受影响迁移。"""
        tamper_index = harness.first_target_frame  # 目标首次出现的帧
        tamper_tick = tamper_index + 1  # 回放 tick 从 1 起
        ref = harness.frame_refs[tamper_index]
        blob = harness.frame_store.blob_path(ref)
        original = np.load(blob)

        base_raw = harness.raw_replay()
        try:
            # 模拟视觉回归样本：目标区域被遮挡（涂成深灰 -> 检测器漏检）。
            x0, y0, x1, y1 = harness.renderer.target_rect()
            tampered = original.copy()
            tampered[y0:y1, x0:x1] = 30
            np.save(blob, tampered)

            tampered_raw = harness.raw_replay()
            # 状态后果：engage 迁移恰好被推迟一拍。
            assert base_raw.state_seq[tamper_index] == "engage"
            assert tampered_raw.state_seq[tamper_index] == "loading"
            assert tampered_raw.state_seq[tamper_index + 1] == "engage"

            diff = diff_replays(
                base_raw, tampered_raw, detector_ids=harness.detector_field_map
            )
            assert diff.first_divergence_tick == tamper_tick
            assert "perception" in diff.kinds and "state" in diff.kinds
            assert diff.severity == "error"
            first = diff.perception_diffs[0]
            assert (first.tick, first.field, first.detector_id) == (
                tamper_tick, "target", "target_marker"
            )
            assert first.before is not None and first.before["present"] is True
            assert first.after is not None and first.after["present"] is False
            assert "loading→engage" in diff.affected_transitions

            report = diff.to_report()
            assert f"首个分歧 tick: {tamper_tick}" in report
            assert "检测器 target_marker" in report
            assert ref in report  # 样本 frame_ref 被指出
            assert "loading→engage" in report
        finally:
            np.save(blob, original)  # 还原共享帧库，避免污染其他用例
        assert (harness.frame_store.get(ref) == original).all()


class TestGuardModificationDivergence:
    def test_guard_change_reports_state_intent_divergence_with_equal_perception(
        self, harness: Harness
    ) -> None:
        """TRC-006/007 交叉场景：感知一致、状态机守卫变化 -> 报告正确分类为状态/意图分歧。"""
        modified = compile_machine_dict(
            GUARD_MODIFIED_MACHINE, known_fields=KNOWN_FIELDS, file="modified.machine.yaml"
        )
        base = harness.expected
        modified_result = harness.fixed_replay(modified)
        diff = diff_replays(base, modified_result, detector_ids=harness.detector_field_map)

        # 感知完全一致：分歧只能来自决策侧。
        assert diff.perception_diffs == ()
        assert "state" in diff.kinds and "intent" in diff.kinds and "hash" in diff.kinds
        assert "perception" not in diff.kinds
        assert diff.severity == "error"
        # 两版机器都在治疗帧恢复并停止：长度一致，分歧窗口有限。
        assert diff.length_mismatch is False
        # 状态分歧窗口：基线在第二次受击（0.35<0.50）才进 hurt，改版在首次受击（0.60<0.65）
        # 已进入；到 hit2 帧两者同处 hurt，重新收敛。
        assert diff.state_divergent_ticks == tuple(
            range(harness.first_hit1_frame + 1, harness.first_hit2_frame + 1)
        )
        # 意图分歧仅在两侧各自触发 hurt entry（press_key h）的那两个 tick。
        assert diff.intent_divergent_ticks == (
            harness.first_hit1_frame + 1,
            harness.first_hit2_frame + 1,
        )
        # 首个分歧（哈希级）出现在两侧都处于 engage 并评估不同守卫文本的首个 tick。
        assert diff.first_divergence_tick == harness.first_target_frame + 2
        assert diff.kinds[0] == "hash"
        assert {"engage", "hurt"} <= set(diff.affected_states)
        assert {"key_down", "key_up"} <= set(diff.affected_intents)

        report = diff.to_report()
        assert "感知分歧: 无（视觉结果一致，分歧来自状态机/决策侧）" in report
        assert "严重度 error" in report
        assert "受影响状态: engage, hurt" in report


class TestCrashToleranceAndPrivacy:
    def test_truncated_trace_prefix_is_replayable(self, harness: Harness) -> None:
        """损坏 trace（截断）-> 最长可信前缀可读，重放前缀与原运行逐序列一致。"""
        lines = harness.trace_path.read_text(encoding="utf-8").splitlines()
        keep = 15  # 远早于停止点的截断位置
        partial = harness.trace_path.with_name("truncated.jsonl")
        partial.write_text("\n".join(lines[:keep]) + "\n" + lines[keep][:20], encoding="utf-8")

        reader = JsonlTraceReader(partial)
        prefix_events = reader.read()
        assert reader.truncated_tail is True
        assert verify(prefix_events) == []

        snapshots = load_snapshots(prefix_events)
        assert 0 < len(snapshots) < len(harness.snapshots)
        replayer = FixedPerceptionReplayer(lambda clock: MachineRuntime(harness.machine, clock))
        result = replayer.run(snapshots, FakeClock())

        expected = harness.expected
        assert len(result.state_seq) == len(snapshots)
        assert result.state_seq == expected.state_seq[: len(result.state_seq)]
        assert result.hash_seq == expected.hash_seq[: len(result.hash_seq)]
        assert result.intent_seq == expected.intent_seq[: len(result.intent_seq)]
        assert result.stopped is False  # 前缀未到终态

    def test_privacy_mode_trace_fixed_replay_works_raw_replay_rejected(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        """隐私模式：frame_ref 全空 -> 固定感知回放可用，原始帧回放明确拒绝。"""
        private_store = FrameStore(tmp_path / "private_frames", enabled=False)
        privacy_trace = tmp_path / "privacy_trace.jsonl"
        with JsonlTraceWriter(privacy_trace) as writer:
            for snapshot in harness.snapshots:
                writer.append(
                    TraceEventType.PERCEPTION_SNAPSHOT,
                    ts_monotonic=snapshot.ts_monotonic,
                    correlation_id=CID,
                    session_id=SESSION,
                    payload=perception_payload(snapshot, frame_ref=None),
                )
        assert private_store.put(np.zeros((2, 2, 3), dtype=np.uint8)) is None

        events = JsonlTraceReader(privacy_trace).read()
        replayer = FixedPerceptionReplayer(lambda clock: MachineRuntime(harness.machine, clock))
        result = replayer.run(load_snapshots(events), FakeClock())
        assert result.state_seq == harness.expected.state_seq
        assert result.hash_seq == harness.expected.hash_seq

        raw_replayer = RawFrameReplayer(
            lambda clock: MachineRuntime(harness.machine, clock),
            harness.detectors,
            private_store,
        )
        with pytest.raises(ValueError, match="frame_ref"):
            raw_replayer.run(events)
