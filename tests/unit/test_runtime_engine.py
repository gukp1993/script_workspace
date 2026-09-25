"""运行时引擎闭环单测（E07/E10，M2）。

覆盖：
- happy path：examples/arena_lab_demo 项目 + arena_lab 渲染合成帧序列，
  shadow 模式下 状态迁移 / 意图 / FakeInputSink 留痕 / 真实执行 0
  （SAFE-004 集成版），trace 契约事件齐全且哈希链 verify 通过；
- 模式语义：observe / dry_run 同样零真实输入；策略拒绝原因写入 trace；
- 错前台：foreground 不匹配 -> 每批 foreground_mismatch、real=0；
- 预算：max_actions_per_minute 小值 -> budget_exhausted 后按 stop 收尾；
- cancel / stop / pause / resume(verify) 控制语义；
- 确定性：同一帧序列 + 同种子两遍运行 -> 状态/意图/decision_hash 逐项一致；
- frame_store：启用时可回取像素、禁用时 frame_ref=None；
- 超时路径：FakeClock 推进 -> 进入 on_timeout_to 目标状态；
- factory：examples 项目全链路装配（含 ensure_compilable 前置门）；
- recovery：检测器抛异常后 close_and_finalize 仍执行、trace 前缀完好。

全部测试使用 Fake 设施（FakeCaptureSource / FakeClock / FakeInputSink /
脚本化检测器），绝不触碰真实桌面与真实输入。
"""

from __future__ import annotations

import json
import random
import shutil
from itertools import count
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from arena_lab.render import Renderer, SceneConfig, SceneState
from capture_api.fake import FakeCaptureSource
from capture_api.frames import Frame, FrameMeta
from common.clock import FakeClock
from domain_model import DomainValidationError, ProjectBundle
from domain_model.dsl import compile_machine_dict
from domain_model.parsing import load_project
from domain_model.static_analysis import ensure_compilable
from input_broker import InputBroker
from input_broker.fake_sink import FakeInputSink
from policy_engine import (
    ForegroundContext,
    ModeGate,
    PolicyEvaluator,
    PolicyInput,
    RunMode,
    TargetRef,
)
from state_machine.compiler import compile_project_machine
from test_kit.fakes import FakeForegroundContext
from trace_format import JsonlTraceReader, JsonlTraceWriter, TraceEventType, verify
from vision_core.base import DetectorResult

