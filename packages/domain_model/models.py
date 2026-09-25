"""核心领域对象（DOM-001）。

本模块只定义**数据与轻量不变式**，不做文件 IO、不依赖 YAML：
- 每个对象用 dataclass 表示，字段语义见架构文档第 4 章；
- 不变式（枚举、数值范围、ROI 归一化约束等）在 ``__post_init__`` 中校验，
  违反时抛出 :class:`DomainValidationError`（带 JSON Pointer 规则 ID）；
- 从 YAML/JSON 字典构造对象的逻辑在 :mod:`domain_model.parsing`。

安全约定：
- ``TargetProfile.real_input_allowed`` 是唯一"是否允许真实输入"的派生属性，
  受保护在线目标（protected_online=True）恒为 False，任何调用方不得绕过；
- ``PerceptionSnapshot`` 只承载"某一时刻的可观察事实"，**不包含任何动作字段**；
- ``InputIntent`` 不在本包定义——由 services/input_broker（INP-001，E08）实现，
  本包仅保留此占位说明，避免领域层与输入代理实现耦合。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from domain_model.errors import DomainValidationError, Issue, join_pointer

# ---------------------------------------------------------------------------
# 公共常量与枚举
# ---------------------------------------------------------------------------

#: 当前领域模型/Schema 的版本号（Schema 中为 const 1）
SCHEMA_VERSION = 1

#: 感知字段观测值的统一子属性（条件表达式 ``when`` 中只允许引用这些后缀）
OBSERVATION_ATTRIBUTES: frozenset[str] = frozenset(
    {"present", "value", "confidence", "changed"}
)


class DisplayMode(str, Enum):
    """允许的窗口显示模式。"""

    WINDOWED = "windowed"
    BORDERLESS = "borderless"
    FULLSCREEN = "fullscreen"


class DetectorType(str, Enum):
    """检测器算法类型（VIS-001 的 M0 子集）。"""

    TEMPLATE_MATCH = "template_match"
    COLOR_BAR_RATIO = "color_bar_ratio"
    COLOR_REGION = "color_region"
    CHANGE_STABILITY = "change_stability"
    OCR_ROI = "ocr_roi"


class PolicyMode(str, Enum):
    """运行模式，按安全等级从低到高排列。"""

    OBSERVE = "observe"
    SHADOW = "shadow"
    DRY_RUN = "dry_run"
    REAL_INPUT = "real_input"


class AssetKind(str, Enum):
    """视觉资产类型。"""

    TEMPLATE = "template"
    MASK = "mask"


class UnattendedSchedule(str, Enum):
    """无人值守调度开关；受保护在线目标必须为 DISABLED。"""

    ENABLED = "enabled"
    DISABLED = "disabled"


def _fail(file: str, pointer: str, rule: str, message: str, hint: str = "") -> None:
    """抛出带单条 Issue 的校验异常（内部便捷函数）。"""
    raise DomainValidationError([Issue(file=file, pointer=pointer, rule=rule, message=message, hint=hint)])


# ---------------------------------------------------------------------------
# 目标档案
# ---------------------------------------------------------------------------


@dataclass
class TargetProfile:
    """目标程序档案：窗口匹配规则与安全边界。

    Attributes:
        target_id:            项目内唯一 ID（kebab-case）。
        executable:           目标可执行文件名（不含路径，如 ``ArenaLab.exe``）。
        title_regex:          窗口标题匹配正则（必须可编译）。
        window_class:         可选的 Win32 窗口类名。
        protected_online:     是否为受保护在线目标；为 True 时禁止真实输入与无人值守。
        allowed_display_modes: 允许运行的显示模式白名单（模糊匹配不得自动放行，见 TGT-002）。
        notes:                备注字段（自由文本）。
    """

    target_id: str
    executable: str
    title_regex: str
    window_class: str | None = None
    protected_online: bool = False
    allowed_display_modes: list[str] = field(default_factory=list)
    notes: str = ""
    #: 来源文件（相对项目根，用于可定位错误；纯内存构造时为 <data>）
    source_file: str = "<data>"

    def __post_init__(self) -> None:
        _validate_target(self, file=self.source_file)

    @property
    def real_input_allowed(self) -> bool:
        """是否允许真实输入；受保护在线目标恒为 False（安全不变量）。"""
        return not self.protected_online


def _validate_target(t: TargetProfile, *, file: str) -> None:
    """TargetProfile 不变式校验（构造与解析共用）。"""
    if not isinstance(t.target_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", t.target_id or ""):
        _fail(file, "/target_id", "invalid_id", "target_id 必须是以小写字母开头的 kebab-case 字符串")
    if not isinstance(t.executable, str) or not t.executable.strip():
        _fail(file, "/executable", "missing_field", "executable 不能为空", "填写目标主程序文件名，如 ArenaLab.exe")
    if "/" in t.executable or "\\" in t.executable:
        _fail(file, "/executable", "invalid_executable", "executable 只能是文件名，不能包含路径分隔符")
    if not isinstance(t.title_regex, str) or not t.title_regex:
        _fail(file, "/title_regex", "missing_field", "title_regex 不能为空")
    try:
        re.compile(t.title_regex)
    except re.error as exc:
        _fail(file, "/title_regex", "invalid_regex", f"title_regex 无法编译：{exc}", "修正正则表达式语法")
    if not isinstance(t.protected_online, bool):
        _fail(file, "/protected_online", "invalid_type", "protected_online 必须是布尔值")
    modes = t.allowed_display_modes
    if not isinstance(modes, list) or not modes:
        _fail(file, "/allowed_display_modes", "missing_field", "allowed_display_modes 必须是非空数组")
    else:
        valid = {m.value for m in DisplayMode}
        for idx, mode in enumerate(modes):
            if mode not in valid:
                _fail(
                    file,
                    join_pointer("allowed_display_modes", idx),
                    "invalid_enum",
                    f"未知显示模式 {mode!r}；允许值：{sorted(valid)}",
                )
    if not isinstance(t.notes, str):
        _fail(file, "/notes", "invalid_type", "notes 必须是字符串")


# ---------------------------------------------------------------------------
# 标定档案
# ---------------------------------------------------------------------------


@dataclass
class CalibrationProfile:
    """某一分辨率/DPI/UI 缩放组合下的标定。

    Attributes:
        calibration_id: 项目内唯一 ID，惯例形如 ``1920x1080-100``。
        resolution:     分辨率字符串 ``<宽>x<高>``。
        dpi_percent:    Windows DPI 缩放百分比（如 100/125/150）。
        ui_scale:       目标程序 UI 缩放系数。
        anchors:        归一化锚点（0~1），键名 -> (x, y)。
    """

    calibration_id: str
    resolution: str
    dpi_percent: int
    ui_scale: float
    anchors: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: 来源文件（相对项目根，用于可定位错误；纯内存构造时为 <data>）
    source_file: str = "<data>"

    def __post_init__(self) -> None:
        _validate_calibration(self, file=self.source_file)

    @property
    def width_px(self) -> int:
        """分辨率宽度（像素）。"""
        return int(self.resolution.split("x")[0])

    @property
    def height_px(self) -> int:
        """分辨率高度（像素）。"""
        return int(self.resolution.split("x")[1])


def _validate_calibration(c: CalibrationProfile, *, file: str) -> None:
    """CalibrationProfile 不变式校验。"""
    if not isinstance(c.calibration_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", c.calibration_id or ""):
        _fail(file, "/calibration_id", "invalid_id", "calibration_id 只能包含字母、数字、点、下划线与连字符")
    if not isinstance(c.resolution, str) or not re.fullmatch(r"\d{2,5}x\d{2,5}", c.resolution or ""):
        _fail(file, "/resolution", "invalid_resolution", 'resolution 必须形如 "1920x1080"')
    if not isinstance(c.dpi_percent, int) or isinstance(c.dpi_percent, bool) or not 50 <= c.dpi_percent <= 500:
        _fail(file, "/dpi_percent", "out_of_range", "dpi_percent 必须是 50~500 的整数")
    if not isinstance(c.ui_scale, (int, float)) or isinstance(c.ui_scale, bool) or not 0.25 <= float(c.ui_scale) <= 4.0:
        _fail(file, "/ui_scale", "out_of_range", "ui_scale 必须在 0.25~4.0 之间")
    if not isinstance(c.anchors, dict):
        _fail(file, "/anchors", "invalid_type", "anchors 必须是 键名 -> [x, y] 的映射")
    for name, xy in c.anchors.items():
        if not name:
            _fail(file, "/anchors", "invalid_id", "锚点键名不能为空字符串")
        if (
            not isinstance(xy, (list, tuple))
            or len(xy) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in xy)
        ):
            _fail(file, join_pointer("anchors", name), "invalid_type", f"锚点 {name!r} 必须是 [x, y] 两个数字")
            continue
        if not all(0.0 <= float(v) <= 1.0 for v in xy):
            _fail(file, join_pointer("anchors", name), "anchor_out_of_range", f"锚点 {name!r} 必须归一化到 0~1")


# ---------------------------------------------------------------------------
# 视觉资产
# ---------------------------------------------------------------------------


@dataclass
class VisualAsset:
    """视觉资产（模板图/掩码）清单条目。

    Attributes:
        asset_id: 项目内唯一 ID。
        path:     相对项目根的路径（``/`` 或 ``\\`` 分隔，不得越出项目根）。
        sha256:   资产内容 SHA-256（64 位十六进制）。
        version:  资产版本号（>=1，内容变更时递增）。
        kind:     资产类型：template 或 mask。
    """

    asset_id: str
    path: str
    sha256: str
    version: int
    kind: AssetKind | str
    #: 来源文件（相对项目根，用于可定位错误；纯内存构造时为 <data>）
    source_file: str = "<data>"

    def __post_init__(self) -> None:
        _validate_asset(self, file=self.source_file)

    @property
    def kind_value(self) -> AssetKind:
        """归一化后的资产类型枚举。"""
        return self.kind if isinstance(self.kind, AssetKind) else AssetKind(self.kind)


def _validate_asset(a: VisualAsset, *, file: str) -> None:
    """VisualAsset 不变式校验。"""
    if not isinstance(a.asset_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", a.asset_id or ""):
        _fail(file, "/asset_id", "invalid_id", "asset_id 必须是以小写字母开头的 kebab-case 字符串")
    if not isinstance(a.path, str) or not a.path.strip():
        _fail(file, "/path", "missing_field", "path 不能为空")
    else:
        norm = a.path.replace("\\", "/")
        if norm.startswith("/") or ".." in norm.split("/") or ":" in norm:
            _fail(file, "/path", "path_escape", "path 必须是项目内相对路径，不得越出项目根目录")
    if not isinstance(a.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", a.sha256 or ""):
        _fail(file, "/sha256", "invalid_hash", "sha256 必须是 64 位小写十六进制")
    if not isinstance(a.version, int) or isinstance(a.version, bool) or a.version < 1:
        _fail(file, "/version", "out_of_range", "version 必须是 >=1 的整数")
    try:
        a.kind_value
    except ValueError:
        _fail(file, "/kind", "invalid_enum", "kind 只允许 template 或 mask")


# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------

#: 归一化 ROI 类型：[x, y, w, h]，x+w<=1、y+h<=1
Roi = tuple[float, float, float, float]


@dataclass
class Detector:
    """视觉检测器定义：算法类型、ROI、阈值与输出语义。

    Attributes:
        detector_id:   项目内唯一 ID。
        type:          算法类型（见 :class:`DetectorType`）。
        roi:           归一化 ROI ``[x, y, w, h]``，全部分量在 0~1 内，x+w<=1、y+h<=1。
        threshold:     判定阈值，0~1。
        stable_frames: 连续稳定帧数要求，>=1（去抖动）。
        field_name:    输出语义字段名（PerceptionSnapshot.values 的键）。
        template:      引用的模板资产相对路径（template_match 必填，其余类型不允许）。
    """

    detector_id: str
    type: DetectorType | str
    roi: Roi | list[float]
    threshold: float
    stable_frames: int
    field_name: str
    template: str | None = None
    #: 来源文件（相对项目根，用于可定位错误；纯内存构造时为 <data>）
    source_file: str = "<data>"

    def __post_init__(self) -> None:
        _validate_detector(self, file=self.source_file)

    @property
    def type_value(self) -> DetectorType:
        """归一化后的检测器类型枚举。"""
        return self.type if isinstance(self.type, DetectorType) else DetectorType(self.type)


def _validate_detector(d: Detector, *, file: str) -> None:
    """Detector 不变式校验（枚举、ROI、阈值、稳定帧、模板引用）。"""
    if not isinstance(d.detector_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", d.detector_id or ""):
        _fail(file, "/detector_id", "invalid_id", "detector_id 必须是以小写字母开头的 kebab-case 字符串")
    try:
        d.type_value
    except ValueError:
        valid = [m.value for m in DetectorType]
        _fail(file, "/type", "invalid_enum", f"未知检测器类型 {d.type!r}；允许值：{valid}")
        return

    roi = d.roi
    if not isinstance(roi, (list, tuple)) or len(roi) != 4:
        _fail(file, "/roi", "invalid_roi", "roi 必须是 [x, y, w, h] 四个数字")
        return
    labels = ("x", "y", "w", "h")
    for idx, value in enumerate(roi):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            _fail(file, join_pointer("roi", idx), "invalid_type", f"roi[{idx}]（{labels[idx]}）必须是数字")
            return
        if value < 0:
            _fail(file, join_pointer("roi", idx), "roi_negative", f"roi[{idx}]（{labels[idx]}）不能为负数，得到 {value}")
            return
    x, y, w, h = (float(v) for v in roi)
    if w == 0 or h == 0:
        _fail(file, "/roi", "roi_out_of_bounds", "roi 的宽高必须大于 0")
        return
    if x + w > 1.0:
        _fail(file, "/roi", "roi_out_of_bounds", f"roi 越界：x+w={x + w:.4f} > 1", "缩小宽度或左移 x")
    if y + h > 1.0:
        _fail(file, "/roi", "roi_out_of_bounds", f"roi 越界：y+h={y + h:.4f} > 1", "缩小高度或上移 y")

    if not isinstance(d.threshold, (int, float)) or isinstance(d.threshold, bool) or not 0.0 <= float(d.threshold) <= 1.0:
        _fail(file, "/threshold", "threshold_out_of_range", "threshold 必须在 0~1 之间")
    if not isinstance(d.stable_frames, int) or isinstance(d.stable_frames, bool) or d.stable_frames < 1:
        _fail(file, "/stable_frames", "stable_frames_invalid", "stable_frames 必须是 >=1 的整数")
    if not isinstance(d.field_name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", d.field_name or ""):
        _fail(file, "/field_name", "invalid_field_name", "field_name 必须是以小写字母开头的 snake_case 标识符")

    if d.template is not None:
        if not isinstance(d.template, str) or not d.template.strip():
            _fail(file, "/template", "missing_field", "template 不能为空字符串")
        else:
            norm = d.template.replace("\\", "/")
            if norm.startswith("/") or ".." in norm.split("/") or ":" in norm:
                _fail(file, "/template", "path_escape", "template 必须是项目内相对路径")
    if d.type_value is DetectorType.TEMPLATE_MATCH and d.template is None:
        _fail(file, "/template", "template_required", "template_match 类型必须引用 template 资产")
    if d.type_value is not DetectorType.TEMPLATE_MATCH and d.template is not None:
        _fail(file, "/template", "template_not_allowed", "只有 template_match 类型允许引用 template 资产")


# ---------------------------------------------------------------------------
# 感知快照（只读事实，不含任何动作字段）
# ---------------------------------------------------------------------------


@dataclass
class FieldObservation:
    """单个语义字段在某一帧的观测结果。

    Attributes:
        name:       语义字段名（与 Detector.field_name 对应）。
        present:    该帧是否成功观测到此字段。
        confidence: 置信度 0~1。
        value:      观测值（类型由检测器输出契约决定，M0 不做进一步约束）。
    """

    name: str
    present: bool = False
    confidence: float = 0.0
    value: object = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            _fail("<data>", "/name", "invalid_field_name", "FieldObservation.name 不能为空")
        if not isinstance(self.present, bool):
            _fail("<data>", "/present", "invalid_type", "present 必须是布尔值")
        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool) or not 0.0 <= float(self.confidence) <= 1.0:
            _fail("<data>", "/confidence", "confidence_out_of_range", "confidence 必须在 0~1 之间")


@dataclass
class PerceptionSnapshot:
    """某一时刻所有可观察事实的快照。

    安全约定：**只包含感知结果，不包含任何动作字段**——动作意图
    （InputIntent）由 services/input_broker 定义，二者不得混用。
    """

    frame_seq: int
    ts_monotonic: float
    values: dict[str, FieldObservation] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.frame_seq, int) or isinstance(self.frame_seq, bool) or self.frame_seq < 0:
            _fail("<data>", "/frame_seq", "out_of_range", "frame_seq 必须是 >=0 的整数")
        if not isinstance(self.ts_monotonic, (int, float)) or isinstance(self.ts_monotonic, bool):
            _fail("<data>", "/ts_monotonic", "invalid_type", "ts_monotonic 必须是数字（单调时钟秒数）")
        if not isinstance(self.values, dict):
            _fail("<data>", "/values", "invalid_type", "values 必须是 字段名 -> FieldObservation 的映射")


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


@dataclass
class Transition:
    """状态迁移。

    Attributes:
        when:          条件表达式字符串。M0 只存储不解析（FSM-001 在 M2 实现白名单语法），
                       静态检查仅提取其中引用的感知字段名。
        to:            迁移目标状态名。
        on_timeout_to: 可选的超时迁移目标状态名（配合所属状态的 timeout_seconds 使用）。
    """

    when: str
    to: str
    on_timeout_to: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.when, str) or not self.when.strip():
            _fail("<data>", "/when", "missing_field", "when 条件表达式不能为空")
        if not isinstance(self.to, str) or not self.to:
            _fail("<data>", "/to", "missing_field", "迁移目标状态 to 不能为空")
        if self.on_timeout_to is not None and (not isinstance(self.on_timeout_to, str) or not self.on_timeout_to):
            _fail("<data>", "/on_timeout_to", "missing_field", "on_timeout_to 不能为空字符串")


@dataclass
class ActionDecl:
    """entry/exit 动作占位（仅结构，M0 不解释执行；FSM-002 在 M2 编译）。"""

    kind: str
    params: dict[str, str | int | float | bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            _fail("<data>", "/kind", "missing_field", "动作占位必须包含 kind")


@dataclass
class StateDef:
    """状态定义。

    Attributes:
        transitions:     出边列表。
        entry:           进入动作占位（仅结构）。
        exit:            退出动作占位（仅结构）。
        timeout_seconds: 可选超时秒数（>0）；非终态的退出手段之一。
        terminal:        是否终态；终态不要求出边。
    """

    transitions: list[Transition] = field(default_factory=list)
    entry: list[ActionDecl] = field(default_factory=list)
    exit: list[ActionDecl] = field(default_factory=list)
    timeout_seconds: float | None = None
    terminal: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            _fail("<data>", "/timeout_seconds", "out_of_range", "timeout_seconds 必须是 >0 的数字")
        if not isinstance(self.terminal, bool):
            _fail("<data>", "/terminal", "invalid_type", "terminal 必须是布尔值")
        if not isinstance(self.transitions, list) or not all(isinstance(t, Transition) for t in self.transitions):
            _fail("<data>", "/transitions", "invalid_type", "transitions 必须是 Transition 列表")


@dataclass
class StateMachineDef:
    """状态机定义：初始状态 + 状态表。

    Attributes:
        machine_id: 项目内唯一 ID。
        initial:    初始状态名（必须出现在 states 中）。
        states:     状态名 -> 状态定义。
    """

    machine_id: str
    initial: str
    states: dict[str, StateDef] = field(default_factory=dict)
    #: 来源文件（相对项目根，用于可定位错误；纯内存构造时为 <data>）
    source_file: str = "<data>"

    def __post_init__(self) -> None:
        if not isinstance(self.machine_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", self.machine_id or ""):
            _fail(self.source_file, "/machine_id", "invalid_id", "machine_id 必须是以小写字母开头的 kebab-case 字符串")
        if not isinstance(self.initial, str) or not self.initial:
            _fail(self.source_file, "/initial", "missing_field", "initial 不能为空")
        if not isinstance(self.states, dict) or not self.states:
            _fail(self.source_file, "/states", "missing_field", "states 不能为空")
            return
        if self.initial not in self.states:
            _fail(
                self.source_file,
                "/initial",
                "initial_state_missing",
                f"初始状态 {self.initial!r} 未在 states 中定义",
                "把 initial 改为已定义的状态名，或补充该状态",
            )


# ---------------------------------------------------------------------------
# 策略档案
# ---------------------------------------------------------------------------


@dataclass
class PolicyProfile:
    """运行策略：模式、人工闸门与各种预算上限。

    Attributes:
        policy_id:            项目内唯一 ID。
        mode:                 运行模式（observe/shadow/dry_run/real_input）。
        require_manual_start: 是否必须人工启动。
        max_runtime_minutes:  单次运行时长上限（分钟，>0）。
        max_actions_per_minute: 每分钟动作数上限（>0）。
        max_total_actions:    单次运行总动作数上限（可选；None 表示沿用运行时默认）。
        on_focus_lost:        失焦行为；M0 只允许 stop（立即停止）。
        unattended_schedule:  无人值守调度开关；受保护在线目标必须 disabled。
    """

    policy_id: str
    mode: PolicyMode | str
    require_manual_start: bool
    max_runtime_minutes: int
    max_actions_per_minute: int
    max_total_actions: int | None = None
    on_focus_lost: str = "stop"
    unattended_schedule: UnattendedSchedule | str = UnattendedSchedule.DISABLED
    #: 来源文件（相对项目根，用于可定位错误；纯内存构造时为 <data>）
    source_file: str = "<data>"

    def __post_init__(self) -> None:
        _validate_policy(self, file=self.source_file)

    @property
    def mode_value(self) -> PolicyMode:
        """归一化后的运行模式枚举。"""
        return self.mode if isinstance(self.mode, PolicyMode) else PolicyMode(self.mode)

    @property
    def unattended_value(self) -> UnattendedSchedule:
        """归一化后的无人值守调度枚举。"""
        return (
            self.unattended_schedule
            if isinstance(self.unattended_schedule, UnattendedSchedule)
            else UnattendedSchedule(self.unattended_schedule)
        )


def _validate_policy(p: PolicyProfile, *, file: str) -> None:
    """PolicyProfile 不变式校验。"""
    if not isinstance(p.policy_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", p.policy_id or ""):
        _fail(file, "/policy_id", "invalid_id", "policy_id 必须是以小写字母开头的 kebab-case 字符串")
    try:
        p.mode_value
    except ValueError:
        _fail(file, "/mode", "invalid_enum", f"未知运行模式 {p.mode!r}；允许值：{[m.value for m in PolicyMode]}")
    if not isinstance(p.require_manual_start, bool):
        _fail(file, "/require_manual_start", "invalid_type", "require_manual_start 必须是布尔值")
    if not isinstance(p.max_runtime_minutes, int) or isinstance(p.max_runtime_minutes, bool) or p.max_runtime_minutes < 1:
        _fail(file, "/max_runtime_minutes", "out_of_range", "max_runtime_minutes 必须是 >=1 的整数")
    if not isinstance(p.max_actions_per_minute, int) or isinstance(p.max_actions_per_minute, bool) or p.max_actions_per_minute < 1:
        _fail(file, "/max_actions_per_minute", "out_of_range", "max_actions_per_minute 必须是 >=1 的整数")
    if p.max_total_actions is not None and (
        not isinstance(p.max_total_actions, int) or isinstance(p.max_total_actions, bool) or p.max_total_actions < 1
    ):
        _fail(file, "/max_total_actions", "out_of_range", "max_total_actions 必须是 >=1 的整数或 null")
    if p.on_focus_lost != "stop":
        _fail(file, "/on_focus_lost", "invalid_enum", "on_focus_lost 目前只允许 stop（失焦立即停止）")
    if p.unattended_schedule not in ("enabled", "disabled", UnattendedSchedule.ENABLED, UnattendedSchedule.DISABLED):
        _fail(file, "/unattended_schedule", "invalid_enum", "unattended_schedule 只允许 enabled 或 disabled")
