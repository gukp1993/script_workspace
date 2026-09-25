"""进阶模板匹配检测器：掩码、尺度集与多模板优先级（VIS-003）。

- **alpha/显式掩码**：模板 RGBA 的 alpha 通道或显式传入的 mask 参与
  匹配；带掩码时用 ``TM_SQDIFF + mask``（OpenCV 仅对 TM_SQDIFF/
  TM_CCORR_NORMED 支持 mask，后者在暗区数值不稳定），并把平方差按
  掩码面积归一化为 0~1 置信度（完全匹配=1.0）；掩码为 0 的区域完全不
  参与匹配分数，可用于"只看前景、忽略背景"的图标匹配；
- **有限尺度集**：可配尺度元组（默认 ``(0.9, 1.0, 1.1)``），对每个尺度
  缩放模板分别匹配，取最优；超出配置尺度的缩放明确失败（不命中）；
- **多模板优先级**：模板按传入顺序即优先级；按序评估，第一个最优分
  达到阈值的模板立即返回（``early_stop``，默认开）。

输出 ``value`` 为命中模板的优先级序号（0 起），便于下游区分命中的是
哪个模板；候选明细存于 ``last_candidates``。
"""

from __future__ import annotations

import cv2
import numpy as np

from capture_api.frames import Frame
from detector_opencv._common import (
    crop_roi,
    now_ms,
    offset_bbox,
    resolve_roi,
    to_gray,
)
from vision_core.base import DetectorResult, NormRoi, clamp01, result_error

__all__ = ["AdvancedTemplateDetector"]


class _TemplateEntry:
    """一个参与匹配的模板条目（灰度图 + 可选掩码 + 标签）。"""

    __slots__ = ("gray", "mask", "label")

    def __init__(
        self,
        gray: np.ndarray,
        mask: np.ndarray | None,
        label: str,
    ) -> None:
        self.gray = gray
        self.mask = mask
        self.label = label


