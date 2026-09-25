"""security_kit——安全与隐私套件（M3/E14：SEC-002/004/005/006/007 + TST-010）。

- path_guard：安全解包与路径边界（SEC-002 完整版，规则 ID 与 M3
  release_kit 的 transfer 预检约定一致）；
- integrity：资产哈希基线、篡改检测与发布包预检（SEC-004）；
- privacy：截图隐私遮罩与类型化导出强制（SEC-005）；
- retention：日志/截图保留策略、发布基线保护与清理审计（SEC-006）；
- sanitize：路径/用户名/窗口标题脱敏与诊断包（SEC-007）。

本包不依赖服务层；仅依赖 common（可选）、capture_api（Frame，privacy）、
trace_format（writer，sanitize）与 numpy/Pillow。
"""

from security_kit.errors import IntegrityError, PathGuardError, SecurityKitError
from security_kit.integrity import (
    AssetIntegrity,
    TamperReport,
    build_hashes,
    ensure_untouched,
    inspect_release_pack,
    verify_assets,
)
from security_kit.path_guard import (
    EXECUTABLE_EXTENSIONS,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    ExtractReport,
    PathIssue,
    SizeLimits,
    check_entry_path,
    precheck_zip,
    safe_extract_zip,
    safe_resolve,
)
from security_kit.privacy import (
    MaskedPixels,
    PrivacyMask,
    assume_masked,
    export_png,
)
from security_kit.retention import (
    CleanupReport,
    RetentionPolicy,
    StorageLayout,
    WallClock,
    enforce,
    protected_paths,
)
from security_kit.sanitize import (
    SECURITY_KIT_VERSION,
    DiagnosticBundleReport,
    SanitizeConfig,
    SanitizedTraceReport,
    diagnostic_bundle,
    sanitize_event_payload,
    sanitize_text,
    sanitize_trace,
)

__version__ = SECURITY_KIT_VERSION

__all__ = [
    # 异常
    "SecurityKitError",
    "PathGuardError",
    "IntegrityError",
    # SEC-002 path_guard
    "PathIssue",
    "SizeLimits",
    "ExtractReport",
    "check_entry_path",
    "safe_resolve",
    "safe_extract_zip",
    "precheck_zip",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "EXECUTABLE_EXTENSIONS",
    # SEC-004 integrity
    "AssetIntegrity",
    "TamperReport",
    "build_hashes",
    "verify_assets",
    "ensure_untouched",
    "inspect_release_pack",
    # SEC-005 privacy
    "PrivacyMask",
    "MaskedPixels",
    "assume_masked",
    "export_png",
    # SEC-006 retention
    "StorageLayout",
    "RetentionPolicy",
    "CleanupReport",
    "WallClock",
    "enforce",
    "protected_paths",
    # SEC-007 sanitize
    "SanitizeConfig",
    "sanitize_text",
    "sanitize_event_payload",
    "sanitize_trace",
    "SanitizedTraceReport",
    "diagnostic_bundle",
    "DiagnosticBundleReport",
]
