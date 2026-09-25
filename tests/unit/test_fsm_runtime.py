"""确定性状态机运行时单测（FSM-002~010）。

覆盖：
- examples/arena_lab_demo/main.machine.yaml 的编译与全链路场景
  （ready -> awaiting_manual_gate -> approve -> exercise 产生按键意图 ->
  health 低 -> stopped）；
- 人工闸门、状态超时（FakeClock 推进）、迁移级 stable_frames；
- pause / resume(verify) / cancel 控制语义（FSM-006）；
- 有界重试 + 退避 + 异常路由（FSM-008）；
- assert_after / wait_until 断言与失败证据（FSM-007）；
- priority 与声明顺序的确定性选择（FSM-003）；
- 同轨迹 100 次哈希完全一致（FSM-009 / AC-P0-08）；
- 覆盖率计数（FSM-010）。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest
import yaml

from common.clock import FakeClock
from domain_model import (
    DomainValidationError,
    FieldObservation,
    PerceptionSnapshot,
    compile_machine_dict,
    compile_machine_yaml,
)
from domain_model.dsl import CompiledMachine
from input_broker.intents import InputIntent
from state_machine import MachineRuntime

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "arena_lab_demo"


# ---------------------------------------------------------------------------
# 夹具辅助
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


def load_example_dict() -> dict[str, Any]:
    """读取示例状态机 YAML 为 dict。"""
    return yaml.safe_load((EXAMPLE / "machines" / "main.machine.yaml").read_text(encoding="utf-8"))


def compile_extended_arena() -> CompiledMachine:
    """示例状态机 + exercise 进入时按键（用于验证动作 -> 意图链路）。"""
    data = load_example_dict()
    data["states"]["exercise"]["entry"] = [{"kind": "press_key", "key": "e"}]
    return compile_machine_dict(data, file="main.machine.yaml")


def full_scenario(machine: CompiledMachine, clock: FakeClock) -> list:
    """跑完整场景：ready -> 闸门 -> approve -> exercise 按键 -> health 低 -> stopped。"""
    rt = MachineRuntime(machine, clock, rng=random.Random(42))
    results = [
        rt.tick(snap(0, 0.0, ready=(True, None))),
        rt.tick(snap(1, 0.1, ready=(True, None), manual_gate=(True, None))),
    ]
    rt.approve_manual_gate()
    results.append(rt.tick(snap(2, 0.2, manual_gate=(True, None))))
    results.append(rt.tick(snap(3, 0.3, health_ratio=(True, 0.9))))
    results.append(rt.tick(snap(4, 0.4, health_ratio=(True, 0.1))))
    return rt, results


def simple_machine(**overrides) -> dict[str, Any]:
    """a --go.present--> b（b 终态）的最小状态机 dict。"""
    data: dict[str, Any] = {
        "schema_version": 1,
        "machine_id": "m1",
        "initial": "a",
        "states": {
            "a": {"transitions": [{"when": "go.present", "to": "b"}]},
            "b": {"terminal": True},
        },
    }
    data.update(overrides)
    return data


class IntentRecorder:
    """on_intent 回调记录器。"""

    def __init__(self) -> None:
        self.intents: list[InputIntent] = []

    def __call__(self, intent: InputIntent) -> None:
        self.intents.append(intent)


# ---------------------------------------------------------------------------
# 编译与基础行为（FSM-002/003）
# ---------------------------------------------------------------------------


class TestCompileAndBasics:
    def test_example_machine_compiles_clean(self) -> None:
        """示例状态机零告警编译。"""
        machine = compile_machine_yaml(EXAMPLE / "machines" / "main.machine.yaml")
        assert machine.warnings == ()
        assert machine.initial == "idle"

    def test_runtime_rejects_non_compiled_machine(self) -> None:
        with pytest.raises(TypeError):
            MachineRuntime({"not": "compiled"}, FakeClock())  # type: ignore[arg-type]

    def test_initial_state_and_not_terminal(self) -> None:
        rt = MachineRuntime(compile_extended_arena(), FakeClock())
        assert rt.state() == "idle"
        assert rt.is_terminal() is False

    def test_declaration_order_deterministic(self) -> None:
        """无 priority：声明顺序优先（第一条被选中）。"""
        data = {
            "schema_version": 1, "machine_id": "ord", "initial": "a",
            "states": {
                "a": {"transitions": [{"when": "ready.present", "to": "first"},
                                       {"when": "ready.present or other.present", "to": "second"}]},
                "first": {"terminal": True},
                "second": {"terminal": True},
            },
        }
        rt = MachineRuntime(compile_machine_dict(data), FakeClock())
        assert rt.tick(snap(0, 0.0, ready=(True, None))).to_state == "first"

    def test_priority_overrides_declaration_order(self) -> None:
        """显式 priority（数值小者优先）覆盖声明顺序。"""
        data = {
            "schema_version": 1, "machine_id": "prio", "initial": "a",
            "states": {
                "a": {"transitions": [{"when": "ready.present", "to": "first"},
                                       {"when": "ready.present or other.present",
                                        "to": "second", "priority": -1}]},
                "first": {"terminal": True},
                "second": {"terminal": True},
            },
        }
        rt = MachineRuntime(compile_machine_dict(data), FakeClock())
        assert rt.tick(snap(0, 0.0, ready=(True, None))).to_state == "second"


# ---------------------------------------------------------------------------
# 示例状态机全链路 + 人工闸门 + 超时（FSM-004/005）
# ---------------------------------------------------------------------------


class TestArenaScenario:
    def test_full_scenario_with_intent(self) -> None:
        """ready -> awaiting_manual_gate -> approve -> exercise 按键 -> health 低 -> stopped。"""
        recorder = IntentRecorder()
        machine = compile_extended_arena()
        rt = MachineRuntime(machine, FakeClock(), on_intent=recorder)
        r1 = rt.tick(snap(0, 0.0, ready=(True, None)))
        assert (r1.from_state, r1.to_state) == ("idle", "awaiting_manual_gate")
        # 进入 awaiting_manual_gate：entry 的 manual_gate 产生 wait 意图
        assert [i.kind for i in r1.emitted_intents] == ["wait"]
        assert r1.emitted_intents[0].cause.startswith("manual_gate:")
        r2 = rt.tick(snap(1, 0.1, ready=(True, None), manual_gate=(True, None)))
        assert r2.to_state is None  # 闸门未放行，迁移被抑制
        rt.approve_manual_gate()
        r3 = rt.tick(snap(2, 0.2, manual_gate=(True, None)))
        assert r3.to_state == "exercise"
        # exercise 的 entry 产生按键意图（只构造不执行）
        assert [i.kind for i in r3.emitted_intents] == ["key_down", "key_up"]
        assert all(i.cause == "exercise" for i in r3.emitted_intents)
        assert all(i.payload["key"] == "e" for i in r3.emitted_intents)
        r4 = rt.tick(snap(3, 0.3, health_ratio=(True, 0.9)))
        assert r4.to_state is None  # 血量健康，无迁移
        r5 = rt.tick(snap(4, 0.4, health_ratio=(True, 0.1)))
        assert r5.to_state == "stopped"
        assert rt.is_terminal() is True
        # 回调顺序与结果一致；意图从未被执行，只有构造记录
        kinds = [i.kind for i in recorder.intents]
        assert kinds == ["wait", "key_down", "key_up"]

    def test_manual_gate_blocks_without_approval(self) -> None:
        """感知到闸门按钮但未 approve：迁移被抑制。"""
        rt = MachineRuntime(compile_extended_arena(), FakeClock())
        rt.tick(snap(0, 0.0, ready=(True, None)))
        assert rt.manual_gate_pending is True
        r = rt.tick(snap(1, 0.1, manual_gate=(True, None)))
        assert r.to_state is None and rt.state() == "awaiting_manual_gate"
        # 未等待时 approve 返回 False；等待时返回 True
        rt2 = MachineRuntime(compile_extended_arena(), FakeClock())
        assert rt2.approve_manual_gate() is False
        assert rt.approve_manual_gate() is True
        assert rt.manual_gate_pending is False

    def test_gate_state_timeout_escapes(self) -> None:
        """闸门失联：状态超时（FakeClock 推进 30s）兜底迁入 stopped。"""
        clock = FakeClock()
        rt = MachineRuntime(compile_extended_arena(), clock)
        rt.tick(snap(0, 0.0, ready=(True, None)))
        clock.advance(29.9)
        r = rt.tick(snap(1, 29.9))
        assert r.to_state is None  # 未到超时
        clock.advance(0.2)
        r2 = rt.tick(snap(2, 30.1))
        assert r2.to_state == "stopped"  # on_timeout_to 目标
        assert rt.is_terminal() is True
        assert rt.manual_gate_pending is False

    def test_timeout_transition_on_simple_machine(self) -> None:
        """通用超时：timeout_seconds + on_timeout_to。"""
        data = simple_machine()
        data["states"]["a"]["timeout_seconds"] = 5
        data["states"]["a"]["transitions"][0]["on_timeout_to"] = "b"
        clock = FakeClock()
        rt = MachineRuntime(compile_machine_dict(data), clock)
        assert rt.tick(snap(0, 0.0)).to_state is None
        assert rt.tick(snap(1, 0.1)).to_state is None
        clock.advance(5.0)
        assert rt.tick(snap(2, 5.1)).to_state == "b"

    def test_missing_field_defaults_false_keeps_state(self) -> None:
        """字段缺失按 present=False 求值：快照为空时不迁移。"""
        rt = MachineRuntime(compile_extended_arena(), FakeClock())
        r = rt.tick(snap(0, 0.0))  # 空 snapshot
        assert r.to_state is None
        assert r.guards_evaluated == ("ready.present",)


# ---------------------------------------------------------------------------
# 迁移级 stable_frames（FSM-003）
# ---------------------------------------------------------------------------


class TestStableFrames:
    def _machine(self, frames: int) -> CompiledMachine:
        data = simple_machine()
        data["machine_id"] = f"stab{frames}"
        data["states"]["a"]["transitions"][0]["stable_frames"] = frames
        return compile_machine_dict(data)

    def test_stable_frames_gating(self) -> None:
        """连续 N 个 tick 满足才迁移。"""
        rt = MachineRuntime(self._machine(3), FakeClock())
        assert rt.tick(snap(0, 0.0, go=(True, None))).to_state is None
        assert rt.tick(snap(1, 0.1, go=(True, None))).to_state is None
        assert rt.tick(snap(2, 0.2, go=(True, None))).to_state == "b"

    def test_stable_frames_reset_on_false(self) -> None:
        """中间一帧不满足 -> 计数清零重新累计。"""
        rt = MachineRuntime(self._machine(2), FakeClock())
        assert rt.tick(snap(0, 0.0, go=(True, None))).to_state is None
        assert rt.tick(snap(1, 0.1, go=(False, None))).to_state is None  # 重置
        assert rt.tick(snap(2, 0.2, go=(True, None))).to_state is None   # 重新计数 1
        assert rt.tick(snap(3, 0.3, go=(True, None))).to_state == "b"    # 计数 2

    def test_counter_cleared_after_leaving_state(self) -> None:
        """离开状态后稳定计数清空（回到 a 需重新累计）。"""
        data = {
            "schema_version": 1, "machine_id": "cycle", "initial": "a",
            "states": {
                "a": {"transitions": [{"when": "go.present", "to": "b", "stable_frames": 2},
                                       {"when": "back.present", "to": "a"}]},
                "b": {"transitions": [{"when": "back.present", "to": "a"}]},
            },
        }
        machine = compile_machine_dict(data)
        rt = MachineRuntime(machine, FakeClock())
        assert rt.tick(snap(0, 0.0, go=(True, None))).to_state is None      # 计数 1
        assert rt.tick(snap(1, 0.1, go=(True, None))).to_state == "b"       # 计数 2 -> 迁移
        assert rt.tick(snap(2, 0.2, back=(True, None))).to_state == "a"     # 返回 a
        # 回到 a 后计数已清零：需要重新连续 2 帧
        assert rt.tick(snap(3, 0.3, go=(True, None))).to_state is None
        assert rt.tick(snap(4, 0.4, go=(True, None))).to_state == "b"


# ---------------------------------------------------------------------------
# 运行控制（FSM-006）
# ---------------------------------------------------------------------------


class TestControl:
    def _runtime(self) -> MachineRuntime:
        return MachineRuntime(compile_machine_dict(simple_machine()), FakeClock())

    def test_pause_blocks_transition(self) -> None:
        rt = self._runtime()
        rt.pause()
        r = rt.tick(snap(0, 0.0, go=(True, None)))
        assert r.to_state is None and rt.state() == "a" and r.stopped is False
        rt.resume()
        assert rt.tick(snap(1, 0.1, go=(True, None))).to_state == "b"

    def test_resume_verify_refusal(self) -> None:
        rt = MachineRuntime(
            compile_machine_dict(simple_machine()), FakeClock(), verify=lambda: False
        )
        rt.pause()
        with pytest.raises(RuntimeError, match="verify"):
            rt.resume()
        assert rt.state() == "a"
        # verify 拒绝期间 tick 仍然不迁移
        assert rt.tick(snap(0, 0.0, go=(True, None))).to_state is None

    def test_resume_after_verify_ok(self) -> None:
        checked: list[bool] = []

        def verify() -> bool:
            checked.append(True)
            return True

        rt = MachineRuntime(compile_machine_dict(simple_machine()), FakeClock(), verify=verify)
        rt.pause()
        rt.resume()
        assert checked == [True]
        assert rt.tick(snap(0, 0.0, go=(True, None))).to_state == "b"

    def test_cancel_semantics(self) -> None:
        """取消后 tick 只推进：不迁移、不产意图、恒 stopped。"""
        recorder = IntentRecorder()
        rt = MachineRuntime(compile_extended_arena(), FakeClock(), on_intent=recorder)
        rt.cancel()
        r = rt.tick(snap(0, 0.0, ready=(True, None)))
        assert r.stopped is True and r.stop_reason == "cancelled"
        assert r.to_state is None and r.emitted_intents == ()
        assert recorder.intents == []
        assert rt.tick(snap(1, 0.1, ready=(True, None))).stopped is True
        # 取消后不能再 pause/resume
        with pytest.raises(RuntimeError):
            rt.resume()

    def test_tick_in_terminal_state(self) -> None:
        """终态上 tick：stopped=True / stop_reason=terminal。"""
        rt = MachineRuntime(compile_machine_dict(simple_machine()), FakeClock())
        assert rt.tick(snap(0, 0.0, go=(True, None))).to_state == "b"
        r = rt.tick(snap(1, 0.1))
        assert r.stopped is True and r.stop_reason == "terminal"


# ---------------------------------------------------------------------------
# 重试与异常路由（FSM-008）
# ---------------------------------------------------------------------------


class TestRetry:
    def _retry_machine(self, *, attempts: int, base_ms: int) -> CompiledMachine:
        data = {
            "schema_version": 1, "machine_id": "retry", "initial": "flaky",
            "states": {
                "flaky": {
                    # press_key 缺 key -> 每次构造意图时抛 ValueError
                    "entry": [
                        {"kind": "press_key"},
                        {"kind": "retry", "max_attempts": attempts, "base_ms": base_ms},
                        {"kind": "on_error_to", "to": "failed"},
                    ],
                    "transitions": [{"when": "false", "to": "failed"}],
                },
                "failed": {"terminal": True},
            },
        }
        return compile_machine_dict(data)

    def test_retry_exhaustion_routes_to_error_state(self) -> None:
        """超过 max_attempts -> 路由到 on_error_to 状态（绝不无限重试）。"""
        clock = FakeClock()
        rt = MachineRuntime(self._retry_machine(attempts=2, base_ms=1000), clock)
        r1 = rt.tick(snap(0, 0.0))  # 第一次尝试失败
        assert r1.to_state is None and rt.state() == "flaky"
        clock.advance(1.0)
        r2 = rt.tick(snap(1, 1.0))  # 第二次尝试失败 -> 耗尽 -> 路由
        assert r2.to_state == "failed"
        assert rt.is_terminal() is True
        assert rt.coverage()["exception_paths"] == {"flaky": 2}

    def test_retry_backoff_waits_on_clock(self) -> None:
        """退避期内 tick 不重试（基于注入 Clock，无真实 sleep）。"""
        clock = FakeClock()
        rt = MachineRuntime(self._retry_machine(attempts=3, base_ms=1000), clock)
        rt.tick(snap(0, 0.0))
        r = rt.tick(snap(1, 0.0))  # 退避未到：无新尝试
        assert r.to_state is None and rt.coverage()["exception_paths"] == {"flaky": 1}
        clock.advance(0.5)
        rt.tick(snap(2, 0.5))  # 仍未到
        assert rt.coverage()["exception_paths"] == {"flaky": 1}
        clock.advance(0.5)
        rt.tick(snap(3, 1.0))  # 到点：第二次尝试
        assert rt.coverage()["exception_paths"] == {"flaky": 2}

    def test_exponential_backoff_grows(self) -> None:
        """指数退避按 2^n 增长且封顶。"""
        from state_machine import RetrySpec, backoff_seconds

        spec = RetrySpec(max_attempts=10, backoff="exponential", base_seconds=1.0, max_seconds=5.0)
        assert backoff_seconds(spec, 1) == 1.0
        assert backoff_seconds(spec, 2) == 2.0
        assert backoff_seconds(spec, 3) == 4.0
        assert backoff_seconds(spec, 4) == 5.0  # 封顶
        fixed = RetrySpec(max_attempts=3, backoff="fixed", base_seconds=0.5, max_seconds=5.0)
        assert backoff_seconds(fixed, 3) == 0.5

    def test_no_retry_policy_routes_immediately(self) -> None:
        """无 retry 策略：entry 失败立即安全停止（无路由目标时）。"""
        data = {
            "schema_version": 1, "machine_id": "naf", "initial": "bad",
            "states": {
                "bad": {
                    "entry": [{"kind": "press_key"}],
                    "transitions": [{"when": "false", "to": "halted"}],
                    "timeout_seconds": 99,
                },
                "halted": {"terminal": True},
            },
        }
        rt = MachineRuntime(compile_machine_dict(data), FakeClock())
        r = rt.tick(snap(0, 0.0))
        assert r.stopped is True and r.stop_reason == "error"
        assert r.to_state is None
        assert rt.coverage()["exception_paths"] == {"bad": 1}


# ---------------------------------------------------------------------------
# 断言（FSM-007）
# ---------------------------------------------------------------------------


class TestAssertions:
    def _assert_machine(self, kind: str) -> CompiledMachine:
        data = {
            "schema_version": 1, "machine_id": f"assert_{kind}", "initial": "check",
            "states": {
                "check": {
                    "entry": [{"kind": kind, "when": "ready.present",
                               "timeout_seconds": 2, "to": "recovery", "message": "等待就绪"}],
                    "transitions": [{"when": "false", "to": "recovery"}],
                },
                "recovery": {"terminal": True},
            },
        }
        return compile_machine_dict(data)

    def test_assert_after_failure_routes_and_keeps_evidence(self) -> None:
        """assert_after 超时失败 -> 迁入出口状态 + 保留快照证据。"""
        clock = FakeClock()
        rt = MachineRuntime(self._assert_machine("assert_after"), clock)
        rt.tick(snap(0, 0.0))  # 登记，未超时
        assert rt.state() == "check"
        clock.advance(2.0)
        r = rt.tick(snap(1, 2.0))
        assert r.to_state == "recovery"
        failure = rt.last_assert_failure
        assert failure is not None
        assert failure["when"] == "ready.present"
        assert failure["failure_state"] == "recovery"
        assert failure["elapsed_seconds"] == pytest.approx(2.0)
        assert failure["snapshot"]["frame_seq"] == 1

    def test_assert_after_satisfied_no_failure(self) -> None:
        """条件满足 -> 断言移除，不触发失败。"""
        rt = MachineRuntime(self._assert_machine("assert_after"), FakeClock())
        rt.tick(snap(0, 0.0))
        r = rt.tick(snap(1, 0.5, ready=(True, None)))
        assert r.to_state is None
        assert rt.state() == "check"
        assert rt.last_assert_failure is None

    def test_wait_until_blocks_then_passes(self) -> None:
        """wait_until：阻塞期间不迁移；条件满足后同 tick 解除并选择迁移。"""
        data = {
            "schema_version": 1, "machine_id": "waitu", "initial": "hold",
            "states": {
                "hold": {
                    "entry": [{"kind": "wait_until", "when": "ready.present",
                               "timeout_seconds": 5, "to": "gaveup"}],
                    "transitions": [{"when": "ready.present", "to": "next"}],
                },
                "next": {"terminal": True},
                "gaveup": {"terminal": True},
            },
        }
        rt = MachineRuntime(compile_machine_dict(data), FakeClock())
        blocked = rt.tick(snap(0, 0.0))
        assert blocked.to_state is None and rt.state() == "hold"
        passed = rt.tick(snap(1, 0.1, ready=(True, None)))
        assert passed.to_state == "next"

    def test_wait_until_timeout_failure(self) -> None:
        """wait_until 超时 -> 迁入出口状态。"""
        clock = FakeClock()
        rt = MachineRuntime(self._assert_machine("wait_until"), clock)
        rt.tick(snap(0, 0.0))
        clock.advance(5.0)
        assert rt.tick(snap(1, 5.0)).to_state == "recovery"

    def test_assertion_dropped_on_state_exit(self) -> None:
        """断言与状态驻留绑定：正常迁出后不再触发失败。"""
        data = {
            "schema_version": 1, "machine_id": "drop", "initial": "a",
            "states": {
                "a": {
                    "entry": [{"kind": "assert_after", "when": "never.present",
                               "timeout_seconds": 1, "to": "recovery"}],
                    "transitions": [{"when": "go.present", "to": "b"}],
                },
                "b": {"terminal": True},
                "recovery": {"terminal": True},
            },
        }
        rt = MachineRuntime(compile_machine_dict(data), FakeClock())
        rt.tick(snap(0, 0.0))
        assert rt.tick(snap(1, 0.1, go=(True, None))).to_state == "b"
        rt2_clock = FakeClock()
        rt2_clock.advance(10)
        # 已离开 a：不会在 t=1 时把机器拉回 recovery
        assert rt.last_assert_failure is None


# ---------------------------------------------------------------------------
# 确定性哈希（FSM-009 / AC-P0-08）
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_hash_sequence_identical_across_100_runs(self) -> None:
        """同版本同轨迹同种子：100 次运行的哈希序列完全一致（AC-P0-08）。"""
        machine = compile_extended_arena()
        sequences: list[list[str]] = []
        for _ in range(100):
            _, results = full_scenario(machine, FakeClock())
            sequences.append([r.decision_hash for r in results])
        assert len(sequences[0]) == 5
        assert all(seq == sequences[0] for seq in sequences)

    def test_hash_indifferent_to_rng(self) -> None:
        """随机性只来自注入 rng：不同 rng 不影响决策哈希。"""
        machine = compile_extended_arena()
        hashes: list[list[str]] = []
        for seed in (1, 999):
            clock = FakeClock()
            rt = MachineRuntime(machine, clock, rng=random.Random(seed))
            seq = [
                rt.tick(snap(0, 0.0, ready=(True, None))).decision_hash,
            ]
            hashes.append(seq)
        assert hashes[0] == hashes[1]

    def test_hash_changes_with_input(self) -> None:
        """不同快照 -> 不同哈希（哈希确实覆盖了输入）。"""
        machine = compile_machine_dict(simple_machine())
        h1 = MachineRuntime(machine, FakeClock()).tick(snap(0, 0.0)).decision_hash
        h2 = MachineRuntime(machine, FakeClock()).tick(snap(0, 0.1)).decision_hash
        assert h1 != h2
        assert len(h1) == 64

    def test_result_records_transition_info(self) -> None:
        """TickResult 携带守卫序列与停止语义字段。"""
        rt = MachineRuntime(compile_machine_dict(simple_machine()), FakeClock())
        r = rt.tick(snap(0, 0.0, go=(True, None)))
        assert r.guards_evaluated == ("go.present",)
        assert r.tick == 1 and r.stop_reason is None


# ---------------------------------------------------------------------------
# 覆盖率（FSM-010）
# ---------------------------------------------------------------------------


class TestCoverage:
    def test_coverage_counts(self) -> None:
        """状态进入/迁移触发/守卫评估/异常路径计数正确。"""
        machine = compile_extended_arena()
        rt, results = full_scenario(machine, FakeClock())
        coverage = rt.coverage()
        assert coverage["states_entered"] == {
            "awaiting_manual_gate": 1, "exercise": 1, "idle": 1, "stopped": 1,
        }
        assert coverage["transitions_fired"] == {
            "awaiting_manual_gate->exercise": 1,
            "exercise->stopped": 1,
            "idle->awaiting_manual_gate": 1,
        }
        # r1: idle 守卫；r3: awaiting 守卫；r4、r5: exercise 守卫（r2 闸门挂起不评估）
        assert coverage["guards_evaluated"]["idle"] == 1
        assert coverage["guards_evaluated"]["awaiting_manual_gate"] == 1
        assert coverage["guards_evaluated"]["exercise"] == 2
        assert coverage["exception_paths"] == {}
        assert set(coverage) == {
            "states_entered", "transitions_fired", "guards_evaluated",
            "guards_true", "exception_paths",
        }

    def test_coverage_counts_exception_paths(self) -> None:
        data = {
            "schema_version": 1, "machine_id": "cov", "initial": "bad",
            "states": {
                "bad": {
                    "entry": [{"kind": "press_key"},
                              {"kind": "retry", "max_attempts": 2, "base_ms": 100},
                              {"kind": "on_error_to", "to": "stopped"}],
                    "transitions": [{"when": "false", "to": "stopped"}],
                },
                "stopped": {"terminal": True},
            },
        }
        clock = FakeClock()
        rt = MachineRuntime(compile_machine_dict(data), clock)
        rt.tick(snap(0, 0.0))
        clock.advance(0.1)
        rt.tick(snap(1, 0.1))
        assert rt.coverage()["exception_paths"] == {"bad": 2}

    def test_guards_not_counted_while_gate_pending(self) -> None:
        """闸门挂起期间不评估守卫（超时兜底仍生效）。"""
        rt = MachineRuntime(compile_extended_arena(), FakeClock())
        rt.tick(snap(0, 0.0, ready=(True, None)))
        rt.tick(snap(1, 0.1, manual_gate=(True, None)))
        assert "awaiting_manual_gate" not in rt.coverage()["guards_evaluated"]


# ---------------------------------------------------------------------------
# 意图契约
# ---------------------------------------------------------------------------


class TestIntentContract:
    def test_intents_carry_cause_and_identity(self) -> None:
        """意图 cause=状态名，TTL 基于注入 Clock。"""
        clock = FakeClock()
        recorder = IntentRecorder()
        machine = compile_extended_arena()
        rt = MachineRuntime(machine, clock, on_intent=recorder,
                            session_id="sess-x", target_id="arena-lab")
        rt.tick(snap(0, 0.0, ready=(True, None)))
        gate = recorder.intents[0]
        assert isinstance(gate, InputIntent)
        assert gate.kind == "wait"
        assert gate.cause.startswith("manual_gate:")
        assert gate.session_id == "sess-x" and gate.target_id == "arena-lab"
        assert gate.created_monotonic == 0.0
        clock.advance(0.1)
        rt.approve_manual_gate()
        rt.tick(snap(1, 0.1, manual_gate=(True, None)))
        for intent in recorder.intents[1:]:
            assert intent.cause == "exercise"
            assert intent.created_monotonic == 0.1
            assert intent.expires_monotonic > intent.created_monotonic

    def test_bad_action_payload_raises_not_executes(self) -> None:
        """参数不合法的意图只引发运行时异常路径，绝不触碰真实输入。"""
        data = simple_machine()
        data["machine_id"] = "badpayload"
        data["states"]["a"]["entry"] = [{"kind": "press_key"}]  # 缺 key
        data["states"]["a"]["transitions"] = []  # 消除迁移，专注 entry 路径
        data["states"]["a"]["timeout_seconds"] = 99
        del data["states"]["b"]  # b 不再被引用
        rt = MachineRuntime(compile_machine_dict(data), FakeClock())
        r = rt.tick(snap(0, 0.0))
        # 无 retry/on_error_to：停止并保留错误原因
        assert r.stopped is True and r.stop_reason == "error"
        assert rt.coverage()["exception_paths"] == {"a": 1}


# ---------------------------------------------------------------------------
# 编译错误映射（FSM-002 补充）
# ---------------------------------------------------------------------------


def test_conflict_compile_error_via_dict() -> None:
    """冲突迁移编译报错（错误定位到后一条迁移）。"""
    data = {
        "schema_version": 1, "machine_id": "dup", "initial": "a",
        "states": {
            "a": {"transitions": [{"when": "true", "to": "b"},
                                   {"when": " true ", "to": "c"}]},
            "b": {"terminal": True},
            "c": {"terminal": True},
        },
    }
    with pytest.raises(DomainValidationError) as excinfo:
        compile_machine_dict(data)
    issue = next(i for i in excinfo.value.issues if i.rule == "transition_conflict")
    assert issue.pointer == "/states/a/transitions/1"
