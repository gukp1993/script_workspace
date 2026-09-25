"""domain_model——领域模型、Schema 与校验器（DOM-001/002/003/005/009）。

职责：
- 定义核心领域对象与轻量不变式（:mod:`domain_model.models`）；
- YAML/JSON 字典 -> 领域对象的解析与项目聚合根（:mod:`domain_model.parsing`）；
- JSON Schema 加载（:mod:`domain_model.schemas`，schema 文件在仓库根 ``schemas/``）；
- 项目级校验规则与 CLI（:mod:`domain_model.validation` / ``python -m domain_model.validate``）；
- 能力清单与默认拒绝授权模型（:mod:`domain_model.capabilities`，DOM-005）。

安全边界：
- ``TargetProfile.real_input_allowed`` 是真实输入许可的唯一判据，受保护在线目标恒为 False；
- 受保护在线目标在项目校验层被硬锁：禁止 real_input、禁止无人值守调度；
- ``PerceptionSnapshot`` 只含感知事实，不含任何动作字段；
- ``InputIntent`` 不在本包定义——由 services/input_broker（INP-001，E08）实现，
  本包仅保留此占位说明，避免领域层与输入代理实现耦合。
"""

from domain_model.capabilities import (
    CAPABILITY_REGISTRY,
    authorize,
    authorize_input,
    is_registered,
)
from domain_model.errors import DomainModelError, DomainValidationError, Issue, join_pointer
from domain_model.models import (
    SCHEMA_VERSION,
    ActionDecl,
    AssetKind,
    CalibrationProfile,
    Detector,
    DetectorType,
    DisplayMode,
    FieldObservation,
    PerceptionSnapshot,
    PolicyMode,
    PolicyProfile,
    StateDef,
    StateMachineDef,
    TargetProfile,
    Transition,
    UnattendedSchedule,
    VisualAsset,
)
from domain_model.parsing import (
    ProjectBundle,
    load_config_file,
    load_project,
    parse_asset,
    parse_calibration,
    parse_detector,
    parse_machine,
    parse_perception_snapshot,
    parse_policy,
    parse_project,
    parse_target,
)
from domain_model.schemas import SCHEMA_ID_PREFIX, SCHEMA_KINDS, load_schema, schema_path
from domain_model.validation import (
    ValidationResult,
    validate_project_dir,
)

__version__ = "0.1.0"

__all__ = [
    # 常量与枚举
    "SCHEMA_VERSION",
    "SCHEMA_KINDS",
    "SCHEMA_ID_PREFIX",
    "DisplayMode",
    "DetectorType",
    "PolicyMode",
    "AssetKind",
    "UnattendedSchedule",
    # 领域对象
    "TargetProfile",
    "CalibrationProfile",
    "VisualAsset",
    "Detector",
    "FieldObservation",
    "PerceptionSnapshot",
    "Transition",
    "ActionDecl",
    "StateDef",
    "StateMachineDef",
    "PolicyProfile",
    "ProjectBundle",
    # 解析
    "load_project",
    "load_config_file",
    "parse_project",
    "parse_target",
    "parse_policy",
    "parse_calibration",
    "parse_detector",
    "parse_machine",
    "parse_asset",
    "parse_perception_snapshot",
    # Schema 与校验
    "load_schema",
    "schema_path",
    "validate_project_dir",
    "ValidationResult",
    # 能力模型（DOM-005）
    "CAPABILITY_REGISTRY",
    "is_registered",
    "authorize",
    "authorize_input",
    # 错误
    "Issue",
    "DomainModelError",
    "DomainValidationError",
    "join_pointer",
]
