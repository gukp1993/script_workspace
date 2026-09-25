"""策略输入与上下文的最小数据结构（POL-001）。

字段与 domain_model 的 PolicyProfile / TargetProfile / 前台窗口快照对齐，
M1 契约测试统一；本包不 import domain_model（并行开发解耦）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: 等待类意图的 kind 值：不计入动作数预算（与 input_broker.IntentKind.WAIT 对齐）。
WAIT_KIND: Final[str] = "wait"


@dataclass(frozen=True)
class PolicyInput:
    """策略配置输入（对应 domain_model.PolicyProfile 的运行期字段）。

    mode 语义：策略允许的最高运行模式（风险上限，保守默认 observe）。
    运行时实际模式由 ModeGate / 控制面决定，evaluator 会校验
    "当前模式风险 <= 策略允许上限"。
    """

    mode: str = "observe"
    require_manual_start: bool = True
    max_runtime_minutes: float = 20.0
    max_actions_per_minute: int = 120
    max_total_actions: int = 10000
    #: M0 仅承载字段（"disabled"/"enabled"）；无人值守时间窗调度在 M1 实现。
    unattended_schedule: str = "disabled"


@dataclass(frozen=True)
class TargetRef:
    """目标引用（对应 domain_model.TargetProfile 的策略相关字段）。"""

    target_id: str
    protected_online: bool = False
    title_regex: str = ""


@dataclass
class ForegroundContext:
    """前台窗口上下文：当前前台事实 + 会话绑定的期望值。

    - target_id/pid/hwnd：当前前台窗口的标识（None 表示无前台目标/桌面）；
    - expected_*：会话启动时绑定的期望值，evaluator 逐项比对，
      任一不符即 foreground_mismatch（SAFE-001）。
    """

    target_id: str | None = None
    pid: int | None = None
    hwnd: int | None = None
    expected_target_id: str | None = None
    expected_pid: int | None = None
    expected_session_id: str | None = None
