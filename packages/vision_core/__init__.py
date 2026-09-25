"""vision_core：视觉检测统一契约与调度/聚合/评测基础设施（E06）。

模块一览：

- :mod:`vision_core.base`：``DetectorResult``/``Detector`` 契约与
  ``PerceptionBuilder``（VIS-001）；
- :mod:`vision_core.stability`：稳定帧、迟滞与置信度聚合（VIS-009）；
- :mod:`vision_core.scheduler`：检测器调度、频率与计算预算（VIS-008）；
- :mod:`vision_core.golden`：黄金数据集与指标计算（VIS-011）；
- :mod:`vision_core.offline`：离线批量运行与差异报告（VIS-012）；
- :mod:`vision_core.registry`：type 枚举 -> OpenCV 实现类注册表
  （会引入 OpenCV 依赖，按需导入）。
"""

from vision_core.base import (
    Detector,
    DetectorResult,
    PerceptionBuilder,
    clamp01,
    result_error,
)
from vision_core.golden import (
    GoldenCase,
    GoldenDataset,
    build_from_scenario,
    metrics,
)
from vision_core.offline import (
    CaseResult,
    DiffReport,
    SampleDiff,
    batch_run,
    diff_reports,
)
from vision_core.scheduler import (
    DetectorScheduler,
    SchedulerEntry,
    SchedulerStat,
)
from vision_core.stability import StabilityConfig, StableFrameAggregator

__all__ = [
    "Detector",
    "DetectorResult",
    "PerceptionBuilder",
    "clamp01",
    "result_error",
    "StabilityConfig",
    "StableFrameAggregator",
    "DetectorScheduler",
    "SchedulerEntry",
    "SchedulerStat",
    "GoldenCase",
    "GoldenDataset",
    "build_from_scenario",
    "metrics",
    "CaseResult",
    "SampleDiff",
    "DiffReport",
    "batch_run",
    "diff_reports",
]
