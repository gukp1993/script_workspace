"""TST-004 IPC/序列化契约测试：冻结跨模块消息结构，防止无意识破坏。

契约测试与单测的区别：单测验证"行为正确"，契约测试验证"结构稳定"——
被冻结的键集合/字段集合/算法输出一旦变化必须显式更新本文件并走评审，
避免运行时引擎、回放器、控制面、前端任何一侧静默漂移。

冻结范围（固定消息样例 + 版本兼容）：

- 轨迹事件（TRC-001/002）：TRACE_FORMAT_VERSION 冻结为 "1"；8 类事件
  序列化 JSON 的键集合与字段类型快照；哈希链算法稳定性（同输入事件
  序列两次生成 -> 哈希序列逐位一致 + 黄金哈希常量）；不满足 v1 结构的
  旧版本/异构事件被 reader 明确标记（truncated_tail + errors），绝不静默；
- perception_snapshot payload（TRC-005）：``{"fields", "frame_ref", "ts"}``
  与字段条目 ``{"present", "confidence", "value"}`` 键集合冻结；构造
  payload -> ``load_snapshots`` 可读；缺键/类型非法 -> 明确 ValueError；
- 机器编译契约（FSM-002）：examples/arena_lab_demo 编译产物
  CompiledMachine 的状态/迁移表结构快照；Compiled* 数据类字段集合冻结；
  DSL 表达式 AST 节点类型枚举冻结（未知节点在求值期被拒绝）；
- 检测器契约（VIS-001）：DetectorResult 字段集合/类型冻结；
  DETECTOR_REGISTRY 五个 type 键冻结；
- 运行时对外结构（E07）：TickOutcome / RunSummary 字段集合冻结；
- 控制面事件（CTL-007）：WS/REST 事件消息键集合冻结
  ``{seq, type, payload, session_id, ts}``（公开构造 Event.to_dict 与
  TestClient 经 REST 兜底端点各断言一次）。
"""

from __future__ import annotations

import dataclasses
import json
import re
import tempfile
from pathlib import Path

import pytest

from trace_format import (
    GENESIS_HASH,
    TRACE_FORMAT_VERSION,
    JsonlTraceReader,
    JsonlTraceWriter,
    TraceEvent,
    TraceEventType,
    canonical_json,
    load_snapshots,
    perception_payload,
    verify,
)
from trace_format.events import EVENT_TYPE_VALUES

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "arena_lab_demo"

CID = "cid-contract"
SID = "sess-contract"

