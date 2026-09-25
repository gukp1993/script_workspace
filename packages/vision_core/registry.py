"""检测器类型注册表（VIS-001）。

``DETECTOR_REGISTRY`` 把 ``domain_model.DetectorType`` 的 type 枚举字符串
映射到实现类；:func:`create_detector` 按 type 实例化检测器（算法特有
参数通过关键字参数透传）。

导入本模块会引入 detector_opencv（OpenCV）实现类；只需输出契约的下游
（FSM/回放）应直接导入 :mod:`vision_core.base`，避免不必要的依赖。
"""

from __future__ import annotations

from typing import Any

from detector_opencv.change_stability import ChangeStabilityDetector
from detector_opencv.color_bar import ColorBarRatioDetector
from detector_opencv.color_region import ColorRegionDetector
from detector_opencv.ocr_roi import RoiOcrDetector
from detector_opencv.template_match import TemplateMatchDetector

__all__ = ["DETECTOR_REGISTRY", "create_detector", "registered_types"]

#: type 枚举字符串 -> 检测器实现类（template_match / color_bar_ratio /
#: color_region / change_stability / ocr_roi）。
DETECTOR_REGISTRY: dict[str, type] = {
    "template_match": TemplateMatchDetector,
    "color_bar_ratio": ColorBarRatioDetector,
    "color_region": ColorRegionDetector,
    "change_stability": ChangeStabilityDetector,
    "ocr_roi": RoiOcrDetector,
}


def registered_types() -> tuple[str, ...]:
    """已注册的检测器类型名（字典序）。"""
    return tuple(sorted(DETECTOR_REGISTRY))


def create_detector(detector_config: Any, **kwargs: Any):
    """按配置的 type 实例化检测器。

    ``detector_config`` 为 ``domain_model.Detector``（或等价字段对象，
    需含 ``type``）；算法特有参数（模板数组、颜色范围、OCR 引擎等）
    经 ``kwargs`` 透传给实现类构造器。
    """
    det_type = getattr(detector_config, "type", None)
    name = getattr(det_type, "value", det_type)
    cls = DETECTOR_REGISTRY.get(str(name))
    if cls is None:
        raise KeyError(
            f"未注册的检测器类型 {name!r}；可用：{', '.join(registered_types())}"
        )
    return cls(detector_config, **kwargs)  # type: ignore[call-arg]
