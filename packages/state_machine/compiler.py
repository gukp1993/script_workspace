"""状态机编译器（FSM-002）：StateMachineDef / YAML / dict -> CompiledMachine。

编译产物是"运行图"：状态 -> 排好序的迁移表 + 分类后的动作序列；
所有编译错误映射回源位置（文件 + JSON Pointer + 表达式内行列）。
编译入口由 :mod:`domain_model.dsl` 提供（避免包间反向依赖），本模块
按 FSM-002 的职责聚合转发，并提供从项目聚合根编译的便捷入口。
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
    compile_state_machine,
)
from domain_model.errors import DomainValidationError, Issue
from domain_model.parsing import ProjectBundle

__all__ = [
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
]


def compile_project_machine(bundle: ProjectBundle, machine_id: str) -> CompiledMachine:
    """从项目聚合根编译指定状态机；感知字段以项目 detectors 输出为准。

    Raises:
        DomainValidationError: 状态机不存在，或编译诊断含 error 级问题。
    """
    defn = bundle.machines.get(machine_id)
    if defn is None:
        raise DomainValidationError(
            [
                Issue(
                    file=str(bundle.root),
                    pointer="",
                    rule="machine_missing",
                    message=f"项目中不存在状态机 {machine_id!r}",
                    hint=f"已定义的状态机：{sorted(bundle.machines)}",
                )
            ]
        )
    return compile_state_machine(
        defn, known_fields=bundle.perception_field_names, file=defn.source_file
    )
