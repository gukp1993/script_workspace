"""扩展 Runner 服务（PLG-001/002 原型，M4）。

- :mod:`contract`：能力契约、清单模型与加载校验（默认拒绝）；
- :mod:`runner`：子进程隔离执行（超时 kill / 输出限额 / 崩溃隔离）；
- :mod:`child`：子进程入口（能力门面 + 结果信封回传）；
- :mod:`host`：注册/列出/调用/撤销 + 审计事件。

架构保证：扩展进程不持有 InputBroker 句柄——父子边界只传 JSON 数据；
扩展能力白名单刻意不含任何 ``input.*``。
"""

from extension_runner.contract import (
    EXTENSION_CAPABILITY_REGISTRY,
    MANIFEST_SCHEMA_VERSION,
    ExtensionManifest,
    ResourceLimits,
    compute_code_hash,
    load_manifest,
)
from extension_runner.errors import (
    CapabilityDenied,
    ExtensionError,
    ExtensionRunError,
    ManifestError,
)
from extension_runner.host import ExtensionHost
from extension_runner.runner import (
    ExtensionRunResult,
    ExtensionRunner,
    RunnerConfig,
    run_extension,
)

__all__ = [
    "EXTENSION_CAPABILITY_REGISTRY",
    "MANIFEST_SCHEMA_VERSION",
    "CapabilityDenied",
    "ExtensionError",
    "ExtensionManifest",
    "ExtensionRunError",
    "ExtensionRunResult",
    "ExtensionRunner",
    "ExtensionHost",
    "ResourceLimits",
    "RunnerConfig",
    "compute_code_hash",
    "load_manifest",
    "run_extension",
]
