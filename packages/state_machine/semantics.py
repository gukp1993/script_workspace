"""迁移选择语义（FSM-003）：确定性的守卫评估与迁移选择。

确定性契约：
- 迁移表在编译期已按 ``(priority, 声明顺序)`` 排序（数值越小越先）；
- 评估按该顺序逐条进行，选中**第一条**守卫为真且稳定帧数满足的迁移
  后立即短路（同一次 tick 内后续迁移不再评估）；
- 同一快照 + 同一稳定计数状态 -> 同一选择结果；
- 守卫求值绝不抛异常（ExprEval 对缺失字段取安全默认）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, MutableMapping

from domain_model.dsl import CompiledState, CompiledTransition, ExprEval
from domain_model.models import PerceptionSnapshot

__all__ = ["GuardOutcome", "evaluate_guards", "select_transition"]

#: 稳定计数键：(状态名, 迁移声明下标)
StableKey = tuple[str, int]
#: 守卫评估回调（状态名, 条件原文, 是否为真）——供覆盖率统计
GuardCallback = Callable[[str, str, bool], None]


@dataclass(frozen=True)
class GuardOutcome:
    """单条迁移的本 tick 守卫评估结果。"""

    transition: CompiledTransition
    value: bool
    stable_count: int
    stable_frames: int

    @property
    def selected(self) -> bool:
        """是否被选中（为真且连续满足帧数达标）。"""
        return self.value and self.stable_count >= self.stable_frames


def evaluate_guards(
    state: CompiledState,
    snapshot: PerceptionSnapshot,
    evaluator: ExprEval,
    stable: MutableMapping[StableKey, int],
    *,
    on_guard: GuardCallback | None = None,
) -> tuple[GuardOutcome, ...]:
    """按编译期顺序评估当前状态全部迁移守卫（选中即短路）。

    - 守卫为真 -> 该迁移的连续计数 +1；为假 -> 计数清零（迁移级 stable_frames）；
    - 短路后未评估的迁移其计数保持不变（下一 tick 恢复评估）。
    """
    outcomes: list[GuardOutcome] = []
    for tr in state.transitions:
        value = bool(evaluator.evaluate(tr.ast, snapshot))
        key: StableKey = (state.name, tr.decl_index)
        count = stable.get(key, 0) + 1 if value else 0
        stable[key] = count
        if on_guard is not None:
            on_guard(state.name, tr.when_src, value)
        outcomes.append(
            GuardOutcome(transition=tr, value=value, stable_count=count, stable_frames=tr.stable_frames)
        )
        if value and count >= tr.stable_frames:
            break
    return tuple(outcomes)


def select_transition(outcomes: tuple[GuardOutcome, ...]) -> CompiledTransition | None:
    """从评估结果中选择迁移：第一条 selected 的迁移，否则 None。"""
    for outcome in outcomes:
        if outcome.selected:
            return outcome.transition
    return None
