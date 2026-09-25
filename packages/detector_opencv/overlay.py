"""调试叠加数据层（VIS-010 数据部分）。

只定义供 UI 渲染（实时预览叠加）消费的**纯数据结构**，不做任何绘制：

- :class:`OverlayItem`：一个检测结果的叠加条目（标签、框、置信度、
  耗时、版本、错误）；
- :class:`OverlayData`：一帧的全部叠加条目 + 帧序/时间戳；
  :meth:`OverlayData.from_results` 从一批 :class:`DetectorResult` 构造。

UI（workbench_ui）拿到该结构后自行决定显示/隐藏与绘制方式——数据层
与渲染层解耦，保证叠加开关不影响检测主循环。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from vision_core.base import DetectorResult

__all__ = ["OverlayItem", "OverlayData"]


@dataclass(frozen=True, slots=True)
class OverlayItem:
    """一个检测结果的叠加条目。

    Attributes:
        label:      显示标签（检测器 ID；有语义字段名时可替换）。
        bbox:       框 (x, y, w, h)，帧像素坐标；无框为 ``None``。
        present:    是否命中（决定 UI 用高亮/灰显样式）。
        confidence: 置信度 0~1。
        elapsed_ms: 本次检测耗时（毫秒）。
        version:    检测器实现版本。
        error:      错误标识（非空时 UI 可显示警示样式）。
        value:      检测输出值（文本/比例，悬停详情展示）。
    """

    label: str
    bbox: tuple[int, int, int, int] | None
    present: bool
    confidence: float
    elapsed_ms: float
    version: str
    error: str | None = None
    value: float | str | None = None


@dataclass(frozen=True, slots=True)
class OverlayData:
    """一帧的调试叠加数据（纯数据，可 JSON 序列化）。"""

    frame_seq: int
    ts_monotonic: float
    items: tuple[OverlayItem, ...] = field(default=())

    @classmethod
    def from_results(
        cls,
        results: Sequence[DetectorResult],
        frame_seq: int,
        ts_monotonic: float,
    ) -> "OverlayData":
        """从一批检测结果构造叠加数据（顺序与输入一致）。"""
        items = tuple(
            OverlayItem(
                label=r.detector_id,
                bbox=r.bbox,
                present=r.present,
                confidence=r.confidence,
                elapsed_ms=r.elapsed_ms,
                version=r.version,
                error=r.error,
                value=r.value,
            )
            for r in results
        )
        return cls(frame_seq=int(frame_seq), ts_monotonic=float(ts_monotonic), items=items)

    def to_json_dict(self) -> dict[str, object]:
        """导出为可 JSON 序列化的 dict（轨迹/UI 通道传输用）。"""
        return {
            "frame_seq": self.frame_seq,
            "ts_monotonic": self.ts_monotonic,
            "items": [
                {
                    "label": item.label,
                    "bbox": list(item.bbox) if item.bbox else None,
                    "present": item.present,
                    "confidence": item.confidence,
                    "elapsed_ms": item.elapsed_ms,
                    "version": item.version,
                    "error": item.error,
                    "value": item.value,
                }
                for item in self.items
            ],
        }
