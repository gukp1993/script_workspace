"""颜色条比例检测器（VIS-004）。

在 ROI 内按方向（水平/垂直）投影定位"条槽"（槽底 + 填充带），再统计
填充像素占比，输出 ``value ∈ [0, 1]``（血条/资源条/进度条类语义）。

亮度鲁棒性（VIS-004 验收）：填充像素用**通道散布**（max-min，对加性
亮度变化严格不变）+ 最低亮度下限识别，而不是绝对亮度或绝对饱和度，
整帧 ±20 亮度扰动下判定稳定。

- 置信度：填充区域矩形度（填充像素数 / 填充外接框面积）加权——
  规则图形（真实条形填充）置信度高，噪声/碎片置信度下降；
- 异常：未找到条槽、比例计算为负等 → ``value=None`` + ``error``；
- 填充方向：约定"从起点向终点填充"（水平条从左到右，垂直条从上到下），
  与 arena_lab 渲染器一致，也覆盖常见游戏 UI 语义。

可选 ``hue_range``：配置 (lo, hi)（OpenCV H 0~179）时，仅色相落在范围
内的彩色像素计为填充（用于同屏多条、颜色互斥的场景）。
"""

from __future__ import annotations

import numpy as np

from capture_api.frames import Frame
from detector_opencv._common import crop_roi, now_ms, resolve_roi, rgb_to_hsv
from vision_core.base import DetectorResult, NormRoi, clamp01, result_error

__all__ = ["ColorBarRatioDetector"]