#: TraceEvent 序列化后的完整键集合（冻结：新增/删除/改名都算契约破坏）。
TRACE_EVENT_KEYS: frozenset[str] = frozenset(
    {"seq", "ts_monotonic", "correlation_id", "session_id", "type",
     "payload", "prev_hash", "hash"}
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# 固定消息样例：8 类事件各自的冻结 payload（构造输入一经写入不得变更）
# ---------------------------------------------------------------------------

_FIXED_PAYLOADS: dict[str, dict] = {
    "frame_captured": {
        "frame_seq": 0, "adapter": "fake", "source_width": 64, "source_height": 48,
    },
    "perception_snapshot": {
        "fields": {
            "health_ratio": {"present": True, "confidence": 0.9, "value": 0.42},
        },
        "frame_ref": None,
        "ts": 12.5,
    },
    "state_transition": {
        "from": "idle", "to": "running", "decision_hash": "a" * 64,
    },
    "intent_issued": {
        "batch_id": "batch-0001",
        "intents": [{"kind": "press_key", "payload": {"key": "e"}, "cause": "tick:3"}],
    },
    "policy_decision": {
        "batch_id": "batch-0001", "allow": False,
        "reasons": ["mode_shadow"], "mode": "shadow",
    },
    "executed": {
        "batch_id": "batch-0001", "accepted": 0, "rejected": 1, "real": 0,
    },
    "estop": {"trigger": "hotkey", "keys_released": ["e"]},
    "anomaly": {"error": "RuntimeError('injected')", "stage": "tick"},
}


def build_fixed_events() -> list[TraceEvent]:
    """按固定 payload 顺序构造 8 类事件的完整链（seq/prev_hash/hash 自动）。"""
    events: list[TraceEvent] = []
    prev = GENESIS_HASH
    for seq, (event_type, payload) in enumerate(_FIXED_PAYLOADS.items()):
        event = TraceEvent.create(
            seq=seq,
            prev_hash=prev,
            ts_monotonic=1.0 + seq,
            type=event_type,
            correlation_id=CID,
            session_id=SID,
            payload=payload,
        )
        events.append(event)
        prev = event.hash
    return events


# ---------------------------------------------------------------------------
# 轨迹事件契约（TRC-001/002）
# ---------------------------------------------------------------------------


def test_trace_format_version_and_event_types_frozen() -> None:
    """格式版本冻结为 "1"；8 类事件类型值集合冻结；创世哈希为 64 个 0。"""
    assert TRACE_FORMAT_VERSION == "1"
    assert GENESIS_HASH == "0" * 64
    assert set(EVENT_TYPE_VALUES) == set(_FIXED_PAYLOADS)  # 类型集合 = 样例覆盖集合
    assert {t.value for t in TraceEventType} == set(EVENT_TYPE_VALUES)


@pytest.mark.parametrize("event_type", sorted(_FIXED_PAYLOADS))
def test_trace_event_json_structure_snapshot(event_type: str) -> None:
    """每类事件的序列化 JSON 结构快照：键集合冻结 + 字段类型冻结 + JSON 往返。"""
    event = next(e for e in build_fixed_events() if e.type == event_type)
    data = event.to_dict()

    # 键集合冻结（多键少键都算破坏）
    assert set(data) == set(TRACE_EVENT_KEYS)
    # 字段类型冻结
    assert isinstance(data["seq"], int)
    assert isinstance(data["ts_monotonic"], float)
    assert isinstance(data["correlation_id"], str) and data["correlation_id"] == CID
    assert isinstance(data["session_id"], str) and data["session_id"] == SID
    assert data["type"] == event_type and isinstance(data["type"], str)
    assert isinstance(data["payload"], dict) and data["payload"] == _FIXED_PAYLOADS[event_type]
    assert isinstance(data["prev_hash"], str) and _SHA256_RE.match(data["prev_hash"])
    assert isinstance(data["hash"], str) and _SHA256_RE.match(data["hash"])

    # JSON 序列化往返稳定（写入 JSONL 的就是这行内容）
    assert json.loads(json.dumps(data, ensure_ascii=False, sort_keys=True)) == data


def test_hash_chain_algorithm_stability() -> None:
    """哈希链算法稳定性（FSM-012 契约前奏）：
    同一输入事件序列两次生成 -> 哈希序列逐位一致，且等于黄金常量
    （算法 = sha256(prev_hash + 规范化 JSON)，跨进程/跨版本不得漂移）。
    """
    first = [e.hash for e in build_fixed_events()]
    second = [e.hash for e in build_fixed_events()]
    assert first == second
    assert verify(build_fixed_events()) == []

    # 黄金哈希常量：固定 8 事件链的首事件与链尾 sha256 输出。
    # 变更哈希算法/规范化规则/固定样例内容必然破坏，须显式评审更新。
    assert first[0] == "a05870c2ad7a28efe0c59a8a3744774ecc0aa986ff48ffa9c04f548bc030863a"
    assert first[-1] == "0d837a56cec9519f6ec5a579196c70f1f47009b4328bbed0ff65543a8d5b16e5"

    # 规范化 JSON 的稳定性：键序无关（sort_keys）且分隔符紧凑
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_reader_rejects_incompatible_old_version_event(tmp_path: Path) -> None:
    """旧版本/异构事件被 reader 明确标记：缺 v1 必需键的行（假想 v0 写入器
    产物）与哈希断链的行都不进入可信前缀——truncated_tail=True + errors
    留痕，完好前缀原样保留，绝不静默接受。
    """
    def write_line(path: Path, data: dict) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")

    def write_good(path: Path) -> TraceEvent:
        with JsonlTraceWriter(path) as writer:
            return writer.append(
                TraceEventType.EXECUTED, ts_monotonic=1.0,
                correlation_id=CID, session_id=SID, payload={"batch_id": "b"},
            )

    # 场景一：旧版本格式行（缺 v1 必需键 "hash"）
    v0_path = tmp_path / "trace-v0.jsonl"
    good = write_good(v0_path)
    write_line(v0_path, {
        "seq": 1, "ts_monotonic": 2.0, "correlation_id": CID, "session_id": SID,
        "type": "state_transition", "payload": {"from": "idle", "to": "running"},
        "prev_hash": good.hash,
    })
    reader = JsonlTraceReader(v0_path)
    events = reader.read()
    assert [e.hash for e in events] == [good.hash]  # 可信前缀完好
    assert reader.truncated_tail is True
    assert len(reader.errors) == 1 and "line 2" in reader.errors[0]
    assert "hash" in reader.errors[0]

    # 场景二：内容被篡改的行（哈希不复算 -> 链校验失败）
    tampered_path = tmp_path / "trace-tampered.jsonl"
    good = write_good(tampered_path)
    tampered = good.to_dict()
    tampered["seq"] = 1
    tampered["payload"] = {"batch_id": "tampered"}
    write_line(tampered_path, tampered)
    reader = JsonlTraceReader(tampered_path)
    assert [e.hash for e in reader.read()] == [good.hash]
    assert reader.truncated_tail is True
    assert len(reader.errors) == 1 and "哈希链校验失败" in reader.errors[0]


# ---------------------------------------------------------------------------
# perception_snapshot payload 契约（TRC-005）
# ---------------------------------------------------------------------------


def test_perception_payload_structure_contract_roundtrip() -> None:
    """payload 键集合冻结 {fields, frame_ref, ts}；字段条目键冻结
    {present, confidence, value}；构造 payload -> load_snapshots 可读且
    frame_seq 按出现顺序重编号。
    """
    from domain_model import FieldObservation, PerceptionSnapshot

    snapshot = PerceptionSnapshot(
        frame_seq=7,  # 回放侧重编号后应为 0（旧值不进 payload）
        ts_monotonic=12.5,
        values={
            "health_ratio": FieldObservation(
                name="health_ratio", present=True, confidence=0.9, value=0.42),
            "ready": FieldObservation(
                name="ready", present=False, confidence=0.0, value=None),
        },
    )
    payload = perception_payload(snapshot, frame_ref="ab" * 32)

    assert set(payload) == {"fields", "frame_ref", "ts"}
    assert set(payload["fields"]) == {"health_ratio", "ready"}
    for entry in payload["fields"].values():
        assert set(entry) == {"present", "confidence", "value"}
    assert payload["frame_ref"] == "ab" * 32 and payload["ts"] == 12.5

    # 经轨迹事件封装 -> load_snapshots 重建（契约的另一侧消费方）
    event = TraceEvent.create(
        seq=0, prev_hash=GENESIS_HASH, ts_monotonic=12.6,
        type=TraceEventType.PERCEPTION_SNAPSHOT,
        correlation_id=CID, session_id=SID, payload=payload,
    )
    rebuilt = load_snapshots([event])
    assert len(rebuilt) == 1
    snap = rebuilt[0]
    assert snap.frame_seq == 0  # 按 perception 事件出现顺序重编号
    assert snap.ts_monotonic == 12.5
    assert set(snap.values) == {"health_ratio", "ready"}
    assert snap.values["health_ratio"].present is True
    assert snap.values["health_ratio"].confidence == pytest.approx(0.9)
    assert snap.values["health_ratio"].value == pytest.approx(0.42)
    assert snap.values["ready"].present is False and snap.values["ready"].value is None


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda p: p.pop("fields"), "缺少 fields 对象"),
        (lambda p: p["fields"].update(
            hp={"present": "yes", "confidence": 0.5, "value": 0.1}), "present 必须是布尔值"),
        (lambda p: p["fields"]["hp"].update(confidence=1.5), "confidence 必须在 0~1"),
        (lambda p: p["fields"]["hp"].update(value={"deep": 1}), "value 只允许数值/字符串/null"),
        (lambda p: p.pop("ts"), "ts 必须是数字"),
        (lambda p: p.update(frame_ref=123), "frame_ref 只允许字符串或 null"),
    ],
)
def test_perception_payload_missing_or_invalid_keys_rejected(
    tmp_path: Path, mutate, reason: str
) -> None:
    """缺键/类型非法 -> 明确 ValueError（带事件 seq），绝不静默吞掉。"""
    payload = {
        "fields": {"hp": {"present": True, "confidence": 0.9, "value": 0.42}},
        "frame_ref": None,
        "ts": 1.0,
    }
    mutate(payload)
    event = TraceEvent.create(
        seq=3, prev_hash=GENESIS_HASH, ts_monotonic=1.1,
        type=TraceEventType.PERCEPTION_SNAPSHOT,
        correlation_id=CID, session_id=SID, payload=payload,
    )
    with pytest.raises(ValueError, match=f"seq=3.*{reason}"):
        load_snapshots([event])


