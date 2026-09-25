"""POL-001/002/003/004/005 单测：默认拒绝、拒绝代码全覆盖与 SAFE 用例单测版。

覆盖拒绝代码：mode_not_allowed、foreground_mismatch、expired_intent、
budget_exhausted、runtime_limit_reached、manual_gate_required、
protected_online_no_real_input（硬锁）、session_mismatch，
以及缺失上下文时的默认拒绝。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from common import FakeClock

from input_broker import (
    FakeInputSink,
    InputBroker,
    InputBatch,
    InputIntent,
    make_batch,
    make_intent,
)
from policy_engine import (
    BudgetTracker,
    ForegroundContext,
    ModeGate,
    PolicyDecision,
    PolicyEvaluator,
    PolicyInput,
    RunMode,
    TargetRef,
)

SESSION = "sess-t"
TARGET = TargetRef("arena-lab", protected_online=False, title_regex=r"^ArenaLab")
PROTECTED_TARGET = TargetRef("protected-online", protected_online=True)


def make_fg(
    *,
    target_id: str | None = "arena-lab",
    pid: int | None = 111,
    session_id: str | None = SESSION,
) -> ForegroundContext:
    """会话绑定的前台上下文（可用参数制造错窗/错进程/错会话）。"""
    return ForegroundContext(
        target_id=target_id,
        pid=pid,
        hwnd=2222,
        expected_target_id="arena-lab",
        expected_pid=111,
        expected_session_id=session_id,
    )


def click_batch(
    clock: FakeClock, *, count: int = 1, target_id: str = "arena-lab"
) -> InputBatch:
    """一个只含 click 意图的批次。"""
    intents: list[InputIntent] = [
        make_intent(
            SESSION, target_id, "click", {"button": "left", "x": i, "y": 0},
            clock=clock, ttl_ms=500.0, cause="test",
        )
        for i in range(count)
    ]
    return make_batch(SESSION, target_id, intents, clock=clock, ttl_ms=1000.0,
                      cause="test")


def confirmed_gate(clock: FakeClock, target: TargetRef | None = TARGET) -> ModeGate:
    """已为目标会话完成人工确认的闸门。"""
    gate = ModeGate(clock=clock)
    assert gate.confirm(SESSION, "op:tester", target=target) is not None
    return gate


def make_pipeline(
    clock: FakeClock,
    *,
    target: TargetRef = TARGET,
    policy_mode: str = "real_input",
    gate: ModeGate | None = None,
    **policy_kw: Any,
) -> tuple[PolicyEvaluator, BudgetTracker, FakeInputSink, InputBroker]:
    """ evaluator + 预算 + FakeSink + Broker 的标准测试装配。"""
    policy = PolicyInput(mode=policy_mode, **policy_kw)
    evaluator = PolicyEvaluator(policy, gate=gate)
    budget = BudgetTracker(clock=clock, policy=policy)
    sink = FakeInputSink(clock=clock)
    broker = InputBroker(sink, clock=clock)
    return evaluator, budget, sink, broker


# ---------------------------------------------------------------- 默认拒绝


def test_unknown_mode_is_denied() -> None:
    """未知 mode -> 默认拒绝（mode_not_allowed）。"""
    clock = FakeClock()
    gate = confirmed_gate(clock)
    evaluator, budget, sink, broker = make_pipeline(clock, gate=gate)
    batch = click_batch(clock)
    decision = evaluator.evaluate(batch, "hyperdrive", TARGET, make_fg(), budget, clock)
    assert not decision.allow and "mode_not_allowed" in decision.reasons
    result = broker.submit(batch, decision)
    assert result.accepted_intents == [] and sink.real_executed_count == 0


def test_missing_context_defaults_to_deny() -> None:
    """缺失前台/缺失目标 -> 默认拒绝，绝不放行。"""
    clock = FakeClock()
    gate = confirmed_gate(clock)
    evaluator, budget, sink, _ = make_pipeline(clock, gate=gate)
    batch = click_batch(clock)
    d1 = evaluator.evaluate(batch, RunMode.REAL_INPUT, TARGET, None, budget, clock)
    assert not d1.allow and "missing_foreground_context" in d1.reasons
    d2 = evaluator.evaluate(batch, RunMode.REAL_INPUT, None, make_fg(), budget, clock)
    assert not d2.allow and "missing_target_context" in d2.reasons


@pytest.mark.parametrize("mode", ["observe", "shadow", "dry_run"])
def test_low_risk_modes_never_produce_real_input(mode: str) -> None:
    """SAFE-004 单测版：observe/shadow/dry_run 下真实意图不产生输入。"""
    clock = FakeClock()
    evaluator, budget, sink, broker = make_pipeline(clock)
    batch = click_batch(clock)
    decision = evaluator.evaluate(batch, mode, TARGET, make_fg(), budget, clock)
    assert not decision.allow and "mode_not_allowed" in decision.reasons
    assert broker.submit(batch, decision).accepted_intents == []
    assert sink.real_executed_count == 0


def test_policy_mode_ceiling_cannot_be_raised_by_runtime() -> None:
    """策略允许上限为 shadow 时，即便一切就绪也不允许 real_input。"""
    clock = FakeClock()
    gate = confirmed_gate(clock)
    evaluator, budget, sink, broker = make_pipeline(
        clock, policy_mode="shadow", gate=gate
    )
    batch = click_batch(clock)
    decision = evaluator.evaluate(batch, "real_input", TARGET, make_fg(), budget, clock)
    assert not decision.allow and "mode_not_allowed" in decision.reasons
    assert broker.submit(batch, decision).accepted_intents == []
    assert sink.real_executed_count == 0


# ---------------------------------------------------------------- SAFE-001


def test_foreground_mismatch_means_zero_real_input() -> None:
    """SAFE-001：前台窗口不是目标 -> 批次拒绝、真实输入 0。"""
    clock = FakeClock()
    gate = confirmed_gate(clock)
    evaluator, budget, sink, broker = make_pipeline(clock, gate=gate)
    batch = click_batch(clock)
    wrong_window = make_fg(target_id="other-window.exe", pid=999)
    decision = evaluator.evaluate(batch, "real_input", TARGET, wrong_window, budget, clock)
    assert not decision.allow and "foreground_mismatch" in decision.reasons
    result = broker.submit(batch, decision)
    assert result.accepted_intents == []
    assert all("foreground_mismatch" in r.reason for r in result.rejected)
    assert sink.real_executed_count == 0


def test_foreground_pid_mismatch_is_rejected() -> None:
    """前台标题匹配但 PID 与会话绑定不符 -> foreground_mismatch（错窗同标题）。"""
    clock = FakeClock()
    gate = confirmed_gate(clock)
    evaluator, budget, _, _ = make_pipeline(clock, gate=gate)
    batch = click_batch(clock)
    same_title_other_pid = make_fg(pid=321)
    decision = evaluator.evaluate(
        batch, "real_input", TARGET, same_title_other_pid, budget, clock
    )
    assert not decision.allow and "foreground_mismatch" in decision.reasons


def test_session_mismatch_from_foreground_binding() -> None:
    """前台绑定的是另一个会话 -> session_mismatch。"""
    clock = FakeClock()
    gate = confirmed_gate(clock)
    evaluator, budget, _, _ = make_pipeline(clock, gate=gate)
    batch = click_batch(clock)
    other_session_fg = make_fg(session_id="sess-other")
    decision = evaluator.evaluate(
        batch, "real_input", TARGET, other_session_fg, budget, clock
    )
    assert not decision.allow and "session_mismatch" in decision.reasons


# ---------------------------------------------------------------- SAFE-003


def test_expired_intent_denied_not_replayed_and_fresh_ok() -> None:
    """SAFE-003：过期意图拒绝、不补发，且不影响后续新批次。"""
    clock = FakeClock(start=100.0)
    gate = confirmed_gate(clock)
    evaluator, budget, sink, broker = make_pipeline(clock, gate=gate)
    fg = make_fg()
    stale = click_batch(clock)  # 意图 TTL 500ms
    clock.advance(1.0)
    decision = evaluator.evaluate(stale, "real_input", TARGET, fg, budget, clock)
    assert not decision.allow and "expired_intent" in decision.reasons
    result = broker.submit(stale, decision)
    assert result.accepted_intents == []
    assert sink.real_executed_count == 0  # 不补发
    fresh = click_batch(clock)  # 新批次在当前时刻创建
    decision2 = evaluator.evaluate(fresh, "real_input", TARGET, fg, budget, clock)
    assert decision2.allow
    assert broker.submit(fresh, decision2).accepted_count == 1


# ---------------------------------------------------------------- SAFE-004


def test_shadow_records_intents_but_never_executes() -> None:
    """SAFE-004：Shadow 模式意图进轨迹/账本记录，OS 输入为 0。"""
    clock = FakeClock()
    evaluator, budget, sink, broker = make_pipeline(clock)
    batch = click_batch(clock, count=2)
    decision = evaluator.evaluate(batch, "shadow", TARGET, make_fg(), budget, clock)
    assert not decision.allow
    assert broker.submit(batch, decision).accepted_intents == []
    assert sink.real_executed_count == 0
    # 运行时在 Shadow 下的记录路径：意图入账本但标记未执行。
    assert sink.record_shadow(batch) == 2
    assert sink.shadow_recorded_count == 2
    assert sink.real_executed_count == 0


# ---------------------------------------------------------------- SAFE-012


def test_actions_per_minute_budget_exhausted() -> None:
    """SAFE-012：max_actions_per_minute=3 连发 5 个 -> 第 4 个起拒绝。"""
    clock = FakeClock()
    gate = confirmed_gate(clock)
    evaluator, budget, sink, broker = make_pipeline(
        clock, gate=gate, max_actions_per_minute=3
    )
    fg = make_fg()
    outcomes: list[PolicyDecision] = []
    for _ in range(5):
        batch = click_batch(clock)
        decision = evaluator.evaluate(batch, "real_input", TARGET, fg, budget, clock)
        outcomes.append(decision)
        broker.submit(batch, decision)
    assert all(d.allow for d in outcomes[:3])
    assert all(
        not d.allow and "budget_exhausted:actions_per_minute" in d.reasons
        for d in outcomes[3:]
    )
    assert sink.real_executed_count == 3  # 当前及后续动作都被拒绝


def test_total_actions_budget_exhausted() -> None:
    """会话总动作数预算：第 3 个动作起拒绝（budget_exhausted:total_actions）。"""
    clock = FakeClock()
    gate = confirmed_gate(clock)
    evaluator, budget, sink, broker = make_pipeline(
        clock, gate=gate, max_actions_per_minute=100, max_total_actions=2
    )
    fg = make_fg()
    outcomes = []
    for _ in range(3):
        batch = click_batch(clock)
        decision = evaluator.evaluate(batch, "real_input", TARGET, fg, budget, clock)
        outcomes.append(decision)
        broker.submit(batch, decision)
    assert outcomes[0].allow and outcomes[1].allow
    assert not outcomes[2].allow
    assert "budget_exhausted:total_actions" in outcomes[2].reasons
    assert sink.real_executed_count == 2


# ---------------------------------------------------------------- SAFE-014


def test_runtime_limit_reached_is_irreversible() -> None:
    """SAFE-014：运行时长达到硬上限 -> runtime_limit_reached，停止态不恢复。"""
    clock = FakeClock(start=0.0)
    gate = confirmed_gate(clock)
    evaluator, budget, sink, broker = make_pipeline(
        clock, gate=gate, max_runtime_minutes=1
    )
    fg = make_fg()
    before = click_batch(clock)
    before_decision = evaluator.evaluate(
        before, "real_input", TARGET, fg, budget, clock
    )
    assert before_decision.allow
    assert broker.submit(before, before_decision).accepted_count == 1
    clock.advance(60.0)  # 恰好达到 1 分钟硬上限
    assert budget.is_runtime_exceeded()
    after = click_batch(clock)
    decision = evaluator.evaluate(after, "real_input", TARGET, fg, budget, clock)
    assert not decision.allow and "runtime_limit_reached" in decision.reasons
    assert broker.submit(after, decision).accepted_intents == []
    assert sink.real_executed_count == 1  # 只有上限前的那一批执行过


# ---------------------------------------------------------------- POL-002


def test_real_input_requires_manual_gate() -> None:
    """real_input 无人工闸门确认 -> manual_gate_required（默认拒绝）。"""
    clock = FakeClock()
    evaluator, budget, sink, broker = make_pipeline(clock, gate=None)
    batch = click_batch(clock)
    decision = evaluator.evaluate(batch, "real_input", TARGET, make_fg(), budget, clock)
    assert not decision.allow and "manual_gate_required" in decision.reasons
    assert broker.submit(batch, decision).accepted_intents == []
    # 有闸门但未确认同样拒绝。
    gate = ModeGate(clock=clock)
    evaluator2 = PolicyEvaluator(PolicyInput(mode="real_input"), gate=gate)
    decision2 = evaluator2.evaluate(
        click_batch(clock), "real_input", TARGET, make_fg(),
        BudgetTracker(clock=clock), clock,
    )
    assert not decision2.allow and "manual_gate_required" in decision2.reasons


def test_mode_gate_records_audit_fields() -> None:
    """confirm 记录人工闸门审计字段；空 operator 被拒绝并入审计。"""
    clock = FakeClock(start=5.0)
    gate = ModeGate(clock=clock)
    confirmation = gate.confirm(
        SESSION, " op:alice ", target=TARGET, note="窗口前人工启动"
    )
    assert confirmation is not None
    assert confirmation.session_id == SESSION
    assert confirmation.operator == "op:alice"
    assert confirmation.target_id == TARGET.target_id
    assert confirmation.mode == "real_input"
    assert confirmation.confirmed_at_monotonic == pytest.approx(5.0)
    assert confirmation.gate_id.startswith("gate-")
    assert gate.is_confirmed(SESSION)
    refused = gate.confirm(SESSION, "   ")  # 空 operator -> 默认拒绝
    assert refused is None
    assert list(gate.refusal_reasons())[-1] == "operator_required"


def test_mode_gate_refuses_protected_target() -> None:
    """受保护目标无法通过人工闸门确认（审计留痕）。"""
    clock = FakeClock()
    gate = ModeGate(clock=clock)
    confirmation = gate.confirm(SESSION, "op:alice", target=PROTECTED_TARGET)
    assert confirmation is None
    assert not gate.is_confirmed(SESSION)
    assert list(gate.refusal_reasons()) == ["protected_online_no_real_input"]


# ---------------------------------------------------------------- POL-003 / SAFE-020


def test_protected_hard_lock_beats_manual_confirmation() -> None:
    """SAFE-020：protected_online 硬锁——即便人工确认存在也拒绝真实输入。"""
    clock = FakeClock()
    gate = ModeGate(clock=clock)
    # 会话级确认（不绑定目标）成功，模拟"确认状态已存在/被绕过"。
    assert gate.confirm(SESSION, "op:alice") is not None
    assert gate.is_confirmed(SESSION)
    evaluator, budget, sink, broker = make_pipeline(clock, gate=gate)
    batch = click_batch(clock, target_id=PROTECTED_TARGET.target_id)
    fg = ForegroundContext(
        target_id=PROTECTED_TARGET.target_id, pid=7, hwnd=77,
        expected_target_id=PROTECTED_TARGET.target_id, expected_pid=7,
        expected_session_id=SESSION,
    )
    decision = evaluator.evaluate(
        batch, "real_input", PROTECTED_TARGET, fg, budget, clock
    )
    assert not decision.allow
    assert "protected_online_no_real_input" in decision.reasons
    assert broker.submit(batch, decision).accepted_intents == []
    assert sink.real_executed_count == 0


# ---------------------------------------------------------------- 集成


def test_real_input_happy_path_consumes_budget_and_executes() -> None:
    """全链路正向用例：确认 + 正确前台 + 预算内 -> 允许并真实执行（FakeSink）。"""
    clock = FakeClock()
    gate = confirmed_gate(clock)
    evaluator, budget, sink, broker = make_pipeline(clock, gate=gate)
    batch = click_batch(clock, count=2)
    decision = evaluator.evaluate(batch, "real_input", TARGET, make_fg(), budget, clock)
    assert decision.allow and decision.reasons == []
    assert decision.mode == "real_input"
    result = broker.submit(batch, decision)
    assert result.accepted_count == 2
    assert sink.real_executed_count == 2
    assert budget.state().total_actions == 2  # 预算随允许决策扣减


def test_real_input_low_risk_decision_updates_broker_mode() -> None:
    """决策带出 mode：低风险决策让 Broker 回到拒绝真实输入的状态。"""
    clock = FakeClock()
    gate = confirmed_gate(clock)
    evaluator, budget, sink, broker = make_pipeline(clock, gate=gate)
    batch = click_batch(clock)
    assert broker.submit(batch, evaluator.evaluate(
        batch, "real_input", TARGET, make_fg(), budget, clock)
    ).accepted_count == 1
    shadow_batch = click_batch(clock)
    shadow_decision = evaluator.evaluate(
        shadow_batch, "shadow", TARGET, make_fg(), budget, clock
    )
    broker.submit(shadow_batch, shadow_decision)  # 决策 mode=shadow 更新 Broker
    assert broker.mode == "shadow"
    real_batch = click_batch(clock)
    # 运行时无法自证 real_input：不带 mode 的 allow 决策不会改变 Broker 模式，
    # Broker 仍按最近一次策略决策的 shadow 拒绝（纵深防御）。
    forged = PolicyDecision.allowed("real_input", real_batch, clock.now())
    assert forged.allow
    result = broker.submit(
        real_batch, SimpleNamespace(allow=True, reasons=[], mode=None)
    )
    assert result.accepted_intents == []
    assert all(r.reason == "mode_not_allowed" for r in result.rejected)
    assert sink.real_executed_count == 1


def test_test_kit_fake_foreground_context_programmable() -> None:
    """TST-001（M0）：FakeForegroundContext 可编程模拟聚焦/失焦/错窗/换绑。"""
    from test_kit import FakeClock, FakeForegroundContext, make_click_batch

    clock = FakeClock()
    fg = FakeForegroundContext()
    fg.bind(target_id="arena-lab", pid=111, session_id=SESSION)
    fg.focus("arena-lab", pid=111, hwnd=1)
    gate = confirmed_gate(clock)
    evaluator, budget, _, _ = make_pipeline(clock, gate=gate)
    batch = make_click_batch(SESSION, "arena-lab", clock=clock)
    assert evaluator.evaluate(batch, "real_input", TARGET, fg, budget, clock).allow
    fg.switch_to("other-window.exe", pid=999)  # 错窗
    decision = evaluator.evaluate(
        make_click_batch(SESSION, "arena-lab", clock=clock),
        "real_input", TARGET, fg, budget, clock,
    )
    assert "foreground_mismatch" in decision.reasons
    fg.focus("arena-lab", pid=111)  # 回到正确窗口
    fg.blur()  # 失焦：默认拒绝
    decision2 = evaluator.evaluate(
        make_click_batch(SESSION, "arena-lab", clock=clock),
        "real_input", TARGET, fg, budget, clock,
    )
    assert "foreground_mismatch" in decision2.reasons
