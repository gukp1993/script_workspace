"""项目级校验：Schema 校验 + 跨对象引用检查 + 受保护目标硬锁（DOM-003）。

规则清单（规则 ID 出现在错误输出中）：
- ``schema_invalid``            JSON Schema 校验失败（DOM-002 六份 Schema）
- ``template_file_missing``     检测器引用的模板文件在磁盘上不存在
- ``template_not_in_manifest``  引用的模板未登记到 assets/assets.yaml 清单
- ``template_hash_mismatch``    清单记录的 sha256 与实际文件内容不一致
- ``perception_field_undefined`` 迁移 ``when`` 引用了未定义的感知字段
- ``perception_attribute_unknown`` ``when`` 引用了感知字段上不存在的子属性
- ``state_target_missing``      迁移目标状态（to / on_timeout_to）不存在
- ``state_unreachable``         状态从 initial 不可达
- ``state_no_exit``             非终态既没有迁移也没有超时
- ``initial_state_missing``     初始状态未定义
- ``protected_online_no_real_input``  受保护在线目标禁止 real_input 模式（硬锁）
- ``protected_online_no_unattended``  受保护在线目标禁止无人值守调度（硬锁）
- ``real_input_no_unattended``  real_input 模式禁止无人值守调度（默认拒绝）
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator

from domain_model.errors import DomainValidationError, Issue, join_pointer
from domain_model.models import OBSERVATION_ATTRIBUTES, Detector, PolicyMode, StateMachineDef, UnattendedSchedule
from domain_model.parsing import CONFIG_SUFFIXES, ProjectBundle, load_config_file, load_project
from domain_model.schemas import load_schema

#: 目录名 -> Schema 种类
_DIR_KINDS: dict[str, str] = {
    "targets": "target",
    "policies": "policy",
    "calibrations": "calibration",
    "detectors": "detector",
    "machines": "machine",
}

#: 条件表达式中需要忽略的保留字
_WHEN_KEYWORDS: frozenset[str] = frozenset({"true", "false", "null", "and", "or", "not", "in"})

#: 感知字段路径（如 health_ratio.value）的提取正则
_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


@dataclass
class ValidationResult:
    """一次项目校验的聚合结果。

    Attributes:
        root:            项目根目录。
        issues:          全部问题（空列表表示有效）。
        bundle:          全部通过时解析出的聚合根，否则为 None。
        objects_checked: 成功解析的领域对象数（用于 ``VALID: <n> objects checked``）。
    """

    root: Path
    issues: list[Issue] = field(default_factory=list)
    bundle: ProjectBundle | None = None

    @property
    def ok(self) -> bool:
        """项目是否通过全部校验。"""
        return not self.issues and self.bundle is not None

    @property
    def objects_checked(self) -> int:
        """成功解析的领域对象数量。"""
        return self.bundle.object_count() if self.bundle is not None else 0


def _ptr(*parts: str | int) -> str:
    """拼装 JSON Pointer，并对状态名等键做 ~// 转义。"""
    out = ""
    for part in parts:
        escaped = str(part).replace("~", "~0").replace("/", "~1")
        out += f"/{escaped}"
    return out


def _norm_rel(path: str) -> str:
    """把相对路径统一为 ``/`` 分隔，便于比较。"""
    return path.replace("\\", "/")


