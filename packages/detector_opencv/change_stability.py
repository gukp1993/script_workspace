"""画面变化、稳定与卡死检测器（VIS-006）。

有状态检测器（``reset()`` 可复位），按帧序驱动：

- **差分**：相邻帧灰度绝对差的均值与"变化像素比例"（|Δ| 超过
  ``pixel_threshold`` 的像素占比）；
- **稳定判定**：连续 ``static_frames`` 帧变化比例 ≤ ``static_ratio``
  → ``is_static``；
- **卡死检测**：静态且零活动（差分完全为零）持续超过
  ``frozen_timeout_frames`` → ``is_frozen``；
- **加载动画区分**：变化比例低但持续非零（如进度条/帧号局部更新，
  结构不变）→ ``is_progressing``，不判卡死；结构性的大变化
  （变化比例 > ``change_ratio``）→ ``changed``。

输出语义由 ``emit`` 选择（下游按需实例化不同检测器）：

- ``"changed"``：present = 本帧发生了（结构性）变化；
- ``"static"``：present = 已进入稳定（连续 M 帧低变化）；
- ``"frozen"``：present = 卡死（稳定超时且零活动）。

``value`` 恒为最近一帧的变化像素比例（float，可观测）。
"""

from __future__ import annotations

import numpy as np

from capture_api.frames import Frame
from detector_opencv._common import crop_roi, now_ms, resolve_roi, to_gray
from vision_core.base import DetectorResult, NormRoi, clamp01, result_error

__all__ = ["ChangeStabilityDetector"]


class ChangeStabilityDetector:
    """画面变化/稳定/卡死检测器（有状态，提供 ``reset()``）。"""

    version: str = "1.0.0"

    def __init__(
        self,
        config: object,
        *,
        emit: str = "changed",
        pixel_threshold: float = 12.0,
        change_ratio: float = 1e-4,
        static_ratio: float | None = None,
        static_frames: int = 5,
        frozen_timeout_frames: int = 150,
        progressing_ratio: float = 0.01,
    ) -> None:
        """``config`` 为 ``domain_model.Detector``（或等价字段对象）。

        Attributes:
            emit:                  输出语义：changed / static / frozen。
            pixel_threshold:       单像素差分计入"变化"的灰度下限。
            change_ratio:          判定"发生（结构性）变化"的像素比例下限。
            static_ratio:          计入"低变化（静止趋势）"的比例上限；
                                   缺省等于 ``change_ratio``。
            static_frames:         进入稳定的连续低变化帧数（>=1）。
            frozen_timeout_frames: 卡死判定所需的连续零活动帧数（>=1）。
            progressing_ratio:     "低活动但非零"（加载类动画）比例上限。
        """
        self._detector_id = str(getattr(config, "detector_id"))
        self._field_name = str(getattr(config, "field_name", self._detector_id))
        self._roi = tuple(float(v) for v in getattr(config, "roi"))  # type: ignore[arg-type]
        emit_norm = str(emit).lower()
        if emit_norm not in ("changed", "static", "frozen"):
            raise ValueError(f"emit 只允许 changed/static/frozen，收到 {emit!r}")
        self._emit = emit_norm
        self._pixel_threshold = float(pixel_threshold)
        self._change_ratio = float(change_ratio)
        self._static_ratio = float(static_ratio if static_ratio is not None else change_ratio)
        self._static_frames = max(1, int(static_frames))
        self._frozen_timeout = max(1, int(frozen_timeout_frames))
        self._progressing_ratio = float(progressing_ratio)
        self.reset()

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
    def emit(self) -> str:
        """输出语义。"""
        return self._emit

    @property
    def last_ratio(self) -> float:
        """最近一帧的变化像素比例（诊断）。"""
        return self._last_ratio

    @property
    def is_static(self) -> bool:
        """是否已进入稳定（连续 M 帧低变化）。"""
        return self._static_streak >= self._static_frames

    @property
    def is_frozen(self) -> bool:
        """是否卡死（稳定超时且零活动）。"""
        return self._zero_streak >= self._frozen_timeout

    @property
    def is_progressing(self) -> bool:
        """是否为低活动但持续非零（加载类动画）状态。"""
        return self._last_ratio > 0.0 and self._last_ratio <= self._progressing_ratio

    @property
    def static_streak(self) -> int:
        """当前连续低变化帧数（诊断）。"""
        return self._static_streak

    def reset(self) -> None:
        """清空状态（换场景/回放起点调用）。"""
        self._prev_gray: np.ndarray | None = None
        self._last_ratio = 0.0
        self._mean_diff = 0.0
        self._static_streak = 0
        self._zero_streak = 0
        self._first_frame = True

    # ---- 检测 ------------------------------------------------------------

    def detect(self, frame: Frame, roi: NormRoi | None = None) -> DetectorResult:
        """处理一帧并返回按 ``emit`` 语义的判定结果。"""
        start = now_ms()

        def elapsed() -> float:
            return now_ms() - start

        try:
            rect = resolve_roi(frame, roi, self._roi)  # type: ignore[arg-type]
        except ValueError:
            return result_error(self._detector_id, "invalid_roi", elapsed(), self.version)
        gray = to_gray(crop_roi(frame, rect)).astype(np.int16)
        if self._prev_gray is None or gray.shape != self._prev_gray.shape:
            # 首帧（或 ROI 形状突变）：无差分可比，重置计时，不算变化。
            self._prev_gray = gray
            self.reset_streaks_keep_shape()
            return self._emit_result(ratio=0.0, changed=False, elapsed=elapsed())
        diff = np.abs(gray - self._prev_gray)
        self._prev_gray = gray
        ratio = float((diff > self._pixel_threshold).mean())
        mean_diff = float(diff.mean())
        self._last_ratio = ratio
        self._mean_diff = mean_diff
        changed = ratio > self._change_ratio
        if ratio <= self._static_ratio:
            self._static_streak += 1
        else:
            self._static_streak = 0
        if ratio == 0.0:
            self._zero_streak += 1
        else:
            self._zero_streak = 0
        return self._emit_result(ratio=ratio, changed=changed, elapsed=elapsed())

    # ---- 内部 ------------------------------------------------------------

    def reset_streaks_keep_shape(self) -> None:
        """保留参考帧，重置连击计数（ROI 形状突变时调用）。"""
        self._static_streak = 0
        self._zero_streak = 0
        self._last_ratio = 0.0
        self._mean_diff = 0.0

    def _emit_result(self, ratio: float, changed: bool, elapsed: float) -> DetectorResult:
        """按 ``emit`` 语义包装输出。"""
        if self._emit == "changed":
            present = changed
        elif self._emit == "static":
            present = self.is_static
        else:  # frozen
            present = self.is_frozen
        return DetectorResult(
            detector_id=self._detector_id,
            present=bool(present),
            confidence=clamp01(ratio) if present else 0.0,
            value=float(ratio),
            bbox=None,
            elapsed_ms=elapsed,
            version=self.version,
            error=None,
        )
