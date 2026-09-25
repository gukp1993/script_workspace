"""policy_engine——策略守卫包（E08）。

POL-001 默认拒绝求值器；POL-002 模式机与人工闸门；
POL-003 受保护在线目标真实输入硬锁；POL-004/005 最小预算；
demo 提供 5 个内置安全场景演示（python -m policy_engine.demo）。
"""

from policy_engine.budgets import BudgetState, BudgetTracker, MINUTE_SECONDS
from policy_engine.evaluator import (
    PROTECTED_LOCK,
    PolicyDecision,
    PolicyEvaluator,
    summarize_reasons,
)
from policy_engine.models import (
    ForegroundContext,
    PolicyInput,
    TargetRef,
    WAIT_KIND,
)
from policy_engine.modes import (
    MODE_RISK,
    GateConfirmation,
    GateRefusal,
    ModeGate,
    RunMode,
    mode_risk,
    parse_mode,
)

__all__ = [
    "MODE_RISK",
    "MINUTE_SECONDS",
    "PROTECTED_LOCK",
    "BudgetState",
    "BudgetTracker",
    "ForegroundContext",
    "GateConfirmation",
    "GateRefusal",
    "ModeGate",
    "PolicyDecision",
    "PolicyEvaluator",
    "PolicyInput",
    "RunMode",
    "TargetRef",
    "WAIT_KIND",
    "mode_risk",
    "parse_mode",
    "summarize_reasons",
]