from runtime_engine import (
    FRAME_SEQ_NONE,
    FrameStore,
    SessionEngine,
    build_session,
    close_and_finalize,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "arena_lab_demo"
PROTECTED = REPO / "examples" / "protected_online_demo"

_TRACE_SEQ = count()  # 测试内轨迹文件唯一命名


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class ScriptedDetector:
    """按帧序号脚本化的假检测器（VIS-001 协议的结构化实现）。"""

    version: str = "fake-1.0"

    def __init__(
        self,
        detector_id: str,
        field_name: str,
        *,
        present_seq: Any = (),
        value_by_seq: dict[int, float | str] | None = None,
        default_value: float | str | None = None,
        raise_on_seq: Any = (),
    ) -> None:
        self.detector_id = detector_id
        self.field_name = field_name
        self._present = frozenset(present_seq)
        self._values: dict[int, float | str] = dict(value_by_seq or {})
        self._default = default_value
        self._raise = frozenset(raise_on_seq)

    def detect(self, frame: Frame, roi: Any = None) -> DetectorResult:
        seq = frame.meta.seq
        if seq in self._raise:
            raise RuntimeError(f"注入检测器异常: {self.detector_id}@seq={seq}")
        present = seq in self._present
        value = self._values.get(seq, self._default) if present else None
        return DetectorResult(
            detector_id=self.detector_id,
            present=present,
            confidence=0.95 if present else 0.0,
            value=value,
            bbox=None,
            elapsed_ms=0.0,
            version=self.version,
        )


#: 最小状态机：idle --go--> act（进入时按 e）--done--> end（终态）。
MINI_MACHINE: dict[str, Any] = {
    "schema_version": 1,
    "machine_id": "mini",
    "initial": "idle",
    "states": {
        "idle": {"transitions": [{"when": "go.present", "to": "act"}]},
        "act": {
            "entry": [{"kind": "press_key", "key": "e"}],
            "transitions": [{"when": "done.present", "to": "end"}],
        },
        "end": {"terminal": True},
    },
}

#: 预算场景状态机：one 进入按 e（1 动作），two 进入按 f（再 1 动作）。
BUDGET_MACHINE: dict[str, Any] = {
    "schema_version": 1,
    "machine_id": "budget",
    "initial": "idle",
    "states": {
        "idle": {"transitions": [{"when": "go.present", "to": "one"}]},
        "one": {
            "entry": [{"kind": "key_down", "key": "e"}],
            "transitions": [{"when": "mid.present", "to": "two"}],
        },
        "two": {"entry": [{"kind": "key_down", "key": "f"}], "terminal": True},
    },
}

#: 持键场景状态机：进入只 key_down 不 key_up（验证 stop 释放）。
HOLD_MACHINE: dict[str, Any] = {
    "schema_version": 1,
    "machine_id": "hold",
    "initial": "idle",
    "states": {
        "idle": {"transitions": [{"when": "go.present", "to": "act"}]},
        "act": {"entry": [{"kind": "key_down", "key": "e"}], "terminal": True},
    },
}


def make_renderer(seed: int = 7) -> Renderer:
    """确定性渲染器（小分辨率，测试提速）。"""
    return Renderer(SceneConfig(seed=seed, resolution=(320, 180)))


def make_frames(
    renderer: Renderer, clock: FakeClock, states: list[SceneState]
) -> list[Frame]:
    """把场景状态序列渲染为帧序列（seq 从 0 递增，ts 取注入时钟）。"""
    frames: list[Frame] = []
    for seq, state in enumerate(states):
        pixels = renderer.render(state)
        meta = FrameMeta(
            seq=seq,
            ts_monotonic=clock.now(),
            adapter="fake",
            source_width=pixels.shape[1],
            source_height=pixels.shape[0],
        )
        frames.append(Frame(pixels, meta))
    return frames


def scene(health_ratios: list[float]) -> list[SceneState]:
    """便捷构造：按血量比例列表生成场景状态（其余字段默认）。"""
    return [SceneState(health_ratio=r) for r in health_ratios]


def example_detectors() -> dict[str, ScriptedDetector]:
    """examples/arena_lab_demo 检测器清单对应的脚本化假检测器。"""
    return {
        "ready_button": ScriptedDetector("ready_button", "ready", present_seq={0}),
        "gate_button": ScriptedDetector("gate_button", "manual_gate", present_seq={2}),
        "health_bar": ScriptedDetector(
            "health_bar",
            "health_ratio",
            present_seq={3, 4},
            value_by_seq={3: 0.9, 4: 0.1},
            default_value=0.5,
        ),
    }


def mini_detectors() -> dict[str, ScriptedDetector]:
    """MINI_MACHINE 需要的 go / done 字段检测器（按帧序脚本化命中）。"""
    return {
        "det_go": ScriptedDetector("det_go", "go", present_seq={0, 1, 2}),
        "det_done": ScriptedDetector("det_done", "done", present_seq={1, 2}),
    }


def make_engine(
    tmp_path: Path,
    frames: list[Frame],
    *,
    clock: FakeClock | None = None,
    machine_dict: dict[str, Any] | None = None,
    detectors: dict[str, ScriptedDetector] | None = None,
    mode: str | RunMode = "shadow",
    session_id: str = "sess-test",
    policy_input: PolicyInput | None = None,
    gate_confirm: bool = False,
    foreground: ForegroundContext | None = None,
    frame_store: FrameStore | None = None,
    max_ticks: int | None = None,
    rng_seed: int = 42,
) -> tuple[SessionEngine, FakeInputSink, FakeClock]:
    """装配一个全 Fake 的 SessionEngine（返回 engine 与 sink 便于断言）。"""
    clock = clock or FakeClock()
    bundle = ProjectBundle(
        root=tmp_path, project={"name": "test", "description": "", "notes": ""}
    )
    machine = compile_machine_dict(machine_dict or MINI_MACHINE, file="test.machine.yaml")
    detector_map = detectors if detectors is not None else mini_detectors()
    mode_value = str(getattr(mode, "value", mode))
    policy = PolicyEvaluator(
        policy_input or PolicyInput(mode=mode_value),
        gate=_confirmed_gate(clock, session_id) if gate_confirm else None,
    )
    sink = FakeInputSink(clock=clock)
    broker = InputBroker(sink, clock=clock)
    broker.mode = mode_value
    trace = JsonlTraceWriter(tmp_path / f"trace-{next(_TRACE_SEQ)}.jsonl")
    engine = SessionEngine(
        project=bundle,
        machine=machine,
        detectors=detector_map,
        policy=policy,
        broker=broker,
        capture=FakeCaptureSource(frames),
        trace=trace,
        clock=clock,
        mode=mode,
        target=TargetRef(target_id="mini-target"),
        foreground=foreground,
        frame_store=frame_store,
        max_ticks=max_ticks,
        session_id=session_id,
        rng=random.Random(rng_seed),
    )
    return engine, sink, clock


def make_example_engine(
    tmp_path: Path,
    frames: list[Frame],
    *,
    clock: FakeClock | None = None,
    mode: str | RunMode = "shadow",
    detectors: dict[str, ScriptedDetector] | None = None,
    frame_store: FrameStore | None = None,
    rng_seed: int = 42,
) -> tuple[SessionEngine, FakeInputSink, FakeClock]:
    """基于 examples/arena_lab_demo 项目的全 Fake 会话引擎。"""
    clock = clock or FakeClock()
    bundle = load_project(EXAMPLE)
    machine = compile_project_machine(bundle, "main")
    mode_value = str(getattr(mode, "value", mode))
    policy = PolicyEvaluator(PolicyInput(mode=mode_value))
    sink = FakeInputSink(clock=clock)
    broker = InputBroker(sink, clock=clock)
    broker.mode = mode_value
    trace = JsonlTraceWriter(tmp_path / f"trace-{next(_TRACE_SEQ)}.jsonl")
    engine = SessionEngine(
        project=bundle,
        machine=machine,
        detectors=detectors if detectors is not None else example_detectors(),
        policy=policy,
        broker=broker,
        capture=FakeCaptureSource(frames),
        trace=trace,
        clock=clock,
        mode=mode,
        target=TargetRef(
            target_id="arena-lab",
            protected_online=False,
            title_regex="^ArenaLab - Training$",
        ),
        frame_store=frame_store,
        session_id="sess-example",
        rng=random.Random(rng_seed),
    )
    return engine, sink, clock


def _confirmed_gate(clock: FakeClock, session_id: str) -> ModeGate:
    """构造并确认 real_input 人工闸门。"""
    gate = ModeGate(clock=clock)
    assert gate.confirm(session_id, "operator") is not None
    return gate


def make_matching_foreground(session_id: str = "sess-test") -> FakeForegroundContext:
    """与 mini-target 会话绑定一致的前台上下文。"""
    fg = FakeForegroundContext()
    fg.focus("mini-target", pid=1234)
    fg.bind(target_id="mini-target", pid=1234, session_id=session_id)
    return fg


def drive_happy(engine: SessionEngine, clock: FakeClock) -> list[Any]:
    """手工驱动 happy path：ready -> 闸门 -> 放行 -> exercise -> 血量低 -> 终态。"""
    outcomes: list[Any] = [engine.run_tick()]
    clock.advance(0.1)
    outcomes.append(engine.run_tick())
    clock.advance(0.1)
    assert engine.approve_manual_gate() is True
    for _ in range(4):
        outcomes.append(engine.run_tick())
        clock.advance(0.1)
    return outcomes


def start_happy(engine: SessionEngine) -> None:
    """setup + 前两帧 + 人工闸门放行（此后 run() 可一路跑到终态）。"""
    engine.setup()
    engine.run_tick()  # idle -> awaiting_manual_gate（闸门挂起）
    engine.run_tick()  # 闸门挂起帧：无迁移
    assert engine.approve_manual_gate() is True


def read_events(engine: SessionEngine) -> list[Any]:
    """读取引擎轨迹文件的完好前缀。"""
    return JsonlTraceReader(engine.trace.path).read()


def events_of_type(events: list[Any], event_type: TraceEventType) -> list[Any]:
    """按类型过滤轨迹事件。"""
    return [e for e in events if e.type == event_type.value]


# ---------------------------------------------------------------------------
# 基础行为
# ---------------------------------------------------------------------------


class TestBasics:
    def test_setup_starts_capture_and_sets_broker_mode(self, tmp_path: Path) -> None:
        """setup：启动采集、同步模式到 Broker、绑定 correlation。"""
        frames = make_frames(make_renderer(), FakeClock(), scene([1.0]))
        engine, _, _ = make_engine(tmp_path, frames, mode="shadow")
        engine.setup()
        assert engine.capture.diagnostics()["started"] is True  # type: ignore[union-attr]
        assert engine.broker.mode == "shadow"
        assert engine.correlation_id == "cid-sess-test"

    def test_run_tick_skips_when_no_frame(self, tmp_path: Path) -> None:
        """grab() 返回 None -> 跳过本帧并计数，不产生任何感知/决策。"""
        engine, _, _ = make_engine(tmp_path, [], mode="shadow")
        engine.setup()
        outcome = engine.run_tick()
        assert outcome.frame_seq == FRAME_SEQ_NONE
        assert outcome.snapshot is None
        assert outcome.tick_result is None
        assert outcome.policy_decision is None
        assert outcome.sink_result is None
        assert engine.skipped_frames == 1
        assert engine.intents_total == 0
        assert read_events(engine) == []

    def test_all_events_share_correlation_id(self, tmp_path: Path) -> None:
        """全部轨迹事件携带同一 correlation_id 与 session_id。"""
        clock = FakeClock()
        frames = make_frames(make_renderer(), clock, scene([1.0] * 6))
        engine, _, _ = make_example_engine(tmp_path, frames, clock=clock)
        start_happy(engine)
        engine.run()
        events = read_events(engine)
        assert events
        assert {e.correlation_id for e in events} == {engine.correlation_id}
        assert {e.session_id for e in events} == {engine.session_id}


# ---------------------------------------------------------------------------
# happy path（examples/arena_lab_demo + shadow）
# ---------------------------------------------------------------------------


class TestHappyPathShadow:
    def _frames(self, clock: FakeClock) -> list[Frame]:
        return make_frames(
            make_renderer(), clock, scene([1.0, 0.95, 0.9, 0.85, 0.1, 0.05])
        )

    def test_happy_path_state_sequence_and_intents(self, tmp_path: Path) -> None:
        """手工驱动：状态迁移、wait 意图、闸门放行、影子留痕、真实执行 0。"""
        clock = FakeClock()
        engine, sink, _ = make_example_engine(
            tmp_path, self._frames(clock), clock=clock
        )
        engine.setup()
        outcomes = drive_happy(engine, clock)

        # 状态序列：idle -> awaiting_manual_gate -> exercise -> stopped。
        assert engine.states_visited == (
            "idle",
            "awaiting_manual_gate",
            "exercise",
            "stopped",
        )
        # t0：进入 awaiting_manual_gate，manual_gate 动作产生一个 wait 意图。
        assert outcomes[0].tick_result.to_state == "awaiting_manual_gate"
        assert outcomes[0].policy_decision is not None
        assert outcomes[0].policy_decision.allow is False
        assert outcomes[0].sink_result is not None
        assert outcomes[0].sink_result.accepted_intents == []
        assert outcomes[0].sink_result.rejected[0].reason.startswith("mode_not_allowed")
        # t1：闸门挂起 -> 无迁移；放行后 t2 进入 exercise。
        assert outcomes[1].tick_result.to_state is None
        assert engine.manual_gate_pending is False
        assert outcomes[2].tick_result.to_state == "exercise"
        # t4：health_ratio.value < 0.20 -> stopped。
        assert outcomes[4].tick_result.to_state == "stopped"
        assert outcomes[-1].frame_seq == 5
        # 影子留痕存在，真实执行为 0（SAFE-004 集成版）。
        assert sink.shadow_recorded_count == 1
        assert sink.real_executed_count == 0
        assert sink.accepted_intents() == []
        assert engine.intents_total == 1

    def test_happy_path_run_loop_summary(self, tmp_path: Path) -> None:
        """run() 循环到终态：摘要计数与状态序列正确。"""
        clock = FakeClock()
        engine, sink, _ = make_example_engine(
            tmp_path, self._frames(clock), clock=clock
        )
        start_happy(engine)
        summary = engine.run()
        assert summary.stopped is True
        assert summary.stop_reason == "terminal"
        assert summary.ticks == 6
        assert summary.intents_total == 1
        assert summary.real_executed == 0
        assert summary.states_visited == (
            "idle",
            "awaiting_manual_gate",
            "exercise",
            "stopped",
        )
        assert summary.wall_elapsed >= 0.0
        assert sink.real_executed_count == 0

    def test_shadow_zero_real_input(self, tmp_path: Path) -> None:
        """shadow 模式：策略与 Broker 双层拒绝，真实输入恒为 0。"""
        clock = FakeClock()
        engine, sink, _ = make_example_engine(
            tmp_path, self._frames(clock), clock=clock
        )
        start_happy(engine)
        engine.run()
        assert engine.mode is RunMode.SHADOW
        assert engine.broker.mode == "shadow"
        assert sink.real_executed_count == 0
        assert sink.shadow_recorded_count == 1
        assert engine.broker.key_ledger.pressed == frozenset()

    def test_trace_contains_all_contract_event_types(self, tmp_path: Path) -> None:
        """trace 五类事件齐全且数量与闭环一一对应。"""
        clock = FakeClock()
        engine, _, _ = make_example_engine(
            tmp_path, self._frames(clock), clock=clock
        )
        start_happy(engine)
        engine.run()
        events = read_events(engine)
        types = [TraceEventType(e.type) for e in events]
        assert types.count(TraceEventType.PERCEPTION_SNAPSHOT) == 6
        assert types.count(TraceEventType.STATE_TRANSITION) == 6
        assert types.count(TraceEventType.INTENT_ISSUED) == 1
        assert types.count(TraceEventType.POLICY_DECISION) == 1
        assert types.count(TraceEventType.EXECUTED) == 1

    def test_trace_hash_chain_verifies(self, tmp_path: Path) -> None:
        """落盘轨迹可整读且哈希链 verify 通过（无截断）。"""
        clock = FakeClock()
        engine, _, _ = make_example_engine(
            tmp_path, self._frames(clock), clock=clock
        )
        start_happy(engine)
        engine.run()
        reader = JsonlTraceReader(engine.trace.path)
        events = reader.read()
        assert reader.truncated_tail is False
        assert reader.errors == []
        assert verify(events) == []
        assert len(events) == 15

    def test_perception_snapshot_payload_contract(self, tmp_path: Path) -> None:
        """perception_snapshot payload 契约：fields/frame_ref/ts。"""
        clock = FakeClock()
        engine, _, _ = make_example_engine(
            tmp_path, self._frames(clock), clock=clock
        )
        start_happy(engine)
        engine.run()
        events = events_of_type(
            read_events(engine), TraceEventType.PERCEPTION_SNAPSHOT
        )
        first = events[0]
        assert set(first.payload) == {"fields", "frame_ref", "ts"}
        assert isinstance(first.payload["ts"], float)
        assert first.payload["frame_ref"] is None  # frame_store 禁用
        fields = first.payload["fields"]
        assert set(fields) == {"manual_gate", "health_ratio", "ready"}
        assert set(fields["ready"]) == {"present", "confidence", "value"}
        assert fields["ready"] == {"present": True, "confidence": 0.95, "value": None}
        assert fields["health_ratio"]["present"] is False

    def test_state_transition_payload_contract(self, tmp_path: Path) -> None:
        """state_transition payload 契约：from/to/decision_hash。"""
        clock = FakeClock()
        engine, _, _ = make_example_engine(
            tmp_path, self._frames(clock), clock=clock
        )
        start_happy(engine)
        engine.run()
        events = events_of_type(read_events(engine), TraceEventType.STATE_TRANSITION)
        assert set(events[0].payload) == {"from", "to", "decision_hash"}
        assert (events[0].payload["from"], events[0].payload["to"]) == (
            "idle",
            "awaiting_manual_gate",
        )
        assert len(events[0].payload["decision_hash"]) == 64
        # 无迁移 tick 也逐帧留痕（exercise 驻留帧）。
        assert events[3].payload["from"] == "exercise"
        assert events[3].payload["to"] is None

    def test_intent_issued_payload_contract(self, tmp_path: Path) -> None:
        """intent_issued payload 契约：batch_id + intents[{kind,payload,cause}]。"""
        clock = FakeClock()
        engine, _, _ = make_example_engine(
            tmp_path, self._frames(clock), clock=clock
        )
        start_happy(engine)
        engine.run()
        events = events_of_type(read_events(engine), TraceEventType.INTENT_ISSUED)
        assert len(events) == 1
        payload = events[0].payload
        assert set(payload) == {"batch_id", "intents"}
        assert payload["batch_id"].startswith("batch-")
        assert payload["intents"] == [
            {
                "kind": "wait",
                "payload": {"duration_ms": 0},
                "cause": "manual_gate:确认开始本地测试",
            }
        ]

    def test_executed_payload_contract(self, tmp_path: Path) -> None:
        """executed payload 契约：batch_id/accepted/rejected/real。"""
        clock = FakeClock()
        engine, _, _ = make_example_engine(
            tmp_path, self._frames(clock), clock=clock
        )
        start_happy(engine)
        engine.run()
        events = events_of_type(read_events(engine), TraceEventType.EXECUTED)
        assert len(events) == 1
        assert set(events[0].payload) == {"batch_id", "accepted", "rejected", "real"}
        assert events[0].payload["accepted"] == 0
        assert events[0].payload["rejected"] == 1
        assert events[0].payload["real"] == 0

    def test_policy_deny_reasons_written_to_trace(self, tmp_path: Path) -> None:
        """策略拒绝原因（mode_not_allowed）写入 policy_decision 事件。"""
        clock = FakeClock()
        engine, _, _ = make_example_engine(
            tmp_path, self._frames(clock), clock=clock
        )
        start_happy(engine)
        engine.run()
        events = events_of_type(read_events(engine), TraceEventType.POLICY_DECISION)
        assert len(events) == 1
        assert events[0].payload["allow"] is False
        assert "mode_not_allowed" in events[0].payload["reasons"]
        assert events[0].payload["mode"] == "shadow"

    def test_observe_mode_zero_real_input(self, tmp_path: Path) -> None:
        """observe 模式：同样零真实输入且影子留痕。"""
        clock = FakeClock()
        frames = make_frames(make_renderer(), clock, scene([1.0] * 6))
        engine, sink, _ = make_example_engine(
            tmp_path, frames, clock=clock, mode="observe"
        )
        start_happy(engine)
        summary = engine.run()
        assert summary.real_executed == 0
        assert summary.intents_total == 1
        assert sink.real_executed_count == 0
        assert sink.shadow_recorded_count == 1
        executed = events_of_type(read_events(engine), TraceEventType.EXECUTED)
        assert all(e.payload["real"] == 0 for e in executed)

    def test_dry_run_mode_zero_real_input(self, tmp_path: Path) -> None:
        """dry_run 模式：同样零真实输入且影子留痕。"""
        clock = FakeClock()
        frames = make_frames(make_renderer(), clock, scene([1.0] * 6))
        engine, sink, _ = make_example_engine(
            tmp_path, frames, clock=clock, mode="dry_run"
        )
        start_happy(engine)
        summary = engine.run()
        assert summary.real_executed == 0
        assert sink.real_executed_count == 0
        assert sink.shadow_recorded_count == 1
        executed = events_of_type(read_events(engine), TraceEventType.EXECUTED)
        assert all(e.payload["real"] == 0 for e in executed)


# ---------------------------------------------------------------------------
# 前台 / 预算 / 真实输入
# ---------------------------------------------------------------------------


class TestForegroundAndBudget:
    def _real_engine(
        self,
        tmp_path: Path,
        clock: FakeClock,
        *,
        foreground: ForegroundContext | None = None,
        policy_input: PolicyInput | None = None,
        machine_dict: dict[str, Any] | None = None,
    ) -> tuple[SessionEngine, FakeInputSink]:
        frames = make_frames(make_renderer(), clock, scene([1.0, 0.9, 0.8]))
        engine, sink, _ = make_engine(
            tmp_path,
            frames,
            clock=clock,
            mode="real_input",
            machine_dict=machine_dict,
            gate_confirm=True,
            foreground=foreground if foreground is not None else make_matching_foreground(),
            policy_input=policy_input,
        )
        return engine, sink

    def test_real_input_executes_when_allowed(self, tmp_path: Path) -> None:
        """正向对照：real_input + 闸门 + 前台一致 -> 真实执行发生。"""
        engine, sink = self._real_engine(tmp_path, FakeClock())
        engine.setup()
        summary = engine.run()
        assert summary.real_executed == 2
        assert summary.stop_reason == "terminal"
        assert sink.real_executed_count == 2
        assert [i.kind for i in sink.accepted_intents()] == ["key_down", "key_up"]
        executed = events_of_type(read_events(engine), TraceEventType.EXECUTED)
        assert len(executed) == 1
        assert executed[0].payload["accepted"] == 2
        assert executed[0].payload["rejected"] == 0
        assert executed[0].payload["real"] == 2
        # key_down + key_up 配对执行后无残留按下键。
        assert engine.broker.key_ledger.pressed == frozenset()

    def test_foreground_mismatch_zero_real(self, tmp_path: Path) -> None:
        """错前台：每批 foreground_mismatch、零真实输入、sink 无执行记录。"""
        fg = FakeForegroundContext()
        fg.focus("other-app", pid=999)
        fg.bind(target_id="mini-target", pid=1234, session_id="sess-test")
        engine, sink = self._real_engine(tmp_path, FakeClock(), foreground=fg)
        engine.setup()
        summary = engine.run()
        assert summary.real_executed == 0
        assert summary.intents_total == 2
        assert sink.records == []  # real_input 拒绝路径不留影子记录
        policy_events = events_of_type(
            read_events(engine), TraceEventType.POLICY_DECISION
        )
        assert policy_events
        assert all(
            "foreground_mismatch" in e.payload["reasons"] for e in policy_events
        )
        executed = events_of_type(read_events(engine), TraceEventType.EXECUTED)
        assert all(
            e.payload["real"] == 0 and e.payload["accepted"] == 0 for e in executed
        )

    def test_budget_exhausted_stops_session(self, tmp_path: Path) -> None:
        """max_actions_per_minute=1 -> 第二批 budget_exhausted 后按 stop 收尾。"""
        clock = FakeClock()
        frames = make_frames(make_renderer(), clock, scene([1.0, 0.9, 0.8]))
        engine, sink, _ = make_engine(
            tmp_path,
            frames,
            clock=clock,
            mode="real_input",
            machine_dict=BUDGET_MACHINE,
            detectors={
                "det_go": ScriptedDetector("det_go", "go", present_seq={0}),
                "det_mid": ScriptedDetector("det_mid", "mid", present_seq={1}),
            },
            gate_confirm=True,
            foreground=make_matching_foreground(),
            policy_input=PolicyInput(
                mode="real_input", max_actions_per_minute=1, max_total_actions=10
            ),
        )
        engine.setup()
        summary = engine.run()
        assert summary.stopped is True
        assert summary.stop_reason == "budget_exhausted:actions_per_minute"
        assert summary.ticks == 2
        assert summary.real_executed == 1  # 第一批（key_down e）放行
        assert sink.real_executed_count == 1
        reasons = [
            e.payload["reasons"]
            for e in events_of_type(
                read_events(engine), TraceEventType.POLICY_DECISION
            )
        ]
        assert [] in reasons
        assert any("budget_exhausted:actions_per_minute" in r for r in reasons)


# ---------------------------------------------------------------------------
# 控制语义：cancel / stop / pause / resume
# ---------------------------------------------------------------------------


class TestControlSemantics:
    def test_cancel_before_run_no_ticks_no_intents(self, tmp_path: Path) -> None:
        """cancel 后 run() 直接以 cancelled 收尾，不产生任何意图或事件。"""
        frames = make_frames(make_renderer(), FakeClock(), scene([1.0] * 3))
        engine, _, _ = make_engine(tmp_path, frames, mode="shadow")
        engine.setup()
        engine.cancel()
        summary = engine.run()
        assert summary.stopped is True
        assert summary.stop_reason == "cancelled"
        assert summary.ticks == 0
        assert engine.intents_total == 0
        assert engine.broker.cancelled is True
        assert read_events(engine) == []

    def test_cancel_midrun_no_new_intents(self, tmp_path: Path) -> None:
        """cancel 后 run_tick 短路：不抓帧、不产意图、不写事件。"""
        frames = make_frames(make_renderer(), FakeClock(), scene([1.0] * 4))
        engine, sink, _ = make_engine(tmp_path, frames, mode="shadow")
        engine.setup()
        engine.run(max_ticks=1)
        assert engine.intents_total == 2  # act 进入产生 key_down + key_up
        engine.cancel()
        outcome = engine.run_tick()
        assert outcome.frame_seq == FRAME_SEQ_NONE
        assert outcome.snapshot is None
        assert engine.intents_total == 2  # 无新意图
        events_before = len(read_events(engine))
        engine.run_tick()
        assert len(read_events(engine)) == events_before  # 无新事件
        summary = engine.run()
        assert summary.stop_reason == "cancelled"
        assert sink.real_executed_count == 0

    def test_stop_idempotent_and_releases_keys(self, tmp_path: Path) -> None:
        """stop() 幂等并经 Broker 释放仍按下的键（补偿 key_up）。"""
        clock = FakeClock()
        frames = make_frames(make_renderer(), clock, scene([1.0, 0.9]))
        engine, sink, _ = make_engine(
            tmp_path,
            frames,
            clock=clock,
            mode="real_input",
            machine_dict=HOLD_MACHINE,
            detectors={
                "det_go": ScriptedDetector("det_go", "go", present_seq={0, 1}),
            },
            gate_confirm=True,
            foreground=make_matching_foreground(),
        )
        engine.setup()
        engine.run(max_ticks=1)
        assert "e" in engine.broker.key_ledger.pressed
        assert "e" in sink.pressed
        summary = engine.stop()
        assert summary.stopped is True
        assert summary.stop_reason == "stopped"
        assert engine.broker.key_ledger.pressed == frozenset()
        assert sink.pressed == frozenset()  # 补偿 key_up 已执行
        ups = [i for i in sink.accepted_intents() if i.kind == "key_up"]
        assert [i.payload["key"] for i in ups] == ["e"]
        # 幂等：重复 stop 返回同一摘要，无额外副作用；tick 短路。
        assert engine.stop() == summary
        assert engine.run_tick().snapshot is None
        assert engine.is_stopped is True

    def test_pause_suppresses_intents_until_verified_resume(self, tmp_path: Path) -> None:
        """pause 后 tick 不产意图；resume(verify=False) 拒绝；True 恢复。"""
        frames = make_frames(make_renderer(), FakeClock(), scene([1.0] * 3))
        engine, _, _ = make_engine(tmp_path, frames, mode="shadow")
        engine.setup()
        engine.pause()
        outcome = engine.run_tick()
        assert outcome.snapshot is not None  # 感知留痕继续
        assert outcome.tick_result is not None
        assert outcome.tick_result.to_state is None
        assert engine.intents_total == 0
        # verify=False -> 拒绝恢复并保持暂停。
        with pytest.raises(RuntimeError, match="verify"):
            engine.resume(verify=lambda: False)
        outcome2 = engine.run_tick()
        assert engine.intents_total == 0
        assert outcome2.tick_result is not None
        assert outcome2.tick_result.to_state is None
        # verify=True -> 恢复后闭环继续产意图。
        engine.resume(verify=lambda: True)
        outcome3 = engine.run_tick()
        assert engine.intents_total == 2
        assert outcome3.tick_result is not None
        assert outcome3.tick_result.to_state == "act"

    def test_resume_requires_verify_callable(self, tmp_path: Path) -> None:
        """resume 必须传入 verify；缺省直接拒绝。"""
        frames = make_frames(make_renderer(), FakeClock(), scene([1.0]))
        engine, _, _ = make_engine(tmp_path, frames, mode="shadow")
        engine.setup()
        engine.pause()
        with pytest.raises(RuntimeError, match="verify"):
            engine.resume()
        # 未恢复：run 立即以 paused 返回，不烧 tick。
        summary = engine.run()
        assert summary.stopped is False
        assert summary.stop_reason == "paused"
        assert summary.ticks == 0


# ---------------------------------------------------------------------------
# 确定性（FSM-009 / AC-P0-08 引擎侧）
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_frames_same_seed_identical_sequences(self, tmp_path: Path) -> None:
        """同一帧序列 + 同种子两遍运行：状态/意图/decision_hash 逐项一致。"""

        def run_once() -> dict[str, Any]:
            clock = FakeClock()
            frames = make_frames(
                make_renderer(seed=2024),
                clock,
                scene([1.0, 0.95, 0.9, 0.85, 0.1, 0.05]),
            )
            engine, sink, _ = make_example_engine(
                tmp_path, frames, clock=clock, rng_seed=42
            )
            engine.setup()
            drive_happy(engine, clock)
            events = read_events(engine)
            return {
                "states": [
                    e.payload["to"]
                    for e in events_of_type(events, TraceEventType.STATE_TRANSITION)
                ],
                "hashes": [
                    e.payload["decision_hash"]
                    for e in events_of_type(events, TraceEventType.STATE_TRANSITION)
                ],
                "intents": [
                    (i["kind"], json.dumps(i["payload"], sort_keys=True), i["cause"])
                    for e in events_of_type(events, TraceEventType.INTENT_ISSUED)
                    for i in e.payload["intents"]
                ],
                "fields": [
                    json.dumps(e.payload["fields"], sort_keys=True)
                    for e in events_of_type(events, TraceEventType.PERCEPTION_SNAPSHOT)
                ],
                "shadow_count": sink.shadow_recorded_count,
                "real_executed": sink.real_executed_count,
            }

        first = run_once()
        second = run_once()
        assert first == second
        assert first["states"] == [
            "awaiting_manual_gate",
            None,
            "exercise",
            None,
            "stopped",
            None,
        ]
        assert len(first["hashes"]) == 6
        assert len(set(first["hashes"])) == 6  # 不同 tick 决策哈希互不相同
        assert len(first["intents"]) == 1


# ---------------------------------------------------------------------------
# frame_store
# ---------------------------------------------------------------------------


class TestFrameStore:
    def test_frame_store_roundtrip_pixels(self, tmp_path: Path) -> None:
        """启用 frame_store：frame_ref 写入事件，且可回取原像素。"""
        store = FrameStore(tmp_path / "frames", max_frames=4)
        frames = make_frames(make_renderer(), FakeClock(), scene([1.0, 0.9]))
        engine, _, _ = make_engine(
            tmp_path, frames, mode="shadow", frame_store=store
        )
        engine.setup()
        engine.run(max_ticks=1)
        events = events_of_type(
            read_events(engine), TraceEventType.PERCEPTION_SNAPSHOT
        )
        ref = events[0].payload["frame_ref"]
        assert isinstance(ref, str) and len(ref) == 64
        restored = store.get(ref)
        assert restored is not None
        assert np.array_equal(restored, frames[0].pixels)
        # 未命中引用返回 None。
        assert store.get("0" * 64) is None

    def test_frame_store_disabled_frame_ref_none(self, tmp_path: Path) -> None:
        """禁用 frame_store：所有 perception 事件 frame_ref=None。"""
        frames = make_frames(make_renderer(), FakeClock(), scene([1.0, 0.9]))
        engine, _, _ = make_engine(
            tmp_path, frames, mode="shadow", frame_store=None
        )
        engine.setup()
        engine.run(max_ticks=2)
        events = events_of_type(
            read_events(engine), TraceEventType.PERCEPTION_SNAPSHOT
        )
        assert len(events) == 2
        assert all(e.payload["frame_ref"] is None for e in events)


# ---------------------------------------------------------------------------
# 超时路径（FSM-005）
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_state_timeout_routes_to_on_timeout_target(self, tmp_path: Path) -> None:
        """awaiting_manual_gate 超时 30s -> FakeClock 推进 -> on_timeout_to stopped。"""
        clock = FakeClock()
        frames = make_frames(make_renderer(), clock, scene([1.0, 1.0, 1.0]))
        engine, sink, _ = make_example_engine(tmp_path, frames, clock=clock)
        engine.setup()
        engine.run_tick()  # idle -> awaiting_manual_gate（manual_gate 动作 -> wait 意图）
        assert engine.state == "awaiting_manual_gate"
        assert engine.intents_total == 1
        clock.advance(30.0)  # 超时时刻到达
        outcome = engine.run_tick()  # 闸门挂起但超时是安全出口
        assert outcome.tick_result is not None
        assert outcome.tick_result.to_state == "stopped"
        assert engine.states_visited == ("idle", "awaiting_manual_gate", "stopped")
        outcome2 = engine.run_tick()  # 终态
        assert outcome2.tick_result is not None
        assert outcome2.tick_result.stopped is True
        assert outcome2.tick_result.stop_reason == "terminal"
        assert sink.real_executed_count == 0


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


@pytest.fixture()
def unicode_safe_imread(monkeypatch: pytest.MonkeyPatch) -> None:
    """cv2.imread 不支持非 ASCII 路径（既有缺陷，单独记录）——
    测试环境用户目录含中文，统一替换为 np.fromfile + imdecode 的等价读取。
    """
    import cv2

    from detector_opencv import template_match as tm

    def _imread(path: Any, flags: int) -> Any:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, flags)

    monkeypatch.setattr(tm.cv2, "imread", _imread)


