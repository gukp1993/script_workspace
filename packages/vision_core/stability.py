"""稳定帧、迟滞与置信度聚合（VIS-009）。

:class:`StableFrameAggregator` 把逐帧的原始 :class:`DetectorResult` 聚合为
"语义上稳定"的判定，消除单帧抖动（flap）：

- **稳定帧**：进入"命中"需要连续 ``enter_frames`` 帧原始命中（且置信度
  ≥ 进入阈值）；退出需要连续 ``exit_frames`` 帧原始未命中（且置信度
  < 退出阈值）；
- **迟滞**：进入阈值与退出阈值分开（进入阈值 ≥ 退出阈值）。中间灰区的
  原始结果既不推进进入也不推进退出，维持当前状态（抗抖动关键）；
- **置信度聚合**：输出置信度 = 最近 ``confidence_window`` 个原始置信度
  的均值（窗口滑动，确定性）。

确定性约定：全部状态由帧序驱动（显式 ``update`` 调用），不读任何真实
时钟、不用随机数；同一输入序列两次聚合输出完全一致，可随轨迹回放。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from vision_core.base import DetectorResult

__all__ = ["StabilityConfig", "StableFrameAggregator"]


@dataclass(frozen=True)
class StabilityConfig:
    """稳定聚合配置。

    Attributes:
        enter_frames:      进入"命中"所需连续帧数（>=1）。
        exit_frames:       退出"命中"所需连续帧数（>=1）。
        enter_threshold:   进入阈值：原始置信度 ≥ 该值才推进进入计数。
        exit_threshold:    退出阈值：处于命中态时，原始置信度 < 该值才推进退出计数。
        confidence_window: 输出置信度的滑动均值窗口（>=1）。
    """

    enter_frames: int = 1
    exit_frames: int = 1
    enter_threshold: float = 0.5
    exit_threshold: float = 0.5
    confidence_window: int = 5

    def __post_init__(self) -> None:
        for name in ("enter_frames", "exit_frames", "confidence_window"):
            value = int(getattr(self, name))
            if value < 1:
                raise ValueError(f"{name} 必须 >=1，收到 {value!r}")
            object.__setattr__(self, name, value)
        enter = float(self.enter_threshold)
        exit_ = float(self.exit_threshold)
        if not (0.0 <= exit_ <= enter <= 1.0):
            raise ValueError(
                "必须满足 0 <= exit_threshold <= enter_threshold <= 1，收到 "
                f"enter={self.enter_threshold!r}, exit={self.exit_threshold!r}"
            )
        object.__setattr__(self, "enter_threshold", enter)
        object.__setattr__(self, "exit_threshold", exit_)


class StableFrameAggregator:
    """按帧序驱动的稳定帧/迟滞/置信度聚合器（有状态，提供 ``reset()``）。

    输入为每个检测器每帧的原始 :class:`DetectorResult`（或 ``None`` 表示
    该帧未执行——不推进任何计数，仅保持状态）；输出为聚合后的
    :class:`DetectorResult`（``present`` 为稳定语义，``confidence`` 为窗口
    均值，其余字段透传最近一次原始结果）。
    """

    def __init__(
        self,
        detector_id: str,
        config: StabilityConfig | None = None,
        *,
        enter_frames: int | None = None,
        enter_threshold: float | None = None,
        exit_threshold: float | None = None,
    ) -> None:
        """构造；可传 :class:`StabilityConfig` 或逐项覆盖常用参数。"""
        base = config or StabilityConfig()
        overrides: dict[str, object] = {}
        if enter_frames is not None:
            overrides["enter_frames"] = int(enter_frames)
        if enter_threshold is not None:
            overrides["enter_threshold"] = float(enter_threshold)
        if exit_threshold is not None:
            overrides["exit_threshold"] = float(exit_threshold)
        if overrides:
            base = StabilityConfig(
                enter_frames=int(overrides.get("enter_frames", base.enter_frames)),
                exit_frames=base.exit_frames,
                enter_threshold=float(overrides.get("enter_threshold", base.enter_threshold)),
                exit_threshold=float(overrides.get("exit_threshold", base.exit_threshold)),
                confidence_window=base.confidence_window,
            )
        self._detector_id = str(detector_id)
        self._config = base
        self.reset()

    @property
    def config(self) -> StabilityConfig:
        """聚合配置（只读）。"""
        return self._config

    @property
    def present(self) -> bool:
        """当前稳定状态。"""
        return self._present

    @property
    def enter_streak(self) -> int:
        """当前连续" qualifying 命中"计数（诊断用）。"""
        return self._enter_streak

    @property
    def exit_streak(self) -> int:
        """当前连续"qualifying 未命中"计数（诊断用）。"""
        return self._exit_streak

    def reset(self) -> None:
        """清空全部状态（场景切换/回放起点调用；确定性归零）。"""
        self._present = False
        self._enter_streak = 0
        self._exit_streak = 0
        self._window: deque[float] = deque(maxlen=self._config.confidence_window)
        self._last: DetectorResult | None = None

    def update(self, result: DetectorResult | None) -> DetectorResult:
        """喂入一帧原始结果，返回聚合后的结果。

        ``result=None`` 表示该帧此检测器未执行（如被调度器降频）：不推进
        任何计数、不更新窗口，返回当前状态快照。
        """
        if result is not None:
            conf = min(max(float(result.confidence), 0.0), 1.0)
            self._window.append(conf)
            qualifies_enter = bool(result.present) and conf >= self._config.enter_threshold
            qualifies_exit = (not result.present) or conf < self._config.exit_threshold
            if self._present:
                # 命中态：只关心退出条件；灰区保持现状。
                self._enter_streak = 0
                if qualifies_exit:
                    self._exit_streak += 1
                    if self._exit_streak >= self._config.exit_frames:
                        self._present = False
                        self._exit_streak = 0
                else:
                    self._exit_streak = 0
            else:
                # 未命中态：只关心进入条件；灰区保持现状。
                self._exit_streak = 0
                if qualifies_enter:
                    self._enter_streak += 1
                    if self._enter_streak >= self._config.enter_frames:
                        self._present = True
                        self._enter_streak = 0
                else:
                    self._enter_streak = 0
            self._last = result

        # 输出置信度 = 窗口均值（无任何历史时用最近原始值或 0）。
        if self._window:
            confidence = sum(self._window) / len(self._window)
        elif self._last is not None:
            confidence = float(self._last.confidence)
        else:
            confidence = 0.0
        last = self._last
        return DetectorResult(
            detector_id=last.detector_id if last is not None else self._detector_id,
            present=self._present,
            confidence=min(max(confidence, 0.0), 1.0),
            value=last.value if last is not None else None,
            bbox=last.bbox if last is not None else None,
            elapsed_ms=last.elapsed_ms if last is not None else 0.0,
            version=last.version if last is not None else "aggregator",
            error=last.error if last is not None else None,
        )