class AdvancedTemplateDetector:
    """掩码 + 有限尺度 + 多模板优先级的模板匹配检测器。"""

    version: str = "1.0.0"

    def __init__(
        self,
        config: object,
        templates: np.ndarray | list[np.ndarray],
        *,
        masks: np.ndarray | list[np.ndarray | None] | None = None,
        template_labels: list[str] | None = None,
        scales: tuple[float, ...] = (0.9, 1.0, 1.1),
        early_stop: bool = True,
    ) -> None:
        """``templates``：单模板或模板列表（RGB/灰度 ndarray，按优先级排序）。

        ``masks``：与模板一一对应的掩码（0~1 float 或 0/255 uint8；
        ``None`` 表示该模板不使用掩码）。掩码亦可由 RGBA 模板的 alpha
        通道自动派生（4 通道模板自动拆分）。
        ``scales``：有限尺度集（正数）；``early_stop``：按优先级命中即返回。
        """
        self._detector_id = str(getattr(config, "detector_id"))
        self._field_name = str(getattr(config, "field_name", self._detector_id))
        self._roi = tuple(float(v) for v in getattr(config, "roi"))  # type: ignore[arg-type]
        self._threshold = float(getattr(config, "threshold", 0.8))
        if not (0.0 <= self._threshold <= 1.0):
            raise ValueError(f"threshold 必须在 0~1，收到 {self._threshold!r}")
        if not scales or any(float(s) <= 0.0 for s in scales):
            raise ValueError(f"scales 必须为正数元组，收到 {scales!r}")
        self._scales = tuple(float(s) for s in scales)
        self._early_stop = bool(early_stop)
        template_list = [templates] if isinstance(templates, np.ndarray) else list(templates)
        if not template_list:
            raise ValueError("templates 不能为空")
        if masks is None:
            mask_list: list[np.ndarray | None] = [None] * len(template_list)
        elif isinstance(masks, np.ndarray):
            mask_list = [masks]
        else:
            mask_list = list(masks)
        if len(mask_list) != len(template_list):
            raise ValueError(
                f"masks 数量({len(mask_list)})必须与 templates 数量({len(template_list)})一致"
            )
        labels = template_labels or [f"tpl_{i}" for i in range(len(template_list))]
        if len(labels) != len(template_list):
            raise ValueError("template_labels 数量必须与 templates 一致")
        self._entries = [
            self._prepare(template_list[i], mask_list[i], labels[i])
            for i in range(len(template_list))
        ]
        self.last_candidates: list[tuple[str, tuple[int, int, int, int], float]] = []

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
    def scales(self) -> tuple[float, ...]:
        """配置的尺度集。"""
        return self._scales

    # ---- 检测 ------------------------------------------------------------

    def detect(self, frame: Frame, roi: NormRoi | None = None) -> DetectorResult:
        """按模板优先级 × 尺度集匹配；返回最优命中或未命中结果。"""
        start = now_ms()

        def elapsed() -> float:
            return now_ms() - start

        try:
            rect = resolve_roi(frame, roi, self._roi)  # type: ignore[arg-type]
        except ValueError:
            return result_error(self._detector_id, "invalid_roi", elapsed(), self.version)
        search = to_gray(crop_roi(frame, rect))
        best: tuple[int, float, tuple[int, int, int, int], str] | None = None
        candidates: list[tuple[str, tuple[int, int, int, int], float]] = []
        for priority, entry in enumerate(self._entries):
            entry_best = self._match_entry(search, rect, entry)
            if entry_best is None:
                continue
            score, bbox, label = entry_best
            candidates.append((label, bbox, score))
            if best is None or score > best[1]:
                best = (priority, score, bbox, label)
            if self._early_stop and score >= self._threshold:
                break  # 优先级命中即停：低优先级模板不再消耗算力。
        self.last_candidates = sorted(candidates, key=lambda c: -c[2])
        if best is None or best[1] < self._threshold:
            conf = clamp01(best[1]) if best is not None else 0.0
            return DetectorResult(
                detector_id=self._detector_id,
                present=False,
                confidence=conf,
                value=None,
                bbox=None,
                elapsed_ms=elapsed(),
                version=self.version,
                error=None if best is not None else "no_candidate",
            )
        priority, score, bbox, label = best
        return DetectorResult(
            detector_id=self._detector_id,
            present=True,
            confidence=clamp01(score),
            value=float(priority),
            bbox=bbox,
            elapsed_ms=elapsed(),
            version=self.version,
            error=None,
        )

    # ---- 内部 ------------------------------------------------------------

    def _match_entry(
        self, search: np.ndarray, rect: tuple[int, int, int, int], entry: _TemplateEntry
    ) -> tuple[float, tuple[int, int, int, int], str] | None:
        """单个模板在尺度集上的最优匹配；搜索区过小返回 None。"""
        best: tuple[float, tuple[int, int, int, int], str] | None = None
        for scale in self._scales:
            th = max(1, int(round(entry.gray.shape[0] * scale)))
            tw = max(1, int(round(entry.gray.shape[1] * scale)))
            if search.shape[0] < th or search.shape[1] < tw:
                continue  # 尺度超出搜索区：该尺度明确不参与（失败）。
            scaled = (
                entry.gray
                if scale == 1.0
                else cv2.resize(entry.gray, (tw, th), interpolation=cv2.INTER_AREA)
            )
            mask = None
            if entry.mask is not None:
                mask = (
                    entry.mask
                    if scale == 1.0
                    else cv2.resize(entry.mask, (tw, th), interpolation=cv2.INTER_NEAREST)
                )
            if mask is not None:
                # OpenCV 的 TM_CCORR_NORMED+mask 在暗区会产生 NaN/爆炸值，
                # 改用稳定的 TM_SQDIFF+mask：完全匹配=0，按掩码面积归一化
                # 为 0~1 置信度（RMS 误差的线性映射）。
                sq = cv2.matchTemplate(search, scaled, cv2.TM_SQDIFF, mask=mask)
                sq = np.nan_to_num(sq, nan=np.inf, posinf=np.inf, neginf=np.inf)
                mask_area = float(mask.sum()) * 255.0 * 255.0
                rms = np.sqrt(np.maximum(sq, 0.0) / mask_area)
                conf_map = 1.0 - rms
                _mn, _mx, _mnl, mxl = cv2.minMaxLoc(conf_map)
                score = clamp01(float(conf_map[int(mxl[1]), int(mxl[0])]))
            else:
                scores = cv2.matchTemplate(search, scaled, cv2.TM_CCOEFF_NORMED)
                _mn, mx, _mnl, mxl = cv2.minMaxLoc(scores)
                score = clamp01(float(mx))
            bbox = offset_bbox(rect, (int(mxl[0]), int(mxl[1]), tw, th))
            if best is None or score > best[0]:
                best = (score, bbox, entry.label)
        return best

    @staticmethod
    def _prepare(
        template: np.ndarray, mask: np.ndarray | None, label: str
    ) -> _TemplateEntry:
        """归一化模板（RGBA 自动拆 alpha）与掩码（0/1 float32）。"""
        arr = np.asarray(template)
        derived_mask: np.ndarray | None = None
        if arr.ndim == 3 and arr.shape[2] == 4:
            derived_mask = (arr[:, :, 3].astype(np.float32) / 255.0).astype(np.float32)
            arr = arr[:, :, :3]
        gray = to_gray(arr) if arr.ndim == 3 else arr.astype(np.uint8)
        prepared_mask: np.ndarray | None = None
        if mask is not None:
            m = np.asarray(mask)
            if m.dtype == np.uint8 and m.max() > 1:
                m = (m.astype(np.float32) / 255.0).astype(np.float32)
            else:
                m = m.astype(np.float32)
            if m.shape[:2] != gray.shape[:2]:
                raise ValueError(f"掩码尺寸 {m.shape[:2]} 必须与模板 {gray.shape[:2]} 一致")
            prepared_mask = m
        elif derived_mask is not None:
            prepared_mask = derived_mask
        return _TemplateEntry(np.ascontiguousarray(gray), prepared_mask, str(label))
