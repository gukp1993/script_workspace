"""断言设施：wait_until / assert_after（FSM-007）。

- 断言在后续 tick 中评估（同一确定性求值器，字段缺失取安全默认）；
- 条件为真 -> 满足并移除；超过 timeout 仍为假 -> 失败，由运行时把机器
  迁入配置的出口状态（``to``）并保留证据（快照存入
  ``MachineRuntime.last_assert_failure``）；
- ``wait_until`` 挂起期间阻塞正常迁移选择（"等待"语义）；
  ``assert_after`` 不阻塞（机器继续运行，失败时改道）；
- 断言与其所属状态的驻留绑定：离开状态即丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass

from common.clock import Clock
from domain_model.dsl import ExprAST, ExprEval
from domain_model.models import PerceptionSnapshot

__all__ = ["PendingAssertion", "AssertionManager"]


@dataclass
class PendingAssertion:
    """一条挂起中的断言。"""

    kind: str  # "wait_until" | "assert_after"
    ast: ExprAST
    when_src: str
    timeout_seconds: float
    failure_state: str
    message: str
    created_at: float


class AssertionManager:
    """当前状态驻留期的断言登记与逐 tick 评估。"""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._pending: list[PendingAssertion] = []

    @property
    def blocking(self) -> bool:
        """是否存在阻塞迁移选择的 wait_until 断言。"""
        return any(a.kind == "wait_until" for a in self._pending)

    @property
    def pending(self) -> tuple[PendingAssertion, ...]:
        """当前全部挂起断言（登记顺序）。"""
        return tuple(self._pending)

    def register(
        self,
        *,
        kind: str,
        ast: ExprAST,
        when_src: str,
        timeout_seconds: float,
        failure_state: str,
        message: str = "",
    ) -> PendingAssertion:
        """登记一条断言（entry/exit 动作执行到该步时调用）。"""
        assertion = PendingAssertion(
            kind=kind,
            ast=ast,
            when_src=when_src,
            timeout_seconds=timeout_seconds,
            failure_state=failure_state,
            message=message,
            created_at=self._clock.now(),
        )
        self._pending.append(assertion)
        return assertion

    def remove(self, assertion: PendingAssertion) -> None:
        """移除一条断言（已满足或已失败）。"""
        try:
            self._pending.remove(assertion)
        except ValueError:  # pragma: no cover - 防御
            pass

    def clear(self) -> None:
        """丢弃全部断言（离开状态时调用：断言与驻留绑定）。"""
        self._pending.clear()

    def evaluate(
        self, snapshot: PerceptionSnapshot, evaluator: ExprEval
    ) -> tuple[list[PendingAssertion], PendingAssertion | None]:
        """评估全部挂起断言。

        Returns:
            (本 tick 满足的断言列表, 首个超时失败的断言或 None)。
            评估顺序 = 登记顺序，失败只取第一个（确定性）。
        """
        satisfied: list[PendingAssertion] = []
        failed: PendingAssertion | None = None
        for assertion in self._pending:
            if bool(evaluator.evaluate(assertion.ast, snapshot)):
                satisfied.append(assertion)
                continue
            if (
                failed is None
                and self._clock.now() - assertion.created_at >= assertion.timeout_seconds
            ):
                failed = assertion
        return satisfied, failed