class TestFactory:
    def test_factory_full_chain_on_example_project(
        self, tmp_path: Path, unicode_safe_imread: None
    ) -> None:
        """examples 项目 build_session 全链路可跑通（含 ensure_compilable 前置）。"""
        issues = ensure_compilable(EXAMPLE)  # 前置门显式验证
        assert isinstance(issues, list)
        clock = FakeClock()
        frames = make_frames(make_renderer(), clock, scene([1.0, 0.9]))
        engine = build_session(
            EXAMPLE,
            mode="shadow",
            clock=clock,
            capture=FakeCaptureSource(frames),
            sink=FakeInputSink(clock=clock),
            trace_path=tmp_path / "factory-trace.jsonl",
            max_ticks=2,
        )
        assert engine.target.target_id == "arena-lab"
        engine.setup()
        summary = engine.run()
        assert summary.ticks == 2
        assert summary.stopped is False
        assert summary.real_executed == 0
        events = JsonlTraceReader(tmp_path / "factory-trace.jsonl").read()
        assert len(events) >= 2
        assert verify(events) == []

    def test_factory_rejects_protected_project(self, tmp_path: Path) -> None:
        """受保护在线反例项目在 ensure_compilable 前置门被拒绝。"""
        with pytest.raises(DomainValidationError):
            build_session(
                PROTECTED,
                mode="shadow",
                clock=FakeClock(),
                capture=FakeCaptureSource([]),
                sink=FakeInputSink(clock=FakeClock()),
                trace_path=tmp_path / "no.jsonl",
            )

    def test_factory_trace_defaults_to_project_traces_dir(
        self, tmp_path: Path, unicode_safe_imread: None
    ) -> None:
        """缺省轨迹写入 <项目>/traces/ 目录（在临时副本上验证）。"""
        project_copy = tmp_path / "arena_lab_demo"
        shutil.copytree(EXAMPLE, project_copy)
        engine = build_session(
            project_copy,
            mode="observe",
            clock=FakeClock(),
            capture=FakeCaptureSource([]),
            sink=FakeInputSink(clock=FakeClock()),
        )
        assert engine.trace.path.parent == project_copy / "traces"
        assert engine.trace.path.suffix == ".jsonl"


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_detector_exception_isolated_and_run_bounded(self, tmp_path: Path) -> None:
        """检测器抛异常 -> 调度器隔离为 error 结果，运行不中断（防御性隔离）。

        契约（VIS-001）：检测器运行期失败走 error 结果而非上抛；单个检测器
        故障不得拖垮感知循环。
        """
        clock = FakeClock()
        frames = make_frames(make_renderer(), clock, scene([1.0, 0.9, 0.8, 0.7]))
        detectors = example_detectors()
        detectors["health_bar"] = ScriptedDetector(
            "health_bar",
            "health_ratio",
            present_seq={3, 4},
            value_by_seq={3: 0.9, 4: 0.1},
            raise_on_seq={2},  # t2 注入异常，其余 tick 正常
        )
        engine, sink, _ = make_example_engine(
            tmp_path, frames, clock=clock, detectors=detectors
        )
        engine.setup()
        summary = engine.run(max_ticks=8)  # 不抛异常、有界收尾
        close_and_finalize(engine)
        assert summary.ticks >= 4  # 异常 tick 之后运行仍在推进
        assert summary.real_executed == 0  # shadow 模式真实输入恒 0
        assert verify(read_events(engine)) == []  # 轨迹哈希链完好

    def test_close_and_finalize_after_capture_exception(self, tmp_path: Path) -> None:
        """采集层异常 -> run 原样上抛；close_and_finalize 仍执行且轨迹完好。"""
        clock = FakeClock()
        frames = make_frames(make_renderer(), clock, scene([1.0, 0.9]))
        engine, sink, _ = make_engine(tmp_path, frames, clock=clock)
        engine.capture.set_fail(RuntimeError("注入采集异常"))
        engine.setup()
        with pytest.raises(RuntimeError, match="注入采集异常"):
            try:
                engine.run()
            finally:
                close_and_finalize(engine)
        # 资源全部释放：采集已停、Broker 已停、无残留按键、引擎已置停止态。
        assert engine.capture.diagnostics()["started"] is False  # type: ignore[union-attr]
        assert engine.broker.cancelled is True
        assert engine.broker.key_ledger.pressed == frozenset()
        assert engine.is_stopped is True
        # 异常前写入的轨迹前缀完好（anomaly 事件收尾）。
        reader = JsonlTraceReader(engine.trace.path)
        events = reader.read()
        assert reader.truncated_tail is False
        assert verify(events) == []
        assert events[-1].type == TraceEventType.ANOMALY.value

    def test_close_and_finalize_idempotent(self, tmp_path: Path) -> None:
        """close_and_finalize 可安全重复调用。"""
        frames = make_frames(make_renderer(), FakeClock(), scene([1.0]))
        engine, _, _ = make_engine(tmp_path, frames, mode="shadow")
        engine.setup()
        engine.run_tick()
        close_and_finalize(engine)
        close_and_finalize(engine)  # 不抛异常
        assert verify(read_events(engine)) == []
