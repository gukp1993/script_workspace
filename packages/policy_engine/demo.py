"""策略安全场景演示（python -m policy_engine.demo）。

运行 5 个内置场景并打印结果表，全部通过 exit 0，否则 exit 1：

1. Shadow 模式：状态机语义上产生的意图经 evaluator + FakeInputSink，
   记录存在但"真实输入执行数 = 0"（SAFE-004）；
2. 错误前台：foreground 与会话绑定不符 -> 批次拒绝、reason 含
   foreground_mismatch、真实输入 0（SAFE-001）；
3. 过期意图：FakeClock 推进超 TTL -> 拒绝 reason 含 expired_intent
   且不补发、不影响后续新批次（SAFE-003）；
4. 预算耗尽：max_actions_per_minute=3 连发 5 个 -> 第 4 个起拒绝
   reason 含 budget_exhausted（SAFE-012）；
5. protected_online 硬锁：受保护目标 + 人工闸门确认 + real_input
   -> 仍拒绝 reason 含 protected_online_no_real_input（POL-003/SAFE-020）。

说明：从仓库根目录直接 `python -m policy_engine.demo` 需要
PYTHONPATH 包含 packages 与 services（pytest 的 pythonpath 仅对 pytest
生效）；本模块自带路径引导，也支持 `python packages/policy_engine/demo.py`。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _ensure_repo_paths() -> None:
    """把仓库的 packages/ 与 services/ 注入 sys.path（幂等）。"""
    root = Path(__file__).resolve().parents[2]
    for sub in ("packages", "services"):
        candidate = str(root / sub)
        if candidate not in sys.path:
            sys.path.insert(0, candidate)


_ensure_repo_paths()

from common import FakeClock, Clock  # noqa: E402

from input_broker import (  # noqa: E402
    FakeInputSink,
    InputBroker,
    make_batch,
    make_intent,
)
from policy_engine import (  # noqa: E402
    BudgetTracker,
    ForegroundContext,
    ModeGate,
    PolicyEvaluator,
    PolicyInput,
    TargetRef,
)

SESSION = "sess-demo"
LOCAL_TARGET = TargetRef("arena-lab", protected_online=False, title_regex=r"^ArenaLab")
PROTECTED_TARGET = TargetRef("protected-online", protected_online=True)


def _foreground() -> ForegroundContext:
    """会话绑定的正确前台上下文。"""
    return ForegroundContext(
        target_id=LOCAL_TARGET.target_id,
        pid=111,
        hwnd=2222,
        expected_target_id=LOCAL_TARGET.target_id,
        expected_pid=111,
        expected_session_id=SESSION,
    )


def _intents_from_state_transitions(clock: Clock) -> list[Any]:
    """用最小迁移序列替身"状态机语义产生意图"（M2 由 state_machine 包实现）。"""
    script = [
        ("idle", "scan", "click", {"button": "left", "x": 10, "y": 20}),
        ("scan", "engage", "key_down", {"key": "f"}),
    ]
    return [
        make_intent(
            SESSION,
            LOCAL_TARGET.target_id,
            kind,
            payload,
            clock=clock,
            ttl_ms=500.0,
            cause=f"fsm:{src}->{dst}",
        )
        for src, dst, kind, payload in script
    ]


def _click(clock: Clock, cause: str) -> Any:
    return make_intent(
        SESSION,
        LOCAL_TARGET.target_id,
        "click",
        {"button": "left", "x": 1, "y": 2},
        clock=clock,
        ttl_ms=500.0,
        cause=cause,
    )


def _make_pipeline(
    clock: Clock, policy: PolicyInput
) -> tuple[ModeGate, PolicyEvaluator, BudgetTracker, FakeInputSink]:
    gate = ModeGate(clock=clock)
    evaluator = PolicyEvaluator(policy, gate=gate)
    budget = BudgetTracker(clock=clock, policy=policy)
    sink = FakeInputSink(clock=clock)
    return gate, evaluator, budget, sink


def scenario_1_shadow_records_but_zero_real_input() -> tuple[bool, str, str, int]:
    """场景 1：Shadow 模式记录意图但真实输入执行数为 0。"""
    clock = FakeClock(start=0.0)
    gate, evaluator, budget, sink = _make_pipeline(
        clock, PolicyInput(mode="real_input", max_actions_per_minute=120)
    )
    fg = _foreground()
    batch = make_batch(
        SESSION, LOCAL_TARGET.target_id, _intents_from_state_transitions(clock),
        clock=clock, ttl_ms=1000.0, cause="fsm",
    )
    decision = evaluator.evaluate(batch, "shadow", LOCAL_TARGET, fg, budget, clock)
    recorded = sink.record_shadow(batch)
    ok = (
        not decision.allow
        and "mode_not_allowed" in decision.reasons
        and recorded == len(batch.intents)
        and sink.real_executed_count == 0
    )
    return ok, "shadow", ";".join(decision.reasons), sink.real_executed_count


def scenario_2_foreground_mismatch_zero_input() -> tuple[bool, str, str, int]:
    """场景 2：错误前台 -> 批次拒绝、真实输入 0。"""
    clock = FakeClock(start=0.0)
    gate, evaluator, budget, sink = _make_pipeline(
        clock, PolicyInput(mode="real_input", max_actions_per_minute=120)
    )
    assert gate.confirm(SESSION, "operator:demo", target=LOCAL_TARGET) is not None
    fg = ForegroundContext(
        target_id="other-window.exe",  # 前台是别的窗口
        pid=999,
        hwnd=8888,
        expected_target_id=LOCAL_TARGET.target_id,
        expected_pid=111,
        expected_session_id=SESSION,
    )
    batch = make_batch(
        SESSION, LOCAL_TARGET.target_id, [_click(clock, "demo2")],
        clock=clock, ttl_ms=1000.0, cause="demo2",
    )
    decision = evaluator.evaluate(batch, "real_input", LOCAL_TARGET, fg, budget, clock)
    broker = InputBroker(sink, clock=clock)
    result = broker.submit(batch, decision)
    ok = (
        not decision.allow
        and "foreground_mismatch" in decision.reasons
        and not result.accepted_intents
        and all("foreground_mismatch" in r.reason for r in result.rejected)
        and sink.real_executed_count == 0
    )
    return ok, "real_input", ";".join(decision.reasons), sink.real_executed_count


def scenario_3_expired_intent_not_replayed() -> tuple[bool, str, str, int]:
    """场景 3：过期意图拒绝且不补发；后续新批次不受影响。"""
    clock = FakeClock(start=100.0)
    gate, evaluator, budget, sink = _make_pipeline(
        clock, PolicyInput(mode="real_input", max_actions_per_minute=120)
    )
    gate.confirm(SESSION, "operator:demo", target=LOCAL_TARGET)
    fg = _foreground()
    broker = InputBroker(sink, clock=clock)

    stale = make_batch(
        SESSION, LOCAL_TARGET.target_id, [_click(clock, "demo3-stale")],
        clock=clock, ttl_ms=1000.0, cause="demo3-stale",
    )
    clock.advance(1.0)  # 超过意图 TTL（500ms）
    decision = evaluator.evaluate(stale, "real_input", LOCAL_TARGET, fg, budget, clock)
    result = broker.submit(stale, decision)
    executed_after_stale = sink.real_executed_count

    fresh = make_batch(
        SESSION, LOCAL_TARGET.target_id, [_click(clock, "demo3-fresh")],
        clock=clock, ttl_ms=1000.0, cause="demo3-fresh",
    )
    decision2 = evaluator.evaluate(fresh, "real_input", LOCAL_TARGET, fg, budget, clock)
    result2 = broker.submit(fresh, decision2)

    ok = (
        not decision.allow
        and "expired_intent" in decision.reasons
        and not result.accepted_intents
        and executed_after_stale == 0  # 不补发
        and decision2.allow
        and len(result2.accepted_intents) == 1  # 不影响后续新批次
        and sink.real_executed_count == 1
    )
    return ok, "real_input", ";".join(decision.reasons), sink.real_executed_count


def scenario_4_budget_exhausted() -> tuple[bool, str, str, int]:
    """场景 4：每分钟动作数耗尽，第 4 个起拒绝。"""
    clock = FakeClock(start=0.0)
    gate, evaluator, budget, sink = _make_pipeline(
        clock, PolicyInput(mode="real_input", max_actions_per_minute=3)
    )
    gate.confirm(SESSION, "operator:demo", target=LOCAL_TARGET)
    fg = _foreground()
    broker = InputBroker(sink, clock=clock)

    allowed, denied_with_budget = 0, 0
    for i in range(5):
        batch = make_batch(
            SESSION, LOCAL_TARGET.target_id, [_click(clock, f"demo4-{i}")],
            clock=clock, ttl_ms=1000.0, cause=f"demo4-{i}",
        )
        decision = evaluator.evaluate(batch, "real_input", LOCAL_TARGET, fg, budget, clock)
        result = broker.submit(batch, decision)
        if decision.allow and result.accepted_intents:
            allowed += 1
        if "budget_exhausted" in ";".join(decision.reasons):
            denied_with_budget += 1

    ok = allowed == 3 and denied_with_budget == 2 and sink.real_executed_count == 3
    reasons = (
        "budget_exhausted:actions_per_minute(第4/5批)" if denied_with_budget else ""
    )
    return ok, "real_input", reasons, sink.real_executed_count


def scenario_5_protected_hard_lock_beats_manual_gate() -> tuple[bool, str, str, int]:
    """场景 5：受保护目标硬锁——人工确认也无法解锁真实输入。"""
    clock = FakeClock(start=0.0)
    gate, evaluator, budget, sink = _make_pipeline(
        clock, PolicyInput(mode="real_input", max_actions_per_minute=120)
    )
    # 显式对受保护目标确认 -> 被闸门拒绝并入审计。
    refused = gate.confirm(SESSION, "operator:demo", target=PROTECTED_TARGET)
    # 即使会话级确认（不绑定目标）成功，硬锁依然生效。
    confirmed = gate.confirm(SESSION, "operator:demo")
    fg = ForegroundContext(
        target_id=PROTECTED_TARGET.target_id,
        pid=7,
        hwnd=77,
        expected_target_id=PROTECTED_TARGET.target_id,
        expected_pid=7,
        expected_session_id=SESSION,
    )
    batch = make_batch(
        SESSION,
        PROTECTED_TARGET.target_id,
        [make_intent(SESSION, PROTECTED_TARGET.target_id, "click",
                     {"button": "left", "x": 0, "y": 0}, clock=clock, ttl_ms=500.0,
                     cause="demo5")],
        clock=clock,
        ttl_ms=1000.0,
        cause="demo5",
    )
    decision = evaluator.evaluate(
        batch, "real_input", PROTECTED_TARGET, fg, budget, clock
    )
    broker = InputBroker(sink, clock=clock)
    result = broker.submit(batch, decision)
    ok = (
        refused is None
        and confirmed is not None
        and gate.is_confirmed(SESSION)  # 人工确认存在
        and not decision.allow
        and "protected_online_no_real_input" in decision.reasons
        and not result.accepted_intents
        and sink.real_executed_count == 0
    )
    return ok, "real_input", ";".join(decision.reasons), sink.real_executed_count


SCENARIOS = [
    ("S1 Shadow 模式：意图有记录、真实输入为 0", scenario_1_shadow_records_but_zero_real_input),
    ("S2 错误前台：批次拒绝、真实输入为 0", scenario_2_foreground_mismatch_zero_input),
    ("S3 过期意图：拒绝且不补发、后续不受影响", scenario_3_expired_intent_not_replayed),
    ("S4 预算耗尽：每分钟 3 个，第 4 个起拒绝", scenario_4_budget_exhausted),
    ("S5 protected_online 硬锁：人工确认也无效", scenario_5_protected_hard_lock_beats_manual_gate),
]


def main() -> int:
    """运行全部场景；全部通过返回 0，否则返回 1。"""
    print("=== E08 策略守卫与安全输入契约演示（M0） ===")
    header = f"{'场景':<44}{'allow':<7}{'拒绝原因':<52}{'真实输入':<10}{'判定':<6}"
    print(header)
    print("-" * len(header))
    passed = 0
    for name, run in SCENARIOS:
        ok, mode, reasons, real_count = run()
        passed += 1 if ok else 0
        print(
            f"{name:<44}{mode:<7}{(reasons or '(allow)'):<52}"
            f"{real_count:<10}{'PASS' if ok else 'FAIL'}"
        )
    print("-" * len(header))
    print(f"总计：{passed}/{len(SCENARIOS)} 通过")
    return 0 if passed == len(SCENARIOS) else 1


if __name__ == "__main__":
    sys.exit(main())