# ---------------------------------------------------------------------------
# 机器编译契约（FSM-002，examples/arena_lab_demo）
# ---------------------------------------------------------------------------


def test_arena_lab_demo_compiled_machine_snapshot() -> None:
    """示例项目编译产物结构快照：状态集合/初始态/迁移表（排序、条件原文、
    目标、指针）/超时/终态——编译器输出的"运行图"形状冻结。
    """
    from domain_model.parsing import load_project
    from state_machine.compiler import compile_project_machine

    machine = compile_project_machine(load_project(EXAMPLE), "main")
    assert machine.machine_id == "main"
    assert machine.initial == "idle"
    assert set(machine.states) == {
        "idle", "awaiting_manual_gate", "exercise", "stopped",
    }

    idle = machine.states["idle"]
    assert [t.when_src for t in idle.transitions] == ["ready.present"]
    tr = idle.transitions[0]
    assert (tr.source_state, tr.target) == ("idle", "awaiting_manual_gate")
    assert (tr.decl_index, tr.priority, tr.stable_frames) == (0, 0, 1)
    assert tr.on_timeout_to is None
    assert tr.pointer == "/states/idle/transitions/0"

    gate = machine.states["awaiting_manual_gate"]
    assert gate.timeout_seconds == 30.0 and gate.terminal is False
    assert [a.kind for a in gate.entry] == ["manual_gate"]
    assert [t.when_src for t in gate.transitions] == ["manual_gate.present"]
    assert (gate.transitions[0].target, gate.transitions[0].on_timeout_to) == (
        "exercise", "stopped")
    assert gate.timeout_transition is gate.transitions[0]

    exercise = machine.states["exercise"]
    assert [t.when_src for t in exercise.transitions] == ["health_ratio.value < 0.20"]
    assert exercise.transitions[0].target == "stopped"

    stopped = machine.states["stopped"]
    assert stopped.terminal is True and stopped.transitions == ()

    # 迁移表按 (priority, 声明顺序) 排序（确定性选择的前提）
    for state in machine.states.values():
        keys = [t.sort_key for t in state.transitions]
        assert keys == sorted(keys)


