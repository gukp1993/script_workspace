"""state_machine——确定性状态机运行时（FSM-002~010，M2/E07）。

- 编译：YAML/dict/:class:`StateMachineDef` -> :class:`CompiledMachine` 运行图
  （错误带源位置；非终态无出口等 error 级问题阻断编译，AC-P0-10）；
- 运行：:class:`MachineRuntime` 逐 tick 确定性迁移，只构造
  :class:`~input_broker.intents.InputIntent` 并经回调交出，绝不执行输入；
- 确定性：同版本 + 同快照轨迹 + 同种子 -> 完全相同的 decision_hash 序列
  （FSM-009，AC-P0-08 的基础）。

安全边界：本包不 import ctypes/win32*、不含 exec/eval/compile、
不调用 time.sleep（定时行为全部基于注入 Clock）。
"""

from __future__ import annotations

from domain_model.dsl import (
    CompiledAction,
    CompiledAssertion,
    CompiledMachine,
    CompiledState,
    CompiledTransition,
    RetrySpec,
    compile_machine_dict,
    compile_machine_yaml,
)
from state_machine.assertions import AssertionManager, PendingAssertion
from state_machine.compiler import compile_project_machine, compile_state_machine
from state_machine.control import RuntimeController
from state_machine.coverage import CoverageTracker
from state_machine.retry import RetryController, backoff_seconds
from state_machine.runtime import MachineRuntime, TickResult, canonical_snapshot
from state_machine.semantics import GuardOutcome, evaluate_guards, select_transition
from state_machine.timers import StateTimer

__all__ = [
    # 编译（FSM-002）
    "CompiledMachine",
    "CompiledState",
    "CompiledTransition",
    "CompiledAction",
    "CompiledAssertion",
    "RetrySpec",
    "compile_machine_dict",
    "compile_machine_yaml",
    "compile_state_machine",
    "compile_project_machine",
    # 运行（FSM-003/004/009）
    "MachineRuntime",
    "TickResult",
    "canonical_snapshot",
    "GuardOutcome",
    "evaluate_guards",
    "select_transition",
    # 定时（FSM-005）
    "StateTimer",
    # 控制（FSM-006）
    "RuntimeController",
    # 断言（FSM-007）
    "AssertionManager",
    "PendingAssertion",
    # 重试（FSM-008）
    "RetryController",
    "backoff_seconds",
    # 覆盖率（FSM-010）
    "CoverageTracker",
]
