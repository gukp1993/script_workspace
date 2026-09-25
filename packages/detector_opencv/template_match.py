"""基础模板匹配检测器（VIS-002）。

灰度化 + ``cv2.matchTemplate``（``TM_CCOEFF_NORMED``，对线性光照变化
不敏感），支持：

- 阈值判定（``threshold``，配置于 ``domain_model.Detector``）；
- ROI（归一化 ``[x, y, w, h]``，``detect`` 参数可覆盖配置值）；
- 最佳匹配（bbox 为帧像素坐标 ``(x, y, w, h)``）；
- top-k 候选（非极大抑制后按分数降序，存于 ``last_candidates`` 供
  调试叠加层消费）。

模板既可直接给数组（测试/内存场景），也可给 PNG 路径（生产资产）。
模板尺寸超过搜索区域时返回 ``error="template_larger_than_roi"``。
"""

from __future__ import annotations

from pathlib import Path

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

__all__ = ["TemplateMatchCandidate", "TemplateMatchDetector"]


class TemplateMatchCandidate:
    """一个匹配候选：帧坐标 bbox + 归一化分数。"""

    __slots__ = ("bbox", "score")

    def __init__(self, bbox: tuple[int, int, int, int], score: float) -> None:
        self.bbox = bbox
        self.score = float(score)

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"TemplateMatchCandidate(bbox={self.bbox}, score={self.score:.4f})"


class TemplateMatchDetector:
    """基础模板匹配检测器（灰度 + TM_CCOEFF_NORMED + top-k 候选）。

    Attributes:
        last_candidates: 最近一次检测的 top-k 候选（调试数据层消费；
            供 VIS-010 叠加与人工排查，不参与判定）。
    """

    version: str = "1.0.0"

    def __init__(
        self,
        config: object,
        *,
        template: np.ndarray | None = None,
        template_path: str | Path | None = None,
        top_k: int = 3,
    ) -> None:
        """``config`` 为 ``domain_model.Detector``（或等价字段对象）。

        模板二选一：``template``（RGB/灰度 ndarray）或 ``template_path``
        （PNG/BMP 等可读文件）；都缺省在构造期抛 ``ValueError``。
        """
        self._detector_id = str(getattr(config, "detector_id"))
        self._field_name = str(getattr(config, "field_name", self._detector_id))
        self._roi = tuple(float(v) for v in getattr(config, "roi"))  # type: ignore[arg-type]
        self._threshold = float(getattr(config, "threshold", 0.8))
        if not (0.0 <= self._threshold <= 1.0):
            raise ValueError(f"threshold 必须在 0~1，收到 {self._threshold!r}")
        self._top_k = max(1, int(top_k))
        self._template_gray = self._prepare_template(template, template_path)
        self.last_candidates: tuple[TemplateMatchCandidate, ...] = ()

    # ---- 属性 ------------------------------------------------------------

    @property
    def detector_id(self) -> str:
        """检测器 ID。"""
        return self._detector_id

    @property
    def field_name(self) -> str:
        """输出语义字段名（PerceptionSnapshot 键）。"""
        return self._field_name

    @property
    def threshold(self) -> float:
        """判定阈值。"""
        return self._threshold

    @property
    def template_shape(self) -> tuple[int, int]:
        """模板尺寸 (高, 宽)。"""
        return self._template_gray.shape[:2]

    # ---- 检测 ------------------------------------------------------------

    def detect(self, frame: Frame, roi: NormRoi | None = None) -> DetectorResult:
        """对一帧执行模板匹配；ROI 参数可覆盖配置 ROI。"""
        start = now_ms()

        def elapsed() -> float:
            return now_ms() - start

        try:
            rect = resolve_roi(frame, roi, self._roi)  # type: ignore[arg-type]
        except ValueError as exc:
            return result_error(self._detector_id, "invalid_roi", elapsed(), self.version)
        search = to_gray(crop_roi(frame, rect))
        th, tw = self._template_gray.shape[:2]
        if search.shape[0] < th or search.shape[1] < tw:
            return result_error(
                self._detector_id, "template_larger_than_roi", elapsed(), self.version
            )
        scores = cv2.matchTemplate(search, self._template_gray, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(scores)
        confidence = clamp01(float(max_val))
        best_bbox = offset_bbox(rect, (int(max_loc[0]), int(max_loc[1]), tw, th))
        self.last_candidates = tuple(
            TemplateMatchCandidate(bbox, score)
            for bbox, score in self._topk(scores, rect, tw, th)
        )
        present = confidence >= self._threshold
        return DetectorResult(
            detector_id=self._detector_id,
            present=present,
            confidence=confidence,
            value=float(confidence) if present else None,
            bbox=best_bbox if present else None,
            elapsed_ms=elapsed(),
            version=self.version,
            error=None,
        )

    # ---- 内部 ------------------------------------------------------------

    def _topk(
        self, scores: np.ndarray, rect: tuple[int, int, int, int], tw: int, th: int
    ) -> list[tuple[tuple[int, int, int, int], float]]:
        """非极大抑制式 top-k 候选（分数降序；邻域 = 模板半宽高）。"""
        work = scores.copy()
        out: list[tuple[tuple[int, int, int, int], float]] = []
        for _ in range(self._top_k):
            _mn, mx, _mnl, mxl = cv2.minMaxLoc(work)
            if mx <= -1.0:
                break
            lx, ly = int(mxl[0]), int(mxl[1])
            out.append((offset_bbox(rect, (lx, ly, tw, th)), clamp01(float(mx))))
            # 抑制当前峰的邻域，避免同一命中重复入选。
            y0 = max(0, ly - th // 2)
            y1 = min(work.shape[0], ly + th - th // 2 + 1)
            x0 = max(0, lx - tw // 2)
            x1 = min(work.shape[1], lx + tw - tw // 2 + 1)
            work[y0:y1, x0:x1] = -1.0
        return out

    @staticmethod
    def _prepare_template(
        template: np.ndarray | None, template_path: str | Path | None
    ) -> np.ndarray:
        """归一化模板为灰度 ndarray；来源缺失/非法在构造期抛错。"""
        if template is None and template_path is None:
            raise ValueError("必须提供 template 数组或 template_path")
        if template is not None:
            arr = np.asarray(template)
            if arr.ndim == 3:
                arr = to_gray(arr)
            elif arr.ndim != 2:
                raise ValueError(f"template 维度必须为 2（灰度）或 3（RGB），收到 {arr.shape}")
            if arr.shape[0] < 1 or arr.shape[1] < 1:
                raise ValueError("template 尺寸必须为正")
            return np.ascontiguousarray(arr.astype(np.uint8))
        assert template_path is not None
        path = Path(template_path)
        if not path.is_file():
            raise ValueError(f"template_path 不存在：{path}")
        # np.fromfile+imdecode：cv2.imread 不支持非 ASCII 路径（如中文用户目录）。
        data = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"template_path 无法读取：{path}")
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
