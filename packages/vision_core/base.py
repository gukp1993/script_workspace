"""视觉检测器统一契约（VIS-001）。

本模块是 E06 视觉检测与调试的底座，只定义数据与协议，不做任何 OpenCV
运算或文件 IO：

- :class:`DetectorResult`：所有检测器的统一输出（值、置信度、框、耗时、
  版本、错误状态），下游 FSM / 回放 / 轨迹只依赖该结构；
- :class:`Detector`：检测器协议。实现类接受 ``domain_model.Detector``
  配置（或等价字段）构造，``detect`` 接受一帧与可选的归一化 ROI 覆盖；
- :class:`PerceptionBuilder`：把一批 :class:`DetectorResult` 聚合为
  :class:`domain_model.PerceptionSnapshot`（字段名 = 检测器输出语义名）。

约定：

- ROI 统一为**归一化** ``[x, y, w, h]``（与 ``domain_model.Detector.roi``
  一致），各分量 0~1、``x+w<=1``、``y+h<=1``；
- ``bbox`` 统一为**帧像素坐标系**的 ``(x, y, w, h)`` 整数元组；
- ``confidence`` 统一在 ``[0, 1]``；``value`` 类型由检测器语义决定
  （比例/数值/文本），异常输出为 ``None`` 并置 ``error``；
- ``present`` 是"单帧是否命中"；含稳定帧判定的语义由调用方通过
  :mod:`vision_core.stability` 聚合，检测器本身不做稳定帧判定；
- 检测器**不得**抛出异常打断主循环：所有可预期失败（资产缺失、ROI 无效、
  引擎不可用等）都以 ``error`` 字段返回，``present=False``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable

from capture_api.frames import Frame
from domain_model.models import FieldObservation, PerceptionSnapshot

__all__ = [
    "DetectorResult",
    "Detector",
    "PerceptionBuilder",
    "clamp01",
    "result_error",
]

#: 归一化 ROI 类型：[x, y, w, h]，全部分量 0~1。
NormRoi = tuple[float, float, float, float]
#: 像素 bbox 类型：(x, y, w, h)，帧坐标系。
PixelBbox = tuple[int, int, int, int]


def clamp01(value: float) -> float:
    """夹取到 [0, 1]（NaN 一律归 0，保证结果可序列化、可比较）。"""
    v = float(value)
    if v != v:  # NaN 判定（不用 math.isnan 以外的魔法，这里直白处理）
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


@dataclass(frozen=True, slots=True)
class DetectorResult:
    """一次检测的统一输出（VIS-001 契约）。

    Attributes:
        detector_id: 检测器 ID（与配置中的 ``detector_id`` 一致）。
        present:     本帧是否命中。含稳定帧判定的语义由调用方聚合。
        confidence:  置信度 0~1（命中强度/匹配分数/结构确信度，按检测器语义）。
        value:       数值型检测器输出（比例、数值或文本）；无输出为 ``None``。
        bbox:        命中框，帧像素坐标系 ``(x, y, w, h)``；无框为 ``None``。
        elapsed_ms:  本次检测实际耗时（毫秒，用于调度预算与性能指标）。
        version:     检测器实现版本号（轨迹与调试叠加展示用）。
        error:       可预期失败的结构化原因（短标识符）；成功为 ``None``。
    """

    detector_id: str
    present: bool
    confidence: float
    value: float | str | None
    bbox: PixelBbox | None
    elapsed_ms: float
    version: str
    error: str | None = None


def result_error(
    detector_id: str, error: str, elapsed_ms: float, version: str
) -> DetectorResult:
    """构造统一的错误结果：present=False、置信度 0、无值无框。"""
    return DetectorResult(
        detector_id=detector_id,
        present=False,
        confidence=0.0,
        value=None,
        bbox=None,
        elapsed_ms=float(elapsed_ms),
        version=version,
        error=error,
    )


@runtime_checkable
class Detector(Protocol):
    """检测器协议（VIS-001）。

    实现类约定：

    - 构造接受 ``domain_model.Detector`` 配置（或等价字段对象）+ 算法特有
      参数（模板数组、颜色范围等）；构造期做参数校验，可预期缺失（如模板
      未提供）立即抛 ``ValueError``，运行期失败走 ``error`` 结果；
    - 暴露 ``detector_id``、``field_name``、``version`` 属性；
    - :meth:`detect` 纯读帧，不修改 ``frame.pixels``；无内部状态的检测器
      必须可并发调用，有状态的（如变化检测）必须提供 ``reset()``。
    """

    detector_id: str
    field_name: str
    version: str

    def detect(
        self, frame: Frame, roi: NormRoi | None = None
    ) -> DetectorResult:
        """对一帧执行检测；``roi`` 为归一化 [x,y,w,h]，缺省用配置 ROI。"""
        ...


class PerceptionBuilder:
    """把一批 :class:`DetectorResult` 聚合为 :class:`PerceptionSnapshot`。

    映射规则（下游 FSM 依赖，字段名 = 检测器输出语义名）：

    - ``FieldObservation.name``  = 配置里的 ``field_name``；
    - ``present``                = ``DetectorResult.present``；
    - ``confidence``             = ``DetectorResult.confidence``；
    - ``value``                  = ``DetectorResult.value``。

    带错误的结果同样写入快照（present=False、置信度 0、value=None），
    保证字段始终可观测、可追踪；未在映射中的 detector_id 的结果被忽略。
    """

    def __init__(self, field_names: Mapping[str, str]) -> None:
        """``field_names``：detector_id -> field_name 语义字段映射。"""
        self._field_names: dict[str, str] = dict(field_names)

    @classmethod
    def from_detectors(cls, detectors: Sequence[Detector]) -> "PerceptionBuilder":
        """从带 ``detector_id``/``field_name`` 属性的检测器列表构造。"""
        return cls({d.detector_id: d.field_name for d in detectors})

    @property
    def field_names(self) -> dict[str, str]:
        """只读副本：detector_id -> field_name。"""
        return dict(self._field_names)

    def build(
        self,
        results: Sequence[DetectorResult],
        frame_seq: int,
        ts_monotonic: float,
    ) -> PerceptionSnapshot:
        """聚合一次检测结果为感知快照（只含感知事实，不含任何动作字段）。"""
        values: dict[str, FieldObservation] = {}
        for result in results:
            field = self._field_names.get(result.detector_id)
            if field is None:
                continue
            values[field] = FieldObservation(
                name=field,
                present=bool(result.present),
                confidence=clamp01(result.confidence),
                value=result.value,
            )
        return PerceptionSnapshot(
            frame_seq=int(frame_seq),
            ts_monotonic=float(ts_monotonic),
            values=values,
        )
