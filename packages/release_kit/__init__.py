"""release_kit——版本包、发布、影响分析与回滚（E12，VER-001~010）。

模块地图：
- :mod:`release_kit.hashing`   内容哈希与不可变发布目录（VER-002，SEC-004）；
- :mod:`release_kit.manifest`  发布清单 ReleaseManifest（VER-001，发布单元=整个项目）；
- :mod:`release_kit.signing`   签名协议 + HMAC/空签名实现（VER-006）；
- :mod:`release_kit.publisher` 发布流水线、质量闸门挂点、发布枚举（VER-002/005）；
- :mod:`release_kit.snapshots` 草稿快照与对象级/字段级 diff（VER-003）；
- :mod:`release_kit.impact`    依赖图与变更影响分析（VER-004）；
- :mod:`release_kit.transfer`  导出/导入与安全预检（VER-007，SEC-002，AC-P0-11）；
- :mod:`release_kit.rollback`  整体回滚、升级备份与失败恢复、兼容检查
  （VER-008/009/010，AC-P0-12/14）。

安全边界：导入预检拒绝路径穿越/绝对路径/盘符/符号链接逃逸、超限包、
zip bomb 与可执行载荷；未知签名默认不启用 real_input；不实现任何
进程注入、内存读写或反检测能力。
"""

from release_kit.errors import CompatError, MigrationStepError, ReleaseError
from release_kit.hashing import (
    content_digest,
    content_hash,
    copy_frozen_files,
    freeze_directory,
    sync_frozen_files,
    verify_directory,
)
from release_kit.impact import ImpactGraph, ImpactReport
from release_kit.manifest import (
    MANIFEST_SCHEMA_VERSION,
    OBJECT_CATEGORIES,
    ObjectEntry,
    ReleaseManifest,
    RuntimeCompat,
    TestSummary,
    build_object_catalog,
    build_release_manifest,
    hash_domain_object,
)
from release_kit.publisher import (
    CheckOutcome,
    ReleaseCheck,
    ReleaseChecks,
    ReleasePublisher,
    ReleaseRecord,
)
from release_kit.rollback import (
    ReleaseRollback,
    RollbackResult,
    UpgradeResult,
    compat_check,
    compat_message,
    parse_semver,
)
from release_kit.signing import (
    HMACSigner,
    NullSigner,
    Signer,
    signature_envelope,
    verify_envelope,
    verify_signature,
)
from release_kit.snapshots import ObjectDiff, SnapshotData, SnapshotManager, SnapshotRef
from release_kit.transfer import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_RATIO,
    DEFAULT_MAX_TOTAL_BYTES,
    EXECUTABLE_SUFFIXES,
    ImportReport,
    Rejection,
    export_release,
    import_release,
)

__all__ = [
    # errors
    "CompatError",
    "MigrationStepError",
    "ReleaseError",
    # hashing
    "content_digest",
    "content_hash",
    "copy_frozen_files",
    "freeze_directory",
    "sync_frozen_files",
    "verify_directory",
    # manifest
    "MANIFEST_SCHEMA_VERSION",
    "OBJECT_CATEGORIES",
    "ObjectEntry",
    "ReleaseManifest",
    "RuntimeCompat",
    "TestSummary",
    "build_object_catalog",
    "build_release_manifest",
    "hash_domain_object",
    # signing
    "HMACSigner",
    "NullSigner",
    "Signer",
    "signature_envelope",
    "verify_envelope",
    "verify_signature",
    # publisher
    "CheckOutcome",
    "ReleaseCheck",
    "ReleaseChecks",
    "ReleasePublisher",
    "ReleaseRecord",
    # snapshots
    "ObjectDiff",
    "SnapshotData",
    "SnapshotManager",
    "SnapshotRef",
    # impact
    "ImpactGraph",
    "ImpactReport",
    # transfer
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_RATIO",
    "DEFAULT_MAX_TOTAL_BYTES",
    "EXECUTABLE_SUFFIXES",
    "ImportReport",
    "Rejection",
    "export_release",
    "import_release",
    # rollback
    "ReleaseRollback",
    "RollbackResult",
    "UpgradeResult",
    "compat_check",
    "compat_message",
    "parse_semver",
]