def test_compiled_structure_fields_frozen() -> None:
    """编译产物数据类字段集合冻结（CompiledMachine/State/Transition/Action/
    Assertion/RetrySpec）——新增字段须显式更新契约。
    """
    from domain_model.dsl import (
        CompiledAction,
        CompiledAssertion,
        CompiledMachine,
        CompiledState,
        CompiledTransition,
        RetrySpec,
    )

    frozen = {
        CompiledMachine: {"machine_id", "initial", "states", "source_file", "warnings"},
        CompiledState: {
            "name", "transitions", "entry", "exit", "timeout_seconds",
            "terminal", "retry_spec", "error_target", "timeout_transition", "pointer",
        },
        CompiledTransition: {
            "source_state", "decl_index", "when_src", "ast", "target",
            "on_timeout_to", "priority", "stable_frames", "pointer",
        },
        CompiledAction: {"kind", "params", "pointer"},
        CompiledAssertion: {
            "kind", "ast", "when_src", "timeout_seconds",
            "failure_state", "message", "pointer",
        },
        RetrySpec: {"max_attempts", "backoff", "base_seconds", "max_seconds"},
    }
    for cls, expected in frozen.items():
        assert {f.name for f in dataclasses.fields(cls)} == expected, cls.__name__
        assert dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_dsl_ast_node_enum_frozen_and_unknown_node_rejected() -> None:
    """DSL AST 节点类型枚举冻结（白名单语法的全部产物）；求值器对未知节点
    明确拒绝（TypeError），解析器对白名单外语法明确拒绝（ParseError）。
    """
    from domain_model import dsl

    node_names = set(_subclass_names(dsl.ExprAST))
    assert node_names == {"NumberLit", "BoolLit", "FieldRef", "UnaryOp", "BinOp"}

    # 结构快照：比较 + 字段引用 + 数字字面量
    ast = dsl.parse_expression("health_ratio.value < 0.20")
    assert isinstance(ast, dsl.BinOp) and ast.op == "<"
    assert isinstance(ast.left, dsl.FieldRef)
    assert (ast.left.name, ast.left.attr) == ("health_ratio", "value")
    assert isinstance(ast.right, dsl.NumberLit) and ast.right.value == pytest.approx(0.20)

    # 逻辑组合：not/and 与裸字段引用（attr=None）
    ast = dsl.parse_expression("not ready.present and gate.changed")
    assert isinstance(ast, dsl.BinOp) and ast.op == "and"
    assert isinstance(ast.left, dsl.UnaryOp) and ast.left.op == "not"
    assert isinstance(ast.right, dsl.FieldRef)
    assert (ast.right.name, ast.right.attr) == ("gate", "changed")

    # 白名单外语法在解析期拒绝（函数调用/字符串/多层属性链）
    for bad in ("f(1)", "'str'", "a.b.c", "import os"):
        with pytest.raises(dsl.ParseError):
            dsl.parse_expression(bad)

    # 未知 AST 节点（未来的/被篡改的节点类型）被求值器拒绝
    @dataclasses.dataclass(frozen=True)
    class GhostNode(dsl.ExprAST):
        payload: str = ""

    from domain_model.models import PerceptionSnapshot

    with pytest.raises(TypeError, match="未知 AST 节点"):
        dsl.ExprEval().evaluate(GhostNode(line=1, col=1), PerceptionSnapshot(0, 0.0, {}))