def _sha256_of(path: Path) -> str | None:
    """计算文件 SHA-256；读取失败返回 None。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 资产引用检查
# ---------------------------------------------------------------------------


def check_asset_references(bundle: ProjectBundle) -> list[Issue]:
    """检查每个检测器引用的模板资产：文件存在、已登记清单、哈希一致。"""
    issues: list[Issue] = []
    by_path: dict[str, str] = {}
    for asset in bundle.assets.values():
        by_path.setdefault(_norm_rel(asset.path), asset.asset_id)

    for detector in sorted(bundle.detectors.values(), key=lambda d: d.detector_id):
        if detector.template is None:
            continue
        pointer = _ptr("template")
        target = bundle.root / _norm_rel(detector.template)
        if not target.is_file():
            issues.append(
                Issue(
                    file=detector.source_file,
                    pointer=pointer,
                    rule="template_file_missing",
                    message=f"检测器 {detector.detector_id!r} 引用的模板文件不存在：{detector.template}",
                    hint="补齐模板文件，或把 template 改为 assets/ 下已存在的相对路径",
                )
            )
            continue
        asset_id = by_path.get(_norm_rel(detector.template))
        if asset_id is None:
            issues.append(
                Issue(
                    file=detector.source_file,
                    pointer=pointer,
                    rule="template_not_in_manifest",
                    message=f"模板 {detector.template!r} 未登记到 assets/assets.yaml 清单",
                    hint="在资产清单中补充该文件的 asset_id/sha256/version/kind 条目",
                )
            )
            continue
        actual = _sha256_of(target)
        listed = bundle.assets[asset_id].sha256
        if actual is not None and actual != listed:
            issues.append(
                Issue(
                    file=bundle.assets[asset_id].source_file,
                    pointer=_ptr("assets", asset_id, "sha256"),
                    rule="template_hash_mismatch",
                    message=f"资产 {asset_id!r}（{detector.template}）的 sha256 与实际文件不一致",
                    hint="用文件真实 sha256 更新清单，或重新导出模板",
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 状态机检查
# ---------------------------------------------------------------------------


def _extract_when_fields(when: str) -> list[tuple[str, str | None]]:
    """提取条件表达式引用的 (字段名, 子属性或 None) 列表（M0 仅静态提取，不求值）。"""
    refs: list[tuple[str, str | None]] = []
    for match in _PATH_RE.finditer(when):
        parts = match.group(0).split(".")
        base = parts[0]
        if base in _WHEN_KEYWORDS:
            continue
        refs.append((base, parts[1] if len(parts) > 1 else None))
    return refs


def check_machine(machine: StateMachineDef, known_fields: set[str]) -> list[Issue]:
    """检查单个状态机：迁移目标、可达性、非终态退出、感知字段引用。"""
    issues: list[Issue] = []
    file = machine.source_file
    states = machine.states

    # d) 迁移目标状态存在（含超时迁移目标）
    for state_name, state in states.items():
        for idx, tr in enumerate(state.transitions):
            if tr.to not in states:
                issues.append(
                    Issue(file=file, pointer=_ptr("states", state_name, "transitions", idx, "to"),
                          rule="state_target_missing",
                          message=f"迁移目标状态 {tr.to!r} 未在 states 中定义",
                          hint="修正 to 为已定义状态名，或补充该状态")
                )
            if tr.on_timeout_to is not None and tr.on_timeout_to not in states:
                issues.append(
                    Issue(file=file, pointer=_ptr("states", state_name, "transitions", idx, "on_timeout_to"),
                          rule="state_target_missing",
                          message=f"超时迁移目标状态 {tr.on_timeout_to!r} 未在 states 中定义",
                          hint="修正 on_timeout_to 为已定义状态名，或补充该状态")
                )
            # b) when 引用的感知字段必须在 detectors 输出语义中已定义
            for base, attr in _extract_when_fields(tr.when):
                if base not in known_fields:
                    issues.append(
                        Issue(file=file, pointer=_ptr("states", state_name, "transitions", idx, "when"),
                              rule="perception_field_undefined",
                              message=f"条件 {tr.when!r} 引用了未定义的感知字段 {base!r}",
                              hint=f"已定义的感知字段：{sorted(known_fields)}；在 detectors/ 中补充输出该字段的检测器")
                    )
                elif attr is not None and attr not in OBSERVATION_ATTRIBUTES:
                    issues.append(
                        Issue(file=file, pointer=_ptr("states", state_name, "transitions", idx, "when"),
                              rule="perception_attribute_unknown",
                              message=f"条件 {tr.when!r} 引用了字段 {base!r} 上不存在的子属性 {attr!r}",
                              hint=f"允许的子属性：{sorted(OBSERVATION_ATTRIBUTES)}")
                    )

    # c) 所有状态从 initial 可达
    reachable = {machine.initial}
    frontier = [machine.initial]
    while frontier:
        current = frontier.pop()
        state = states.get(current)
        if state is None:
            continue
        for tr in state.transitions:
            for target in (tr.to, tr.on_timeout_to):
                if target is not None and target in states and target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
    for state_name in sorted(states):
        if state_name not in reachable:
            issues.append(
                Issue(file=file, pointer=_ptr("states", state_name), rule="state_unreachable",
                      message=f"状态 {state_name!r} 从初始状态 {machine.initial!r} 不可达",
                      hint="补一条从可达状态出发的迁移，或删除该状态")
            )

    # c) 非终态必须有迁移、on_timeout_to 或 timeout_seconds 至少其一
    for state_name, state in states.items():
        if state.terminal:
            continue
        if not state.transitions and state.timeout_seconds is None:
            issues.append(
                Issue(file=file, pointer=_ptr("states", state_name), rule="state_no_exit",
                      message=f"非终态 {state_name!r} 没有任何迁移、超时迁移或 timeout_seconds，可能卡死",
                      hint="添加 transitions，或设置 timeout_seconds（配合 on_timeout_to）")
            )
    return issues


def check_machines(bundle: ProjectBundle) -> list[Issue]:
    """检查项目内全部状态机。"""
    known = bundle.perception_field_names
    issues: list[Issue] = []
    for machine in sorted(bundle.machines.values(), key=lambda m: m.machine_id):
        issues.extend(check_machine(machine, known))
    return issues


# ---------------------------------------------------------------------------
# 受保护目标硬锁（SAFE-020）
# ---------------------------------------------------------------------------


def check_protected_hard_locks(bundle: ProjectBundle) -> list[Issue]:
    """受保护在线目标硬锁：禁止 real_input 模式、禁止无人值守调度。"""
    issues: list[Issue] = []
    if not bundle.has_protected_target:
        # 无受保护目标时仍执行 real_input 与无人值守互斥的默认拒绝规则
        for policy in sorted(bundle.policies.values(), key=lambda p: p.policy_id):
            if policy.mode_value is PolicyMode.REAL_INPUT and policy.unattended_value is UnattendedSchedule.ENABLED:
                issues.append(
                    Issue(file=policy.source_file, pointer=_ptr("unattended_schedule"),
                          rule="real_input_no_unattended",
                          message=f"策略 {policy.policy_id!r} 为 real_input 模式却启用了无人值守调度",
                          hint="真实输入必须有人在场：设置 unattended_schedule: disabled")
                )
        return issues

    for policy in sorted(bundle.policies.values(), key=lambda p: p.policy_id):
        if policy.mode_value is PolicyMode.REAL_INPUT:
            issues.append(
                Issue(file=policy.source_file, pointer=_ptr("mode"),
                      rule="protected_online_no_real_input",
                      message=(f"项目包含受保护在线目标（protected_online: true），策略 {policy.policy_id!r} "
                               f"不得使用 real_input 模式"),
                      hint="受保护在线目标只允许 observe/shadow/dry_run；如确为本地自有目标，"
                           "把目标的 protected_online 改为 false 并确认合规")
            )
        if policy.unattended_value is UnattendedSchedule.ENABLED:
            issues.append(
                Issue(file=policy.source_file, pointer=_ptr("unattended_schedule"),
                      rule="protected_online_no_unattended",
                      message=(f"项目包含受保护在线目标，策略 {policy.policy_id!r} 不得启用无人值守调度"),
                      hint="设置 unattended_schedule: disabled（受保护目标必须 disabled）")
            )
    return issues


# ---------------------------------------------------------------------------
# Schema 校验与目录装配
# ---------------------------------------------------------------------------


def _discover(root: Path) -> list[tuple[Path, str]]:
    """收集项目目录内需要 Schema 校验的文件及其种类。"""
    found: list[tuple[Path, str]] = []
    for name in ("project.yaml", "project.yml"):
        p = root / name
        if p.is_file():
            found.append((p, "project"))
            break
    for sub, kind in _DIR_KINDS.items():
        d = root / sub
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() in CONFIG_SUFFIXES:
                found.append((f, kind))
    return found


def _json_pointer_of(error: object) -> str:
    """把 jsonschema 校验错误的实例路径转换为 JSON Pointer。"""
    parts = list(getattr(error, "absolute_path", []))  # type: ignore[arg-type]
    return _ptr(*parts) if parts else ""


def validate_project_dir(path: str | Path) -> ValidationResult:
    """校验一个项目目录：Schema -> 解析 -> 跨对象规则 -> 硬锁。

    流程分三段，避免级联噪音：
    1. 每个配置文件做 JSON Schema（draft 2020-12）校验；
    2. 全部文件 Schema 通过后整体解析为 :class:`ProjectBundle`；
    3. 解析干净后执行跨对象引用检查与受保护目标硬锁。
    """
    root = Path(path)
    result = ValidationResult(root=root)
    if not root.is_dir():
        result.issues.append(
            Issue(file=str(root), pointer="", rule="project_dir_missing", message=f"项目目录不存在：{root}")
        )
        return result

    # 阶段 1：Schema 校验
    schema_clean = True
    for file_path, kind in _discover(root):
        display = file_path.relative_to(root).as_posix()
        try:
            schema = load_schema(kind)
        except DomainValidationError as exc:
            result.issues.extend(exc.issues)
            schema_clean = False
            continue
        try:
            data = load_config_file(file_path)
        except DomainValidationError as exc:
            result.issues.extend(
                Issue(file=display, pointer=i.pointer, rule=i.rule, message=i.message, hint=i.hint) for i in exc.issues
            )
            schema_clean = False
            continue
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(data), key=lambda e: (list(e.absolute_path), e.message))
        for err in errors:
            result.issues.append(
                Issue(
                    file=display,
                    pointer=_json_pointer_of(err),
                    rule="schema_invalid",
                    message=f"{err.message}（{err.validator}）",
                    hint=f"参照 schemas/{kind}.schema.json 修正该字段",
                )
            )
        if errors:
            schema_clean = False

    if not schema_clean:
        return result

    # 阶段 2：整体解析
    try:
        bundle = load_project(root)
    except DomainValidationError as exc:
        result.issues.extend(exc.issues)
        return result
    result.bundle = bundle

    # 阶段 3：跨对象规则 + 硬锁
    result.issues.extend(check_asset_references(bundle))
    result.issues.extend(check_machines(bundle))
    result.issues.extend(check_protected_hard_locks(bundle))
    return result
