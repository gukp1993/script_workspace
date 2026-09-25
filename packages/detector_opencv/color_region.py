"""颜色区域/像素统计检测器（VIS-005）。

支持 HSV/RGB 颜色范围（多段 OR）、面积比例输出与连通域过滤（最小面积）：

- ``ranges``：``[(lo3, hi3), ...]`` 多段范围（OR 语义）；``color_space``
  决定按 HSV（OpenCV 约定 H 0~179、S/V 0~255）或 RGB 解释；
- ``value``：满足范围且通过连通域过滤的像素面积 / ROI 面积 ∈ [0, 1]；
- ``bbox``：最大保留连通域的外接框（帧像素坐标）；
- ``present``：``value >= threshold`` 且至少保留一个连通域；
- 连通域过滤：``min_area_px``/``min_area_ratio``（相对 ROI 面积）之下的
  噪点连通域剔除（cv2.connectedComponentsWithStats，8 连通）。
"""

from __future__ import annotations

from collections.abc import Sequence

import cv2
import numpy as np

from capture_api.frames import Frame
from detector_opencv._common import (
    crop_roi,
    now_ms,
    offset_bbox,
    resolve_roi,
    rgb_in_ranges,
    rgb_to_hsv,
)
from vision_core.base import DetectorResult, NormRoi, clamp01, result_error

__all__ = ["ColorRegionDetector"]


class ColorRegionDetector:
    """颜色区域/像素统计检测器（HSV/RGB 范围 + 连通域过滤）。"""

    version: str = "1.0.0"

    def __init__(
        self,
        config: object,
        ranges: Sequence[tuple[Sequence[float], Sequence[float]]],
        *,
        color_space: str = "hsv",
        min_area_px: int = 1,
        min_area_ratio: float = 0.0,
        connectivity: int = 8,
    ) -> None:
        """``config`` 为 ``domain_model.Detector``（或等价字段对象）。

        Attributes:
            ranges:         颜色范围列表 [(lo3, hi3), ...]（OR 语义）；
                            HSV 时 H 分量范围按 OpenCV 约定（0~179）。
            color_space:    ``hsv``（默认）或 ``rgb``。
            min_area_px:    连通域最小面积（像素数，下限 1）。
            min_area_ratio: 连通域最小面积占 ROI 面积比例（0~1，可与
                            ``min_area_px`` 叠加，取较大者）。
            connectivity:   连通域连通性（4 或 8，默认 8）。
        """
        self._detector_id = str(getattr(config, "detector_id"))
        self._field_name = str(getattr(config, "field_name", self._detector_id))
        self._roi = tuple(float(v) for v in getattr(config, "roi"))  # type: ignore[arg-type]
        self._threshold = float(getattr(config, "threshold", 0.1))
        space = str(color_space).lower()
        if space not in ("hsv", "rgb"):
            raise ValueError(f"color_space 只允许 hsv/rgb，收到 {color_space!r}")
        if not ranges:
            raise ValueError("ranges 不能为空")
        self._color_space = space
        self._ranges = [
            ([float(v) for v in lo], [float(v) for v in hi]) for lo, hi in ranges
        ]
        self._min_area_px = max(1, int(min_area_px))
        if not (0.0 <= float(min_area_ratio) <= 1.0):
            raise ValueError(f"min_area_ratio 必须在 0~1，收到 {min_area_ratio!r}")
        self._min_area_ratio = float(min_area_ratio)
        if int(connectivity) not in (4, 8):
            raise ValueError(f"connectivity 只允许 4/8，收到 {connectivity!r}")
        self._connectivity = int(connectivity)

    # ---- 属性 ------------------------------------------------------------

    @property
    def detector_id(self) -> str:
        """检测器 ID。"""
        return self._detector_id

    @property
    def field_name(self) -> str:
        """输出语义字段名。"""
        return self._field_name

    # ---- 检测 ------------------------------------------------------------

    def detect(self, frame: Frame, roi: NormRoi | None = None) -> DetectorResult:
        """统计 ROI 内命中颜色范围的面积比例与最大连通域。"""
        start = now_ms()

        def elapsed() -> float:
            return now_ms() - start

        try:
            rect = resolve_roi(frame, roi, self._roi)  # type: ignore[arg-type]
        except ValueError:
            return result_error(self._detector_id, "invalid_roi", elapsed(), self.version)
        crop = crop_roi(frame, rect)
        height, width = crop.shape[:2]
        if height < 1 or width < 1:
            return result_error(self._detector_id, "empty_roi", elapsed(), self.version)
        if self._color_space == "hsv":
            mask = self._mask(crop)
        else:
            mask = rgb_in_ranges(crop, self._ranges)
        roi_area = float(height * width)
        min_area = max(self._min_area_px, int(np.ceil(self._min_area_ratio * roi_area)))

        # 连通域过滤：剔除面积不足的噪点，保留达标连通域。
        count, labels, stats, _centers = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8), connectivity=self._connectivity
        )
        kept = np.zeros_like(mask, dtype=bool)
        best_label = -1
        best_area = 0
        for label in range(1, count):  # 0 为背景
            area = int(stats[label, cv2.CC_STAT_AREA])
            if area < min_area:
                continue
            kept |= labels == label
            if area > best_area:
                best_area = area
                best_label = label
        value = clamp01(float(kept.sum()) / roi_area)
        present = value >= self._threshold and best_label >= 0
        bbox: tuple[int, int, int, int] | None = None
        if best_label >= 0:
            lx = int(stats[best_label, cv2.CC_STAT_LEFT])
            ly = int(stats[best_label, cv2.CC_STAT_TOP])
            lw = int(stats[best_label, cv2.CC_STAT_WIDTH])
            lh = int(stats[best_label, cv2.CC_STAT_HEIGHT])
            bbox = offset_bbox(rect, (lx, ly, lw, lh))
        return DetectorResult(
            detector_id=self._detector_id,
            present=present,
            confidence=clamp01(value) if present else clamp01(value * 0.5),
            value=float(value),
            bbox=bbox,
            elapsed_ms=elapsed(),
            version=self.version,
            error=None,
        )

    # ---- 内部 ------------------------------------------------------------

    def _mask(self, crop: np.ndarray) -> np.ndarray:
        """按配置颜色空间计算命中掩码（多段范围 OR）。"""
        if self._color_space == "hsv":
            hsv = rgb_to_hsv(crop)
            out = np.zeros(crop.shape[:2], dtype=bool)
            for lo, hi in self._ranges:
                out |= (
                    (hsv[:, :, 0] >= lo[0]) & (hsv[:, :, 0] <= hi[0])
                    & (hsv[:, :, 1] >= lo[1]) & (hsv[:, :, 1] <= hi[1])
                    & (hsv[:, :, 2] >= lo[2]) & (hsv[:, :, 2] <= hi[2])
                )
            return out
        return rgb_in_ranges(crop, self._ranges)
