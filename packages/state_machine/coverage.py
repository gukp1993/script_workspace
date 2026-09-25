"""运行覆盖率统计（FSM-010）。

统计四类计数：状态进入、迁移触发、守卫评估（含为真次数）、异常路径。
``as_dict`` 返回纯 dict（键排序），供轨迹记录与测试断言。
"""

from __future__ import annotations

__all__ = ["CoverageTracker"]


class CoverageTracker:
    """状态机运行覆盖率的累计器。"""

    def __init__(self) -> None:
        self.states_entered: dict[str, int] = {}
        self.transitions_fired: dict[str, int] = {}
        self.guards_evaluated: dict[str, int] = {}
        self.guards_true: dict[str, int] = {}
        self.exception_paths: dict[str, int] = {}

    def record_state_entered(self, state: str) -> None:
        """记录一次状态进入（含初始状态）。"""
        self.states_entered[state] = self.states_entered.get(state, 0) + 1

    def record_transition(self, source: str, target: str) -> None:
        """记录一次迁移触发（键为 ``源->目标``；超时/断言/异常路由同样计入）。"""
        key = f"{source}->{target}"
        self.transitions_fired[key] = self.transitions_fired.get(key, 0) + 1

    def record_guard(self, state: str, when_src: str, value: bool) -> None:
        """记录一次守卫评估（及是否为真）。"""
        self.guards_evaluated[state] = self.guards_evaluated.get(state, 0) + 1
        if value:
            self.guards_true[state] = self.guards_true.get(state, 0) + 1

    def record_exception(self, state: str) -> None:
        """记录一次异常路径（动作构造/发出失败被重试或路由）。"""
        self.exception_paths[state] = self.exception_paths.get(state, 0) + 1

    def as_dict(self) -> dict[str, dict[str, int]]:
        """导出为纯 dict（各节按键排序，便于快照对比）。"""

        def sorted_copy(mapping: dict[str, int]) -> dict[str, int]:
            return {k: mapping[k] for k in sorted(mapping)}

        return {
            "states_entered": sorted_copy(self.states_entered),
            "transitions_fired": sorted_copy(self.transitions_fired),
            "guards_evaluated": sorted_copy(self.guards_evaluated),
            "guards_true": sorted_copy(self.guards_true),
            "exception_paths": sorted_copy(self.exception_paths),
        }
