"""YAML/JSON 字典 -> 领域对象的解析层（DOM-001 的文件侧入口）。

约定：
- 只使用 ``yaml.safe_load``（禁止 unsafe load，避免任意对象反序列化）；
- 解析函数把字段缺失/类型错误转换为带 JSON Pointer 与规则 ID 的 :class:`Issue`；
- ``load_project(path)`` 把整个项目目录装配为 :class:`ProjectBundle` 聚合根。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from domain_model.errors import DomainValidationError, Issue, join_pointer
from domain_model.models import (
    SCHEMA_VERSION,
    ActionDecl,
    CalibrationProfile,
    Detector,
    FieldObservation,
    PerceptionSnapshot,
    PolicyProfile,
    StateDef,
    StateMachineDef,
    TargetProfile,
    Transition,
    VisualAsset,
)

#: 支持的配置文件扩展名
CONFIG_SUFFIXES = frozenset({".yaml", ".yml", ".json"})


# ---------------------------------------------------------------------------
# 底层读取
# ---------------------------------------------------------------------------


def load_config_file(path: str | Path) -> dict[str, Any]:
    """读取单个 YAML/JSON 配置文件并要求顶层为映射。

    Raises:
        DomainValidationError: 文件不存在、YAML/JSON 语法错误或顶层不是映射。
    """
    p = Path(path)
    display = p.name
    if not p.is_file():
        raise DomainValidationError(
            [Issue(file=display, pointer="", rule="file_missing", message=f"配置文件不存在：{p}")]
        )
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise DomainValidationError(
            [Issue(file=display, pointer="", rule="file_unreadable", message=f"配置文件无法读取：{exc}")]
        ) from exc
    try:
        if p.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DomainValidationError(
            [Issue(file=display, pointer="", rule="yaml_parse_error", message=f"YAML 解析失败：{exc}")]
        ) from exc
    except json.JSONDecodeError as exc:
        raise DomainValidationError(
            [Issue(file=display, pointer="", rule="yaml_parse_error", message=f"JSON 解析失败：{exc}")]
        ) from exc
    if not isinstance(data, dict):
        raise DomainValidationError(
            [Issue(file=display, pointer="", rule="invalid_root", message="配置文件顶层必须是键值映射")]
        )
    return data


def _require(data: dict[str, Any], key: str, *, file: str) -> Any:
    """取出必填字段；缺失时抛出带指针的问题。"""
    if key not in data or data[key] is None:
        raise DomainValidationError(
            [Issue(file=file, pointer=f"/{key}", rule="missing_field", message=f"缺少必填字段 {key}")]
        )
    return data[key]


def _check_version(data: dict[str, Any], *, file: str) -> None:
    """校验 schema_version 字段存在且为 1。"""
    version = data.get("schema_version")
    if version is None:
        raise DomainValidationError(
            [Issue(file=file, pointer="/schema_version", rule="missing_field", message="缺少必填字段 schema_version")]
        )
    if version != SCHEMA_VERSION or isinstance(version, bool):
        raise DomainValidationError(
            [
                Issue(
                    file=file,
                    pointer="/schema_version",
                    rule="unsupported_schema_version",
                    message=f"schema_version 只支持 {SCHEMA_VERSION}，得到 {version!r}",
                    hint="把配置文件升级/降级到当前 Schema 版本",
                )
            ]
        )


def _build(builder: Callable[[], Any], *, file: str) -> Any:
    """执行构造函数；把模型层异常的 ``<data>`` 文件占位替换为真实文件名。"""
    try:
        return builder()
    except DomainValidationError as exc:
        retagged = [
            Issue(file=file, pointer=i.pointer, rule=i.rule, message=i.message, hint=i.hint) if i.file == "<data>" else i
            for i in exc.issues
        ]
        raise DomainValidationError(retagged) from None


def _reject_unknown(data: dict[str, Any], known: set[str], *, file: str) -> None:
    """拒绝未知的顶层字段（防止拼写错误静默生效）。"""
    for key in data:
        if key not in known:
            raise DomainValidationError(
                [Issue(file=file, pointer=f"/{key}", rule="unknown_field",
                       message=f"不支持的字段 {key}", hint="检查字段名拼写或 Schema 版本")]
            )


# ---------------------------------------------------------------------------
# 各对象解析
# ---------------------------------------------------------------------------

_TARGET_FIELDS = {"schema_version", "target_id", "executable", "title_regex", "window_class",
                  "protected_online", "allowed_display_modes", "notes"}
_POLICY_FIELDS = {"schema_version", "policy_id", "mode", "require_manual_start", "max_runtime_minutes",
                  "max_actions_per_minute", "max_total_actions", "on_focus_lost", "unattended_schedule"}
_CALIBRATION_FIELDS = {"schema_version", "calibration_id", "resolution", "dpi_percent", "ui_scale", "anchors"}
_DETECTOR_FIELDS = {"schema_version", "detector_id", "type", "roi", "threshold", "stable_frames",
                    "field_name", "template"}
_MACHINE_FIELDS = {"schema_version", "machine_id", "initial", "states"}
_ASSET_FIELDS = {"asset_id", "path", "sha256", "version", "kind"}


def parse_project(data: dict[str, Any], *, file: str = "project.yaml") -> dict[str, Any]:
    """解析 project.yaml 元数据（name/description/notes）。"""
    _check_version(data, file=file)
    issues: list[Issue] = []
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append(Issue(file=file, pointer="/name", rule="missing_field", message="缺少必填字段 name"))
    for opt_key in ("description", "notes"):
        value = data.get(opt_key)
        if value is not None and not isinstance(value, str):
            issues.append(Issue(file=file, pointer=f"/{opt_key}", rule="invalid_type", message=f"{opt_key} 必须是字符串"))
    for key in data:
        if key not in {"schema_version", "name", "description", "notes"}:
            issues.append(
                Issue(file=file, pointer=f"/{key}", rule="unknown_field",
                      message=f"project.yaml 不支持字段 {key}", hint="检查字段名拼写")
            )
    if issues:
        raise DomainValidationError(issues)
    return {"name": name, "description": data.get("description", ""), "notes": data.get("notes", "")}


def parse_target(data: dict[str, Any], *, file: str = "<target>") -> TargetProfile:
    """解析 targets/*.yaml -> :class:`TargetProfile`。"""
    _check_version(data, file=file)
    target = _build(
        lambda: TargetProfile(
            target_id=_require(data, "target_id", file=file),
            executable=_require(data, "executable", file=file),
            title_regex=_require(data, "title_regex", file=file),
            window_class=data.get("window_class"),
            protected_online=_require(data, "protected_online", file=file),
            allowed_display_modes=_require(data, "allowed_display_modes", file=file),
            notes=data.get("notes", ""),
            source_file=file,
        ),
        file=file,
    )
    _reject_unknown(data, _TARGET_FIELDS, file=file)
    return target


def parse_policy(data: dict[str, Any], *, file: str = "<policy>") -> PolicyProfile:
    """解析 policies/*.yaml -> :class:`PolicyProfile`。"""
    _check_version(data, file=file)
    kwargs: dict[str, Any] = {
        "policy_id": _require(data, "policy_id", file=file),
        "mode": _require(data, "mode", file=file),
        "require_manual_start": _require(data, "require_manual_start", file=file),
        "max_runtime_minutes": _require(data, "max_runtime_minutes", file=file),
        "max_actions_per_minute": _require(data, "max_actions_per_minute", file=file),
    }
    for opt in ("max_total_actions", "on_focus_lost", "unattended_schedule"):
        if opt in data:
            kwargs[opt] = data[opt]
    kwargs["source_file"] = file
    policy = _build(lambda: PolicyProfile(**kwargs), file=file)
    _reject_unknown(data, _POLICY_FIELDS, file=file)
    return policy


def parse_calibration(data: dict[str, Any], *, file: str = "<calibration>") -> CalibrationProfile:
    """解析 calibrations/* -> :class:`CalibrationProfile`。"""
    _check_version(data, file=file)
    raw_anchors = data.get("anchors", {})
    anchors: dict[str, tuple[float, float]] = {}
    anchor_issues: list[Issue] = []
    if isinstance(raw_anchors, dict):
        for key, xy in raw_anchors.items():
            if isinstance(xy, (list, tuple)) and len(xy) == 2 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in xy
            ):
                anchors[key] = (float(xy[0]), float(xy[1]))
            else:
                anchor_issues.append(
                    Issue(file=file, pointer=join_pointer("anchors", key), rule="invalid_type",
                          message=f"锚点 {key!r} 必须是 [x, y] 两个数字")
                )
    elif raw_anchors is not None:
        anchor_issues.append(Issue(file=file, pointer="/anchors", rule="invalid_type", message="anchors 必须是映射"))
    calibration = _build(
        lambda: CalibrationProfile(
            calibration_id=_require(data, "calibration_id", file=file),
            resolution=_require(data, "resolution", file=file),
            dpi_percent=_require(data, "dpi_percent", file=file),
            ui_scale=_require(data, "ui_scale", file=file),
            anchors=anchors,
            source_file=file,
        ),
        file=file,
    )
    if anchor_issues:
        raise DomainValidationError(anchor_issues)
    _reject_unknown(data, _CALIBRATION_FIELDS, file=file)
    return calibration


def parse_detector(data: dict[str, Any], *, file: str = "<detector>") -> Detector:
    """解析 detectors/*.yaml -> :class:`Detector`。"""
    _check_version(data, file=file)
    kwargs: dict[str, Any] = {
        "detector_id": _require(data, "detector_id", file=file),
        "type": _require(data, "type", file=file),
        "roi": _require(data, "roi", file=file),
        "threshold": _require(data, "threshold", file=file),
        "stable_frames": _require(data, "stable_frames", file=file),
        "field_name": _require(data, "field_name", file=file),
    }
    if "template" in data:
        kwargs["template"] = data["template"]
    kwargs["source_file"] = file
    detector = _build(lambda: Detector(**kwargs), file=file)
    _reject_unknown(data, _DETECTOR_FIELDS, file=file)
    return detector


def parse_machine(data: dict[str, Any], *, file: str = "<machine>") -> StateMachineDef:
    """解析 machines/*.yaml -> :class:`StateMachineDef`。"""
    _check_version(data, file=file)
    states_raw = _require(data, "states", file=file)
    if not isinstance(states_raw, dict) or not states_raw:
        raise DomainValidationError(
            [Issue(file=file, pointer="/states", rule="invalid_type", message="states 必须是 非空的状态映射")]
        )
    states: dict[str, StateDef] = {}
    for state_name, state_body in states_raw.items():
        if not isinstance(state_body, dict):
            raise DomainValidationError(
                [Issue(file=file, pointer=join_pointer("states", state_name), rule="invalid_type",
                       message=f"状态 {state_name!r} 的定义必须是映射")]
            )
        states[state_name] = _parse_state(state_body, file=file, base=join_pointer("states", state_name))
    machine = _build(
        lambda: StateMachineDef(
            machine_id=_require(data, "machine_id", file=file),
            initial=_require(data, "initial", file=file),
            states=states,
            source_file=file,
        ),
        file=file,
    )
    _reject_unknown(data, _MACHINE_FIELDS, file=file)
    return machine


def _parse_state(body: dict[str, Any], *, file: str, base: str) -> StateDef:
    """解析单个状态定义（迁移、动作占位、超时、终态标记）。"""
    transitions: list[Transition] = []
    for idx, tr in enumerate(body.get("transitions", []) or []):
        if not isinstance(tr, dict):
            raise DomainValidationError(
                [Issue(file=file, pointer=join_pointer(base, "transitions", idx), rule="invalid_type",
                       message="迁移必须是映射")]
            )
        transitions.append(
            _build(
                lambda tr=tr: Transition(
                    when=_require(tr, "when", file=file),
                    to=_require(tr, "to", file=file),
                    on_timeout_to=tr.get("on_timeout_to"),
                ),
                file=file,
            )
        )
    entry = [_parse_action(a, file=file, base=join_pointer(base, "entry", i))
             for i, a in enumerate(body.get("entry", []) or [])]
    exit_ = [_parse_action(a, file=file, base=join_pointer(base, "exit", i))
             for i, a in enumerate(body.get("exit", []) or [])]
    for key in body:
        if key not in {"transitions", "entry", "exit", "timeout_seconds", "terminal"}:
            raise DomainValidationError(
                [Issue(file=file, pointer=join_pointer(base, key), rule="unknown_field",
                       message=f"状态定义不支持字段 {key}",
                       hint="允许字段：transitions/entry/exit/timeout_seconds/terminal")]
            )
    return _build(
        lambda: StateDef(
            transitions=transitions,
            entry=entry,
            exit=exit_,
            timeout_seconds=body.get("timeout_seconds"),
            terminal=body.get("terminal", False),
        ),
        file=file,
    )


def _parse_action(body: object, *, file: str, base: str) -> ActionDecl:
    """解析 entry/exit 动作占位（仅结构：kind + 平铺标量参数，M0 不解释执行）。"""
    if not isinstance(body, dict):
        raise DomainValidationError(
            [Issue(file=file, pointer=base, rule="invalid_type", message="动作占位必须是包含 kind 的映射")]
        )
    params = {k: v for k, v in body.items() if k != "kind"}
    for k, v in params.items():
        if not isinstance(v, (str, int, float, bool, type(None))):
            raise DomainValidationError(
                [Issue(file=file, pointer=join_pointer(base, k), rule="invalid_type",
                       message="动作占位参数只允许标量值（M0 仅保存结构）")]
            )
    return _build(lambda: ActionDecl(kind=_require(body, "kind", file=file), params=params), file=file)


def parse_asset(data: dict[str, Any], *, file: str = "<asset>") -> VisualAsset:
    """解析资产清单条目 -> :class:`VisualAsset`。

    注意：schema_version 由清单文件（assets/assets.yaml）统一携带，
    单个条目不再重复要求。
    """
    asset = _build(
        lambda: VisualAsset(
            asset_id=_require(data, "asset_id", file=file),
            path=_require(data, "path", file=file),
            sha256=_require(data, "sha256", file=file),
            version=_require(data, "version", file=file),
            kind=_require(data, "kind", file=file),
            source_file=file,
        ),
        file=file,
    )
    _reject_unknown(data, _ASSET_FIELDS, file=file)
    return asset


def parse_perception_snapshot(data: dict[str, Any], *, file: str = "<snapshot>") -> PerceptionSnapshot:
    """解析感知快照（供测试夹具与回放使用）-> :class:`PerceptionSnapshot`。"""
    values_raw = data.get("values", {})
    if not isinstance(values_raw, dict):
        raise DomainValidationError(
            [Issue(file=file, pointer="/values", rule="invalid_type", message="values 必须是映射")]
        )
    values: dict[str, FieldObservation] = {}
    for name, body in values_raw.items():
        if not isinstance(body, dict):
            raise DomainValidationError(
                [Issue(file=file, pointer=join_pointer("values", name), rule="invalid_type",
                       message=f"字段 {name!r} 的观测必须是映射")]
            )
        values[name] = FieldObservation(
            name=name,
            present=bool(body.get("present", False)),
            confidence=float(body.get("confidence", 0.0)),
            value=body.get("value"),
        )
    return _build(
        lambda: PerceptionSnapshot(
            frame_seq=_require(data, "frame_seq", file=file),
            ts_monotonic=_require(data, "ts_monotonic", file=file),
            values=values,
        ),
        file=file,
    )


# ---------------------------------------------------------------------------
# 项目聚合根
# ---------------------------------------------------------------------------


@dataclass
class ProjectBundle:
    """项目聚合根：project.yaml 元数据 + 各类对象清单。

    Attributes:
        root:         项目根目录。
        project:      project.yaml 元数据（name/description/notes）。
        targets:      target_id -> TargetProfile。
        policies:     policy_id -> PolicyProfile。
        calibrations: calibration_id -> CalibrationProfile。
        detectors:    detector_id -> Detector。
        machines:     machine_id -> StateMachineDef。
        assets:       asset_id -> VisualAsset（来自 assets/assets.yaml 清单）。
    """

    root: Path
    project: dict[str, Any]
    targets: dict[str, TargetProfile] = field(default_factory=dict)
    policies: dict[str, PolicyProfile] = field(default_factory=dict)
    calibrations: dict[str, CalibrationProfile] = field(default_factory=dict)
    detectors: dict[str, Detector] = field(default_factory=dict)
    machines: dict[str, StateMachineDef] = field(default_factory=dict)
    assets: dict[str, VisualAsset] = field(default_factory=dict)

    def object_count(self) -> int:
        """已解析的领域对象总数（用于 CLI 的 ``VALID: <n> objects checked``）。"""
        return (
            1
            + len(self.targets)
            + len(self.policies)
            + len(self.calibrations)
            + len(self.detectors)
            + len(self.machines)
            + len(self.assets)
        )

    @property
    def has_protected_target(self) -> bool:
        """是否存在受保护在线目标。"""
        return any(t.protected_online for t in self.targets.values())

    @property
    def perception_field_names(self) -> set[str]:
        """本项目全部检测器输出的语义字段名集合。"""
        return {d.field_name for d in self.detectors.values()}


def _parse_into(
    files: list[tuple[Path, dict[str, Any]]],
    root: Path,
    parser: Callable[..., Any],
    id_attr: str,
    bucket: dict[str, Any],
    issues: list[Issue],
) -> None:
    """把一个目录下的配置文件批量解析进 bucket，收集问题而不中断。"""
    for f, data in files:
        display = f.relative_to(root).as_posix()
        try:
            obj = parser(data, file=display)
            obj_id = getattr(obj, id_attr, None)
            if obj_id is None:
                issues.append(Issue(file=display, pointer="", rule="invalid_root",
                                    message=f"{display} 缺少标识字段 {id_attr}"))
                continue
            if obj_id in bucket:
                issues.append(Issue(file=display, pointer=f"/{id_attr}", rule="duplicate_id",
                                    message=f"{id_attr} {obj_id!r} 重复定义", hint="ID 在项目内必须唯一"))
            bucket[obj_id] = obj
        except DomainValidationError as exc:
            issues.extend(exc.issues)


def _load_dir(root: Path, sub: str, issues: list[Issue]) -> list[tuple[Path, dict[str, Any]]]:
    """读取目录下全部受支持的配置文件；读取失败记录问题并跳过。"""
    out: list[tuple[Path, dict[str, Any]]] = []
    d = root / sub
    if not d.is_dir():
        return out
    for f in sorted(d.iterdir()):
        if f.is_file() and f.suffix.lower() in CONFIG_SUFFIXES:
            try:
                out.append((f, load_config_file(f)))
            except DomainValidationError as exc:
                issues.extend(Issue(file=f.relative_to(root).as_posix(), pointer=i.pointer, rule=i.rule,
                                    message=i.message, hint=i.hint) for i in exc.issues)
    return out


def load_project(path: str | Path) -> ProjectBundle:
    """加载并解析整个项目目录为 :class:`ProjectBundle`。

    只做**结构与对象不变式**校验（缺失字段、非法枚举、ROI 越界等）；
    跨对象检查（资产引用、状态可达性、受保护目标硬锁等）见
    :mod:`domain_model.validation`。

    Raises:
        DomainValidationError: 任一文件缺失/无法解析/对象不变式被违反。
    """
    root = Path(path)
    issues: list[Issue] = []

    project_file = root / "project.yaml"
    if not project_file.is_file():
        project_file = root / "project.yml"
    if not project_file.is_file():
        raise DomainValidationError(
            [Issue(file=str(root / "project.yaml"), pointer="", rule="project_file_missing",
                   message=f"项目目录缺少 project.yaml：{root}")]
        )
    try:
        project_data = load_config_file(project_file)
        meta = parse_project(project_data, file=project_file.name)
    except DomainValidationError as exc:
        raise DomainValidationError(
            [Issue(file=str(project_file), pointer=i.pointer, rule=i.rule, message=i.message, hint=i.hint)
             for i in exc.issues]
        ) from None

    bundle = ProjectBundle(root=root, project=meta)
    _parse_into(_load_dir(root, "targets", issues), root, parse_target, "target_id", bundle.targets, issues)
    _parse_into(_load_dir(root, "policies", issues), root, parse_policy, "policy_id", bundle.policies, issues)
    _parse_into(_load_dir(root, "calibrations", issues), root, parse_calibration, "calibration_id",
                bundle.calibrations, issues)
    _parse_into(_load_dir(root, "detectors", issues), root, parse_detector, "detector_id", bundle.detectors, issues)
    _parse_into(_load_dir(root, "machines", issues), root, parse_machine, "machine_id", bundle.machines, issues)

    # 资产清单：assets/assets.yaml（schema_version + assets 条目数组）
    manifest: Path | None = None
    for name in ("assets.yaml", "assets.yml"):
        candidate = root / "assets" / name
        if candidate.is_file():
            manifest = candidate
            break
    if manifest is not None:
        manifest_display = str(manifest.relative_to(root))
        try:
            manifest_data = load_config_file(manifest)
            _check_version(manifest_data, file=manifest_display)
            entries = manifest_data.get("assets")
            if not isinstance(entries, list):
                issues.append(Issue(file=manifest_display, pointer="/assets", rule="invalid_type",
                                    message="assets 必须是资产条目数组"))
            else:
                for idx, entry in enumerate(entries):
                    if not isinstance(entry, dict):
                        issues.append(Issue(file=manifest_display, pointer=join_pointer("assets", idx),
                                            rule="invalid_type", message="资产条目必须是映射"))
                        continue
                    try:
                        asset = parse_asset(entry, file=manifest_display)
                        if asset.asset_id in bundle.assets:
                            issues.append(Issue(file=manifest_display, pointer=join_pointer("assets", idx, "asset_id"),
                                                rule="duplicate_id", message=f"asset_id {asset.asset_id!r} 重复定义"))
                        bundle.assets[asset.asset_id] = asset
                    except DomainValidationError as exc:
                        issues.extend(exc.issues)
        except DomainValidationError as exc:
            issues.extend(exc.issues)

    if issues:
        raise DomainValidationError(issues)
    return bundle
