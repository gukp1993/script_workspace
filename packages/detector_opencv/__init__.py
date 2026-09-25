"""detector_opencv：基于 OpenCV 的检测器实现包（E06）。

模块一览：

- :mod:`detector_opencv.template_match`：基础模板匹配（VIS-002）；
- :mod:`detector_opencv.template_advanced`：掩码/尺度/多模板（VIS-003）；
- :mod:`detector_opencv.color_bar`：颜色条比例（VIS-004）；
- :mod:`detector_opencv.color_region`：颜色区域/像素统计（VIS-005）；
- :mod:`detector_opencv.change_stability`：变化/稳定/卡死（VIS-006）；
- :mod:`detector_opencv.ocr_roi`：事件触发式 ROI OCR（VIS-007）；
- :mod:`detector_opencv.overlay`：调试叠加数据层（VIS-010 数据部分）；
- :mod:`detector_opencv._common`：内部公共工具（不属于对外契约）。

所有实现类实现 :mod:`vision_core.base` 定义的 ``Detector`` 协议，输出
统一的 :class:`vision_core.base.DetectorResult`。
"""

from detector_opencv.change_stability import ChangeStabilityDetector
from detector_opencv.color_bar import ColorBarRatioDetector
from detector_opencv.color_region import ColorRegionDetector
from detector_opencv.ocr_roi import (
    EngineUnavailableError,
    MockOcrEngine,
    OcrEngine,
    OcrReading,
    RoiOcrDetector,
    TesseractOcrEngine,
)
from detector_opencv.overlay import OverlayData, OverlayItem
from detector_opencv.template_advanced import AdvancedTemplateDetector
from detector_opencv.template_match import (
    TemplateMatchCandidate,
    TemplateMatchDetector,
)

__all__ = [
    "TemplateMatchDetector",
    "TemplateMatchCandidate",
    "AdvancedTemplateDetector",
    "ColorBarRatioDetector",
    "ColorRegionDetector",
    "ChangeStabilityDetector",
    "OcrEngine",
    "OcrReading",
    "MockOcrEngine",
    "RoiOcrDetector",
    "TesseractOcrEngine",
    "EngineUnavailableError",
    "OverlayItem",
    "OverlayData",
]
