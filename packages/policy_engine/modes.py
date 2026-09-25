"""运行模式机与人工闸门（POL-002）。

- RunMode：observe / shadow / dry_run / real_input 四级风险模式；
- ModeGate：切到 real_input 必须显式 confirm(session_id, operator)，
  生成含审计字段的人工闸门记录；受保护目标永远无法确认通过；
- observe/shadow/dry_run 不产生任何 OS 输入（由 evaluator 强制）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from common.clock import Clock, MonotonicClock
from common.ids import new_id

from policy_engine.models import TargetRef


class RunMode(str, Enum):
    """运行模式（风险从低到高）。"""

    OBSERVE = "observe"
    SHADOW = "shadow"
    DRY_RUN = "dry_run"
    REAL_INPUT = "real_input"


#: 模式风险序：evaluator 用它校验"当前模式 <= 策略允许上限"。
MODE_RISK: dict[RunMode, int] = {
    RunMode.OBSERVE: 0,
    RunMode.SHADOW: 1,
    RunMode.DRY_RUN: 2,
    RunMode.REAL_INPUT: 3,
}


def mode_risk(mode: str | RunMode) -> int:
    """返回模式风险序；未知模式返回 -1（保证默认拒绝路径生效）。"""
    value = str(getattr(mode, "value", mode))
    try:
        return MODE_RISK[RunMode(value)]
    except KeyError:
        return -1


def parse_mode(mode: str | RunMode) -> RunMode | None:
    """解析模式；未知模式返回 None（调用方按默认拒绝处理）。"""
    if isinstance(mode, RunMode):
        return mode
    try:
        return RunMode(str(mode))
    except ValueError:
        return None


@dataclass(frozen=True)
class GateConfirmation:
    """人工闸门确认记录（审计字段）。"""

    gate_id: str
    session_id: str
    operator: str
    target_id: str | None
    mode: str
    confirmed_at_monotonic: float
    note: str = ""


@dataclass(frozen=True)
class GateRefusal:
    """人工闸门拒绝记录（审计字段）。"""

    session_id: str
    operator: str
    target_id: str | None
    mode: str
    reason: str
    refused_at_monotonic: float


@dataclass
class ModeGate:
    """real_input 人工闸门。

    约定：
    - 只有 real_input 需要闸门；confirm 不带 target 时仅代表会话级
      人工启动确认（target_id=None 入审计记录）；
    - confirm 显式传入受保护目标时拒绝（protected_online_no_real_input），
      受保护目标永远无法通过闸门解锁真实输入；
    - 硬锁本体在 evaluator（protected_online_no_real_input），
      即使确认状态被伪造/绕过，真实输入仍会被拒绝（纵深防御）。
    """

    clock: Clock = field(default_factory=MonotonicClock)

    def __post_init__(self) -> None:
        self.records: list[GateConfirmation] = []
        self.refusals: list[GateRefusal] = []

    def confirm(
        self,
        session_id: str,
        operator: str,
        *,
        target: TargetRef | None = None,
        mode: str | RunMode = RunMode.REAL_INPUT,
        note: str = "",
    ) -> GateConfirmation | None:
        """记录一次人工确认；成功返回审计记录，被拒返回 None（拒绝同样入审计）。"""
        now = float(self.clock.now())
        mode_value = str(getattr(mode, "value", mode))
        if not operator.strip():
            self.refusals.append(
                GateRefusal(session_id, operator, _target_id(target), mode_value,
                            "operator_required", now)
            )
            return None
        if mode_value != RunMode.REAL_INPUT.value:
            self.refusals.append(
                GateRefusal(session_id, operator, _target_id(target), mode_value,
                            "gate_only_for_real_input", now)
            )
            return None
        if target is not None and target.protected_online:
            self.refusals.append(
                GateRefusal(session_id, operator, target.target_id, mode_value,
                            "protected_online_no_real_input", now)
            )
            return None
        confirmation = GateConfirmation(
            gate_id=new_id("gate"),
            session_id=session_id,
            operator=operator.strip(),
            target_id=target.target_id if target is not None else None,
            mode=mode_value,
            confirmed_at_monotonic=now,
            note=note,
        )
        self.records.append(confirmation)
        return confirmation

    def is_confirmed(self, session_id: str) -> bool:
        """会话是否存在有效的 real_input 人工确认记录。"""
        return any(
            r.session_id == session_id and r.mode == RunMode.REAL_INPUT.value
            for r in self.records
        )

    def confirmations(self) -> Sequence[GateConfirmation]:
        """只读审计序列。"""
        return tuple(self.records)

    def refusal_reasons(self) -> Sequence[str]:
        """全部拒绝原因（按发生顺序）。"""
        return tuple(r.reason for r in self.refusals)


def _target_id(target: TargetRef | None) -> str | None:
    return target.target_id if target is not None else None
