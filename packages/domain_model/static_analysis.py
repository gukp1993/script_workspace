"""DOM-007 项目级静态分析（文档 §5.1：静态检查是发布前提）。

规则清单（rule ID -> severity）：
- ``state_no_exit``               error  非终态无迁移、无 on_timeout_to、无 timeout
                                         （与 domain_model.validation 同名规则同语义；
                                         AC-P0-10：定位到该状态并阻断编译）
- ``state_unreachable``           error  状态从 initial 不可达
- ``transition_conflict``         warning 同状态两个迁移恒可同时为真（静态可判定时）
- ``reference_missing_field``     error  when 引用未定义的感知字段
- ``reference_missing_asset``     error  检测器引用的模板文件/清单哈希缺失或不一致
- ``action_unauthorized``         error  动作类型不在注册能力表（DOM-005）
- ``protected_online_unattended`` error  受保护在线目标 + unattended enabled（硬锁）
- ``protected_online_no_real_input`` error 受保护在线目标 + real_input 模式（硬锁）
- ``real_input_no_unattended``    error  real_input 模式 + 无人值守（默认拒绝）
- ``infinite_retry``              error  重试没有有界上限
- ``state_target_missing``        error  迁移/超时/断言/异常出口目标状态不存在
- ``condition_parse_error``       error  条件表达式无法被白名单解析器解析
- ``assertion_invalid``           error  断言动作缺 when / 超时不合法
- ``initial_state_missing``       error  初始状态未定义（模型不变式）

用法：
- ``analyze(project_dir_or_bundle)`` 返回 :class:`AnalysisIssue` 列表
  （:class:`AnalysisIssue` 是 :class:`~domain_model.errors.Issue` 的子类，
  附加 ``severity`` 字段，可当作普通 Issue 使用）；
- 存在 severity=error 的问题时项目不能编译运行：
  ``ensure_compilable`` 在 compile 前置调用 analyze 并阻断（AC-P0-10）；
  :func:`domain_model.dsl.compile_machine_dict` 的 ``project_dir`` 参数
  同样会先执行 analyze 并阻断。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from domain_model.capabilities import CAPABILITY_REGISTRY
from domain_model.dsl import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    compile_with_diagnostics,
)
from domain_model.errors import DomainValidationError, Issue
from domain_model.models import PolicyMode, UnattendedSchedule
from domain_model.parsing import ProjectBundle, load_project

__all__ = [
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "AnalysisIssue",
    "analyze",
    "ensure_compilable",
    "has_errors",
]


@dataclass(frozen=True)
class AnalysisIssue(Issue):
    """带严重级别的静态分析问题（Issue 的只增扩展，兼容一切 Issue 用法）。"""

    severity: str = SEVERITY_ERROR

    @property
    def blocking(self) -> bool:
        """是否阻断编译/运行。"""
        return self.severity == SEVERITY_ERROR


def _as_analysis(issues: list[Issue]) -> list[AnalysisIssue]:
    """把普通 Issue 升格为 error 级 AnalysisIssue。"""
    return [
        AnalysisIssue(
            file=i.file, pointer=i.pointer, rule=i.rule, message=i.message, hint=i.hint,
            severity=SEVERITY_ERROR,
        )
        for i in issues
    ]


def _check_machines(
    bundle: ProjectBundle, known_fields: set[str]
) -> list[AnalysisIssue]:
    """复用编译器的结构诊断；其中迁移冲突在分析视图中降级为告警。"""
    out: list[AnalysisIssue] = []
    for machine in sorted(bundle.machines.values(), key=lambda m: m.machine_id):
        _, diags = compile_with_diagnostics(
            machine,
            hints=None,
            file=machine.source_file,
            known_fields=known_fields,
        )
        for issue, severity in diags:
            if issue.rule == "transition_conflict":
                severity = SEVERITY_WARNING
            out.append(
                AnalysisIssue(
                    file=issue.file, pointer=issue.pointer, rule=issue.rule,
                    message=issue.message, hint=issue.hint, severity=severity,
                )
            )
    return out


def _norm_rel(path: str) -> str:
    """相对路径统一为 ``/`` 分隔。"""
    return path.replace("\\", "/")


def _sha256_of(path: Path) -> str | None:
    """文件 SHA-256；读取失败返回 None。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _check_assets(bundle: ProjectBundle) -> list[AnalysisIssue]:
    """检测器引用的模板资产：文件存在、已登记清单、哈希一致。"""
    out: list[AnalysisIssue] = []
    by_path: dict[str, object] = {}
    for asset in bundle.assets.values():
        by_path.setdefault(_norm_rel(asset.path), asset)

    for detector in sorted(bundle.detectors.values(), key=lambda d: d.detector_id):
        if detector.template is None:
            continue
        norm = _norm_rel(detector.template)
        target = bundle.root / norm
        if not target.is_file():
            out.append(
                AnalysisIssue(
                    file=detector.source_file,
                    pointer="/template",
                    rule="reference_missing_asset",
                    message=f"检测器 {detector.detector_id!r} 引用的模板文件不存在：{detector.template}",
                    hint="补齐模板文件，或把 template 改为 assets/ 下已存在的相对路径",
                )
            )
            continue
        asset = by_path.get(norm)
        if asset is None:
            out.append(
                AnalysisIssue(
                    file=detector.source_file,
                    pointer="/template",
                    rule="reference_missing_asset",
                    message=f"模板 {detector.template!r} 未登记到 assets/assets.yaml 清单",
                    hint="在资产清单中补充该文件的 asset_id/sha256/version/kind 条目",
                )
            )
            continue
        actual = _sha256_of(target)
        listed = asset.sha256  # type: ignore[union-attr]
        if actual is not None and actual != listed:
            out.append(
                AnalysisIssue(
                    file=asset.source_file,  # type: ignore[union-attr]
                    pointer=f"/assets/{asset.asset_id}/sha256",  # type: ignore[union-attr]
                    rule="reference_missing_asset",
                    message=f"资产 {asset.asset_id!r}（{detector.template}）的 sha256 与实际文件不一致",
                    hint="用文件真实 sha256 更新清单，或重新导出模板",
                )
            )
    return out