def _subclass_names(cls: type) -> set[str]:
    """递归收集子类名（防止未来把节点类型包一层中间基类而漏检）。"""
    names: set[str] = set()
    for sub in cls.__subclasses__():
        names.add(sub.__name__)
        names |= _subclass_names(sub)
    return names


# ---------------------------------------------------------------------------
# 检测器契约（VIS-001）
# ---------------------------------------------------------------------------


def test_detector_result_contract_frozen() -> None:
    """DetectorResult 字段集合/顺序/类型冻结；不可变（frozen dataclass）；
    统一错误构造 result_error 语义冻结（present=False、置信度 0、无值无框）。
    """
    from vision_core.base import DetectorResult, result_error

    assert [f.name for f in dataclasses.fields(DetectorResult)] == [
        "detector_id", "present", "confidence", "value",
        "bbox", "elapsed_ms", "version", "error",
    ]
    assert DetectorResult.__dataclass_params__.frozen is True  # type: ignore[attr-defined]

    result = DetectorResult(
        detector_id="health_bar", present=True, confidence=0.9, value=0.42,
        bbox=(1, 2, 3, 4), elapsed_ms=1.5, version="tmpl-1",
    )
    assert isinstance(result.detector_id, str)
    assert isinstance(result.present, bool)
    assert isinstance(result.confidence, float)
    assert isinstance(result.bbox, tuple) and all(isinstance(v, int) for v in result.bbox)
    assert isinstance(result.version, str) and result.error is None

    failed = result_error("gate_button", "template_missing", elapsed_ms=0.2, version="ocr-1")
    assert (failed.present, failed.confidence, failed.value, failed.bbox) == (
        False, 0.0, None, None)
    assert failed.error == "template_missing"