class ColorBarRatioDetector:
    """颜色条填充比例检测器（投影找槽 + 散布分类填充）。"""

    version: str = "1.0.0"

    def __init__(
        self,
        config: object,
        *,
        axis: str = "horizontal",
        spread_min: float = 50.0,
        value_min: float = 60.0,
        slot_value_max: float = 70.0,
        slot_spread_max: float = 30.0,
        hue_range: tuple[float, float] | None = None,
    ) -> None:
        """``config`` 为 ``domain_model.Detector``（或等价字段对象）。

        Attributes（算法参数，均有确定性默认值）:
            axis:           条方向：``horizontal``（从左向右）或 ``vertical``
                            （从上到下）。
            spread_min:     填充像素的通道散布下限（加性亮度不变量）。
            value_min:      填充像素的最低亮度（排除近黑噪声）。
            slot_value_max: 槽底像素的亮度上限（槽底为暗中性色）。
            slot_spread_max: 槽底像素的散布上限（低饱和中性）。
            hue_range:      可选色相过滤 (lo, hi)（OpenCV H 0~179）。
        """
        self._detector_id = str(getattr(config, "detector_id"))
        self._field_name = str(getattr(config, "field_name", self._detector_id))
        self._roi = tuple(float(v) for v in getattr(config, "roi"))  # type: ignore[arg-type]
        self._threshold = float(getattr(config, "threshold", 0.5))
        axis_norm = str(axis).lower()
        if axis_norm not in ("horizontal", "vertical"):
            raise ValueError(f"axis 只允许 horizontal/vertical，收到 {axis!r}")
        self._axis = axis_norm
        self._spread_min = float(spread_min)
        self._value_min = float(value_min)
        self._slot_value_max = float(slot_value_max)
        self._slot_spread_max = float(slot_spread_max)
        self._hue_range = hue_range

    # ---- 属性 ------------------------------------------------------------

    @property
    def detector_id(self) -> str:
        """检测器 ID。"""
        return self._detector_id

    @property
    def field_name(self) -> str:
        """输出语义字段名。"""
        return self._field_name

    @property
    def axis(self) -> str:
        """条方向。"""
        return self._axis

    # ---- 检测 ------------------------------------------------------------

    def detect(self, frame: Frame, roi: NormRoi | None = None) -> DetectorResult:
        """测量 ROI 内颜色条的填充比例。"""
        start = now_ms()

        def elapsed() -> float:
            return now_ms() - start

        try:
            rect = resolve_roi(frame, roi, self._roi)  # type: ignore[arg-type]
        except ValueError:
            return result_error(self._detector_id, "invalid_roi", elapsed(), self.version)
        crop = crop_roi(frame, rect)
        if crop.size == 0:
            return result_error(self._detector_id, "empty_roi", elapsed(), self.version)

        mx = crop.max(axis=2).astype(np.int16)
        mn = crop.min(axis=2).astype(np.int16)
        spread = mx - mn
        fill = (spread >= self._spread_min) & (mx >= self._value_min)
        if self._hue_range is not None:
            hue = rgb_to_hsv(crop)[:, :, 0]
            lo, hi = self._hue_range
            fill &= (hue >= lo) & (hue <= hi)
        slot = (mx <= self._slot_value_max) & (spread <= self._slot_spread_max)
        band = fill | slot

        # 方向投影：把条带（fill|slot）的行/列范围框出来，再在带内统计。
        if self._axis == "horizontal":
            # 水平条：带行 = band 像素显著的行；填充沿列方向累积。
            row_max = float(band.mean(axis=1).max()) if band.size else 0.0
            row_frac = band.mean(axis=1)
            rows = np.flatnonzero(row_frac >= 0.5 * row_max) if row_max > 0.0 else np.empty(0, dtype=int)
            if rows.size == 0:
                return result_error(
                    self._detector_id, "bar_not_found", elapsed(), self.version
                )
            band_rows = crop[rows[0] : rows[-1] + 1]
            band_fill = fill[rows[0] : rows[-1] + 1]
            # 注意：投影用 2D 的 band（crop 是 3D，mean(axis=0) 会保留通道维）。
            col_frac = band[rows[0] : rows[-1] + 1].mean(axis=0)
            cols = np.flatnonzero(col_frac >= 0.5)
            if cols.size == 0:
                return result_error(
                    self._detector_id, "bar_not_found", elapsed(), self.version
                )
            inner_width = int(cols[-1] - cols[0] + 1)
            fill_cols = np.flatnonzero(band_fill.any(axis=0))
            if fill_cols.size == 0:
                return self._ratio_result(0.0, 0.5, rect, rows, cols, elapsed())
            start_col = int(cols[0])
            fill_end = int(fill_cols[-1]) + 1
            raw_ratio = (fill_end - start_col) / inner_width
            if raw_ratio < 0.0:
                return result_error(
                    self._detector_id, "negative_ratio", elapsed(), self.version
                )
            # 置信度：填充矩形度（真条形填充应接近 1）。
            f_rows = np.flatnonzero(band_fill.any(axis=1))
            f_cols = fill_cols
            rectness = float(
                band_fill[f_rows[0] : f_rows[-1] + 1, f_cols[0] : f_cols[-1] + 1].mean()
            )
            return self._ratio_result(
                clamp01(raw_ratio), 0.5 + 0.5 * rectness, rect, rows, cols, elapsed()
            )
        # 垂直条：与水平对称（从上向下填充）。
        col_max = float(band.mean(axis=0).max()) if band.size else 0.0
        col_frac = band.mean(axis=0)
        cols = np.flatnonzero(col_frac >= 0.5 * col_max) if col_max > 0.0 else np.empty(0, dtype=int)
        if cols.size == 0:
            return result_error(self._detector_id, "bar_not_found", elapsed(), self.version)
        band_cols = crop[:, cols[0] : cols[-1] + 1]
        band_fill = fill[:, cols[0] : cols[-1] + 1]
        # 投影用 2D 的 band（理由同水平分支）。
        row_frac = band[:, cols[0] : cols[-1] + 1].mean(axis=1)
        rows = np.flatnonzero(row_frac >= 0.5)
        if rows.size == 0:
            return result_error(self._detector_id, "bar_not_found", elapsed(), self.version)
        inner_height = int(rows[-1] - rows[0] + 1)
        fill_rows = np.flatnonzero(band_fill.any(axis=1))
        if fill_rows.size == 0:
            return self._ratio_result(0.0, 0.5, rect, rows, cols, elapsed())
        start_row = int(rows[0])
        fill_end = int(fill_rows[-1]) + 1
        raw_ratio = (fill_end - start_row) / inner_height
        if raw_ratio < 0.0:
            return result_error(self._detector_id, "negative_ratio", elapsed(), self.version)
        f_cols = np.flatnonzero(band_fill.any(axis=0))
        rectness = float(
            band_fill[fill_rows[0] : fill_rows[-1] + 1, f_cols[0] : f_cols[-1] + 1].mean()
        )
        return self._ratio_result(
            clamp01(raw_ratio), 0.5 + 0.5 * rectness, rect, rows, cols, elapsed()
        )

    # ---- 内部 ------------------------------------------------------------

    def _ratio_result(
        self,
        ratio: float,
        confidence: float,
        rect: tuple[int, int, int, int],
        rows: np.ndarray,
        cols: np.ndarray,
        elapsed: float,
    ) -> DetectorResult:
        """构造比例结果（bbox = 条带外接框，帧坐标系）。"""
        x0, y0, _x1, _y1 = rect
        bbox = (
            int(x0 + cols[0]),
            int(y0 + rows[0]),
            int(cols[-1] - cols[0] + 1),
            int(rows[-1] - rows[0] + 1),
        )
        return DetectorResult(
            detector_id=self._detector_id,
            present=ratio >= self._threshold,
            confidence=clamp01(confidence),
            value=float(clamp01(ratio)),
            bbox=bbox,
            elapsed_ms=elapsed,
            version=self.version,
            error=None,
        )