def _check_protected_locks(bundle: ProjectBundle) -> list[AnalysisIssue]:
    """受保护在线目标硬锁 + real_input 与无人值守互斥（默认拒绝）。"""
    out: list[AnalysisIssue] = []
    if bundle.has_protected_target:
        for policy in sorted(bundle.policies.values(), key=lambda p: p.policy_id):
            if policy.mode_value is PolicyMode.REAL_INPUT:
                out.append(
                    AnalysisIssue(
                        file=policy.source_file,
                        pointer="/mode",
                        rule="protected_online_no_real_input",
                        message=(f"项目包含受保护在线目标（protected_online: true），"
                                 f"策略 {policy.policy_id!r} 不得使用 real_input 模式"),
                        hint="受保护在线目标只允许 observe/shadow/dry_run",
                    )
                )
            if policy.unattended_value is UnattendedSchedule.ENABLED:
                out.append(
                    AnalysisIssue(
                        file=policy.source_file,
                        pointer="/unattended_schedule",
                        rule="protected_online_unattended",
                        message=(f"项目包含受保护在线目标，策略 {policy.policy_id!r} "
                                 f"不得启用无人值守调度"),
                        hint="设置 unattended_schedule: disabled（受保护目标必须 disabled）",
                    )
                )
        return out
    for policy in sorted(bundle.policies.values(), key=lambda p: p.policy_id):
        if (
            policy.mode_value is PolicyMode.REAL_INPUT
            and policy.unattended_value is UnattendedSchedule.ENABLED
        ):
            out.append(
                AnalysisIssue(
                    file=policy.source_file,
                    pointer="/unattended_schedule",
                    rule="real_input_no_unattended",
                    message=f"策略 {policy.policy_id!r} 为 real_input 模式却启用了无人值守调度",
                    hint="真实输入必须有人在场：设置 unattended_schedule: disabled",
                )
            )
    return out


def _check_capabilities(bundle: ProjectBundle) -> list[AnalysisIssue]:
    """防御性复核：动作映射到的能力必须已注册（正常由编译器拦截）。"""
    from domain_model.dsl import ACTION_KIND_CAPABILITY

    out: list[AnalysisIssue] = []
    for machine in sorted(bundle.machines.values(), key=lambda m: m.machine_id):
        for state_name, state in sorted(machine.states.items()):
            for role, actions in (("entry", state.entry), ("exit", state.exit)):
                for idx, act in enumerate(actions):
                    capability = ACTION_KIND_CAPABILITY.get(act.kind)
                    if capability is not None and capability not in CAPABILITY_REGISTRY:
                        out.append(
                            AnalysisIssue(
                                file=machine.source_file,
                                pointer=f"/states/{state_name}/{role}/{idx}/kind",
                                rule="action_unauthorized",
                                message=f"动作 {act.kind!r} 映射到未注册能力 {capability!r}",
                            )
                        )
    return out


def _analyze_bundle(bundle: ProjectBundle) -> list[AnalysisIssue]:
    """对解析干净的项目聚合根执行全部静态规则。"""
    known_fields = bundle.perception_field_names
    out: list[AnalysisIssue] = []
    out.extend(_check_machines(bundle, known_fields))
    out.extend(_check_assets(bundle))
    out.extend(_check_protected_locks(bundle))
    out.extend(_check_capabilities(bundle))
    return out


def analyze(target: str | Path | ProjectBundle) -> list[AnalysisIssue]:
    """对一个项目目录或 :class:`ProjectBundle` 执行静态分析（DOM-007）。

    - 传目录：先 ``load_project``（结构错误原样作为 error 级问题返回）；
    - 传聚合根：直接分析。
    """
    if isinstance(target, ProjectBundle):
        return _analyze_bundle(target)
    root = Path(target)
    if not root.is_dir():
        return [
            AnalysisIssue(
                file=str(root), pointer="", rule="invalid_target",
                message=f"静态分析目标必须是项目目录或 ProjectBundle：{root}",
            )
        ]
    try:
        bundle = load_project(root)
    except DomainValidationError as exc:
        return _as_analysis(exc.issues)
    return _analyze_bundle(bundle)


def has_errors(issues: list[AnalysisIssue]) -> bool:
    """问题列表中是否存在 error 级（阻断级）问题。"""
    return any(i.severity == SEVERITY_ERROR for i in issues)


def ensure_compilable(target: str | Path | ProjectBundle) -> list[AnalysisIssue]:
    """compile 前置门：analyze 后若存在 error 级问题则抛出阻断异常。

    Returns:
        全部问题（含 warning）——调用方仅在未抛异常时拿到。
    Raises:
        DomainValidationError: 存在 severity=error 的问题（AC-P0-10）。
    """
    issues = analyze(target)
    errors = [i for i in issues if i.severity == SEVERITY_ERROR]
    if errors:
        raise DomainValidationError(
            [Issue(file=i.file, pointer=i.pointer, rule=i.rule, message=i.message, hint=i.hint)
             for i in errors]
        )
    return issues
