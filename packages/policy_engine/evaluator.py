"""策略求值器（POL-001）：默认拒绝，拒绝必带机器可读原因。

evaluate(batch, mode, target, foreground, budget_state, clock) -> PolicyDecision，
拒绝代码（reasons，机器可读）：
- mode_not_allowed：未知模式 / observe|shadow|dry_run 不产生真实输入 /
  当前模式风险超过策略允许上限；
- protected_online_no_real_input：受保护在线目标硬锁（POL-003，任何参数、
  任何人工确认都不可绕过）；
- manual_gate_required：real_input 缺少人工闸门确认（POL-002）；
- foreground_mismatch：前台窗口与会话绑定/批次目标不符（SAFE-001）；
- session_mismatch：前台绑定会话与批次会话不符；
- missing_foreground_context / missing_target_context：上下文缺失（默认拒绝）；
- expired_intent：批次或任一意图已过 TTL（SAFE-003）；
- runtime_limit_reached：运行时长硬上限（SAFE-014）；
- budget_exhausted:actions_per_minute / budget_exhausted:total_actions：
  动作预算耗尽（SAFE-012）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from common.clock import Clock, MonotonicClock

from policy_engine.budgets import BudgetTracker
from policy_engine.models import ForegroundContext, PolicyInput, TargetRef, WAIT_KIND
from policy_engine.modes import ModeGate, RunMode, mode_risk, parse_mode

#: 受保护目标拒绝代码（硬锁）。
PROTECTED_LOCK: str = "protected_online_no_real_input"


@dataclass(frozen=True)
class PolicyDecision:
    """策略决策结果：allow + 机器可读原因（供审计与轨迹记录）。

    mode 字段带出本次评估使用的模式，InputBroker 以此做二次模式校验。
    """

    allow: bool
    reasons: list[str] = field(default_factory=list)
    mode: str = ""
    batch_id: str = ""
    session_id: str = ""
    checked_at_monotonic: float = 0.0

    @classmethod
    def allowed(
        cls, mode: str | RunMode, batch: Any, now: float
    ) -> "PolicyDecision":
        """构造一个允许决策。"""
        return cls(
            allow=True,
            reasons=[],
            mode=str(getattr(mode, "value", mode)),
            batch_id=str(getattr(batch, "batch_id", "")),
            session_id=str(getattr(batch, "session_id", "")),
            checked_at_monotonic=now,
        )


class PolicyEvaluator:
    """默认拒绝的策略求值器。

    构造参数：
    - policy：策略配置（模式上限与预算默认值）；
    - gate：人工闸门（real_input 必需；None 时 real_input 一律拒绝）。
    """

    def __init__(
        self,
        policy: PolicyInput | None = None,
        *,
        gate: ModeGate | None = None,
    ) -> None:
        self.policy = policy if policy is not None else PolicyInput()
        self.gate = gate

    def evaluate(
        self,
        batch: Any,
        mode: str | RunMode,
        target: TargetRef | None,
        foreground: ForegroundContext | None = None,
        budget_state: BudgetTracker | None = None,
        clock: Clock | None = None,
    ) -> PolicyDecision:
        """评估一个批次；聚合全部拒绝原因（可审计），全部通过才允许。"""
        now = float(clock.now()) if clock is not None else float(MonotonicClock().now())
        reasons: list[str] = []

        current = parse_mode(mode)
        if current is None:
            reasons.append("mode_not_allowed")
        elif current is not RunMode.REAL_INPUT:
            # observe/shadow/dry_run：真实意图不产生任何输入。
            reasons.append("mode_not_allowed")
        if current is not None and mode_risk(current) > mode_risk(self.policy.mode):
            # 当前模式风险超过策略允许上限（脚本不能自行提高策略上限）。
            reasons.append("mode_not_allowed")

        if target is None:
            reasons.append("missing_target_context")

        if current is RunMode.REAL_INPUT:
            # POL-003 硬锁：先于一切检查，不受人工确认或任何参数影响。
            if target is not None and target.protected_online:
                reasons.append(PROTECTED_LOCK)
            # POL-002 人工闸门：real_input 必须有显式确认。
            if self.gate is None or not self.gate.is_confirmed(
                str(getattr(batch, "session_id", ""))
            ):
                reasons.append("manual_gate_required")

        reasons.extend(self._foreground_reasons(batch, foreground))

        if batch.expired(now) or any(i.expired(now) for i in batch.intents):
            reasons.append("expired_intent")

        action_count = sum(
            1 for i in batch.intents if getattr(i, "kind", "") != WAIT_KIND
        )
        if budget_state is not None:
            reasons.extend(budget_state.preview_reasons(action_count, now=now))

        allow = not reasons
        if allow and budget_state is not None and action_count > 0:
            budget_state.consume(action_count, now=now)
        return PolicyDecision(
            allow=allow,
            reasons=reasons,
            mode=str(getattr(current, "value", current)) if current else str(mode),
            batch_id=str(getattr(batch, "batch_id", "")),
            session_id=str(getattr(batch, "session_id", "")),
            checked_at_monotonic=now,
        )

    def _foreground_reasons(
        self, batch: Any, foreground: ForegroundContext | None
    ) -> list[str]:
        """前台校验（SAFE-001）：当前前台必须与批次目标及会话绑定一致。"""
        if foreground is None:
            return ["missing_foreground_context"]
        batch_target = str(getattr(batch, "target_id", ""))
        if foreground.target_id is None or foreground.target_id != batch_target:
            return ["foreground_mismatch"]
        reasons: list[str] = []
        if (
            foreground.expected_target_id is not None
            and foreground.expected_target_id != batch_target
        ):
            reasons.append("foreground_mismatch")
        if (
            foreground.expected_pid is not None
            and foreground.pid != foreground.expected_pid
        ):
            reasons.append("foreground_mismatch")
        if (
            foreground.expected_session_id is not None
            and foreground.expected_session_id != str(getattr(batch, "session_id", ""))
        ):
            reasons.append("session_mismatch")
        return reasons


def summarize_reasons(reasons: Sequence[str]) -> str:
    """把原因列表合并为单行字符串（轨迹/日志用）。"""
    return ";".join(reasons)