def test_detector_registry_type_keys_frozen() -> None:
    """DETECTOR_REGISTRY 五个 type 键冻结（模板/颜色条/颜色区域/变化稳定/OCR）；
    未注册类型在 create_detector 处明确拒绝。
    """
    from vision_core.registry import DETECTOR_REGISTRY, create_detector, registered_types

    expected = {
        "template_match", "color_bar_ratio", "color_region",
        "change_stability", "ocr_roi",
    }
    assert set(DETECTOR_REGISTRY) == expected
    assert registered_types() == tuple(sorted(expected))
    assert all(isinstance(v, type) for v in DETECTOR_REGISTRY.values())

    class _UnknownType:
        class type:  # noqa: A001 - 模拟 enum 成员
            value = "ghost_detector"

    with pytest.raises(KeyError, match="未注册的检测器类型"):
        create_detector(_UnknownType())


# ---------------------------------------------------------------------------
# 运行时对外结构契约（E07：runtime_engine）
# ---------------------------------------------------------------------------


def test_tick_outcome_and_run_summary_fields_frozen() -> None:
    """TickOutcome / RunSummary 字段集合冻结（引擎对外唯一结果结构，
    前端/回放/测试三方共同依赖）；FRAME_SEQ_NONE 占位值冻结。
    """
    from runtime_engine.engine import FRAME_SEQ_NONE, RunSummary, TickOutcome

    assert [f.name for f in dataclasses.fields(TickOutcome)] == [
        "frame_seq", "snapshot", "tick_result", "policy_decision", "sink_result",
    ]
    assert [f.name for f in dataclasses.fields(RunSummary)] == [
        "ticks", "states_visited", "intents_total", "real_executed",
        "stopped", "stop_reason", "wall_elapsed",
    ]
    assert FRAME_SEQ_NONE == -1
    for cls in (TickOutcome, RunSummary):
        assert cls.__dataclass_params__.frozen is True  # type: ignore[attr-defined]

    summary = RunSummary(
        ticks=3, states_visited=("idle", "running"), intents_total=1,
        real_executed=0, stopped=True, stop_reason="terminal", wall_elapsed=1.5,
    )
    assert isinstance(summary.ticks, int) and isinstance(summary.stopped, bool)
    assert isinstance(summary.states_visited, tuple)
    assert summary.stop_reason == "terminal"


# ---------------------------------------------------------------------------
# 控制面事件契约（CTL-007：WS/REST 消息键）
# ---------------------------------------------------------------------------


def test_control_plane_event_message_keys_frozen() -> None:
    """公开构造 Event.to_dict 的键集合冻结 {seq, type, payload, session_id, ts}；
    发布序号全局单调递增；payload 为结构化事实的浅拷贝。
    """
    pytest.importorskip("fastapi")
    from control_plane.events import EventBroker

    broker = EventBroker()
    first = broker.publish("session_started", {"state": "running"}, session_id=SID)
    second = broker.publish("session_stopped", {}, session_id=None)

    for event, seq in ((first, 1), (second, 2)):
        data = event.to_dict()
        assert set(data) == {"seq", "type", "payload", "session_id", "ts"}
        assert data["seq"] == seq  # 从 1 起单调递增
        assert isinstance(data["type"], str) and isinstance(data["payload"], dict)
        assert isinstance(data["ts"], str)  # UTC ISO-8601
    assert first.session_id == SID and second.session_id is None
    assert broker.latest_seq == 2


def test_control_plane_rest_events_emit_frozen_keys(tmp_path: Path) -> None:
    """经 TestClient 从 REST 兜底端点（WS 断线重连的兜底通道）收真实事件：
    事件信封键冻结，每条事件键集合与公开构造一致。
    """
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from control_plane.app import create_app
    from control_plane.config import ControlPlaneConfig

    token = "m2-contract-token"
    headers = {"X-VAW-Token": token}
    with tempfile.TemporaryDirectory() as td:
        config = ControlPlaneConfig(project_root=Path(td), token=token, port=17654)
        with TestClient(create_app(config), base_url="http://127.0.0.1:17654") as client:
            response = client.get("/api/v1/events?since_seq=0", headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert set(body) == {"events", "latest_seq", "resync_required"}
            assert body["events"], "lifespan 应至少发布一条 control_plane_started"
            for event in body["events"]:
                assert set(event) == {"seq", "type", "payload", "session_id", "ts"}
            assert any(e["type"] == "control_plane_started" for e in body["events"])
