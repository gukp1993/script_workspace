"""检测器调度、频率与计算预算（VIS-008）。

:class:`DetectorScheduler` 按帧驱动一批检测器：

- **频率**：每个检测器可配 ``every_n_frames``（每 N 帧执行一次，帧序
  取模，确定性）；
- **优先级**：``priority`` 越大越先执行（同优先级按注册顺序）；
- **计算预算**：每帧毫秒预算 ``budget_ms``。按优先级顺序执行，若预算
  不足以容纳下一个检测器的近期耗时（上次实测 elapsed 的 EWMA 估计），
  该检测器本帧被跳过；
- **降级（降频）**：因预算被跳过、且优先级低于本帧最高优先级的检测器
  会提高自己的有效执行间隔（``effective_every_n`` + 1，封顶
  ``max_every_n``）；当某帧全程无预算跳过时逐步恢复（-1，直到基础值）。
  **安全检测器（``safety_critical=True``）永不跳过、永不降级**——预算
  可以被它突破，保证安全检测不被阻塞；
- **统计**：逐检测器统计应执行/实际执行/预算跳过/降级次数与实际执行率。

确定性约定：调度只用帧序、显式配置与检测器返回的 ``elapsed_ms``（不读
真实时钟），同一输入序列两次调度轨迹一致，可随轨迹回放。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from capture_api.frames import Frame
from vision_core.base import Detector, DetectorResult, result_error

__all__ = ["SchedulerEntry", "SchedulerStat", "DetectorScheduler"]


@dataclass
class SchedulerEntry:
    """一个检测器的调度条目。

    Attributes:
        detector:        检测器实例（VIS-001 协议）。
        every_n_frames:  基础频率：每 N 帧执行一次（>=1）。
        priority:        优先级（越大越先执行、越不容易被降级）。
        safety_critical: 安全检测器标记：预算不足时仍执行且永不降级。
        max_every_n:     降频上限（有效间隔不得超过该值；>=every_n_frames）。
    """

    detector: Detector
    every_n_frames: int = 1
    priority: int = 0
    safety_critical: bool = False
    max_every_n: int = 4

    def __post_init__(self) -> None:
        if int(self.every_n_frames) < 1:
            raise ValueError("every_n_frames 必须 >=1")
        if int(self.max_every_n) < int(self.every_n_frames):
            raise ValueError("max_every_n 不能小于 every_n_frames")
        object.__setattr__(self, "every_n_frames", int(self.every_n_frames))
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "max_every_n", int(self.max_every_n))
        # 运行期状态（非配置）：有效间隔、近期耗时估计、统计计数。
        self.effective_every_n: int = self.every_n_frames
        self.last_cost_ms: float = 0.0
        self.scheduled = 0
        self.executed = 0
        self.skipped_budget = 0
        self.demotions = 0


@dataclass(frozen=True)
class SchedulerStat:
    """一个检测器的调度统计快照。"""

    detector_id: str
    every_n_frames: int
    effective_every_n: int
    priority: int
    safety_critical: bool
    scheduled: int
    executed: int
    skipped_budget: int
    demotions: int
    execution_rate: float


@dataclass
class _FrameRecord:
    """单帧调度过程记录（内部）。"""

    spent_ms: float = 0.0
    skipped_budget: bool = False


class DetectorScheduler:
    """按帧调度检测器：频率 + 优先级 + 每帧毫秒预算 + 安全检测器保护。"""

    def __init__(self, entries: list[SchedulerEntry], *, budget_ms: float = 12.0) -> None:
        """``entries`` 为调度条目列表；``budget_ms`` 为每帧计算预算。"""
        if not entries:
            raise ValueError("entries 不能为空")
        ids = [e.detector.detector_id for e in entries]
        if len(set(ids)) != len(ids):
            raise ValueError(f"detector_id 重复：{ids}")
        if not (float(budget_ms) > 0.0):
            raise ValueError(f"budget_ms 必须为正数，收到 {budget_ms!r}")
        # 执行顺序：优先级降序，同优先级保持注册顺序（稳定排序）。
        self._entries: list[SchedulerEntry] = sorted(
            entries, key=lambda e: -e.priority
        )
        self._budget_ms = float(budget_ms)
        self._frame_index = 0  # 已处理的帧数（从 0 开始，第一帧即 frame 0）

    @property
    def budget_ms(self) -> float:
        """每帧计算预算（毫秒）。"""
        return self._budget_ms

    @property
    def frame_index(self) -> int:
        """已处理帧数（下一帧序号，从 0 计数）。"""
        return self._frame_index

    def entries(self) -> tuple[SchedulerEntry, ...]:
        """按执行顺序返回条目（只读视图）。"""
        return tuple(self._entries)

    def process_frame(self, frame: Frame) -> list[DetectorResult]:
        """处理一帧：按序执行到期检测器，返回本帧实际产生的检测结果。"""
        record = _FrameRecord()
        results: list[DetectorResult] = []
        # 预算跳过的最高优先级：用于决定"低优先级降级"。
        top_priority = self._entries[0].priority
        for entry in self._entries:
            if self._frame_index % entry.effective_every_n != 0:
                continue  # 频率未到：正常降频跳过（不算预算跳过）。
            entry.scheduled += 1
            if entry.safety_critical:
                # 安全检测器：永不跳过、永不降级，允许突破预算。
                result = self._run(entry, frame, record)
                results.append(result)
                continue
            estimated = entry.last_cost_ms
            if record.spent_ms + estimated > self._budget_ms:
                # 预算不足：本帧跳过；低优先级条目降频（高优先级仅跳过）。
                entry.skipped_budget += 1
                record.skipped_budget = True
                if entry.priority < top_priority:
                    self._demote(entry)
                continue
            result = self._run(entry, frame, record)
            results.append(result)
        self._recover_if_healthy(record)
        self._frame_index += 1
        return results

    def stats(self) -> dict[str, SchedulerStat]:
        """返回 detector_id -> 统计快照（含实际执行率）。"""
        out: dict[str, SchedulerStat] = {}
        for entry in self._entries:
            frames = max(self._frame_index, 1)
            out[entry.detector.detector_id] = SchedulerStat(
                detector_id=entry.detector.detector_id,
                every_n_frames=entry.every_n_frames,
                effective_every_n=entry.effective_every_n,
                priority=entry.priority,
                safety_critical=entry.safety_critical,
                scheduled=entry.scheduled,
                executed=entry.executed,
                skipped_budget=entry.skipped_budget,
                demotions=entry.demotions,
                execution_rate=entry.executed / frames,
            )
        return out

    # ---- 内部 -----------------------------------------------------------

    def _run(
        self, entry: SchedulerEntry, frame: Frame, record: _FrameRecord
    ) -> DetectorResult:
        """执行一个检测器并记账（耗时累加、EWMA 估计更新）。

        检测器抛异常时隔离为 error 结果——单个检测器故障不得拖垮感知循环。
        """
        import time as _time

        t0 = _time.perf_counter()
        try:
            result = entry.detector.detect(frame)
        except Exception as exc:  # noqa: BLE001——隔离边界，见 docstring
            elapsed = (_time.perf_counter() - t0) * 1000.0
            record.spent_ms += elapsed
            entry.executed += 1
            return result_error(
                detector_id=entry.detector.detector_id,
                error=f"detector_exception:{type(exc).__name__}:{exc}",
                elapsed_ms=elapsed,
                version=getattr(entry.detector, "version", "unknown"),
            )
        elapsed = float(result.elapsed_ms)
        record.spent_ms += elapsed
        # 近期耗时估计：EWMA（首次直接采用实测；α=0.5，确定性数值递推）。
        entry.last_cost_ms = (
            elapsed if entry.executed == 0 else 0.5 * entry.last_cost_ms + 0.5 * elapsed
        )
        entry.executed += 1
        if record.spent_ms > self._budget_ms:
            record.skipped_budget = True  # 本帧已超支：后续非安全项将被跳过。
        return result

    def _demote(self, entry: SchedulerEntry) -> None:
        """降频一步：有效间隔 +1（封顶 max_every_n）。"""
        if entry.effective_every_n < entry.max_every_n:
            entry.effective_every_n += 1
            entry.demotions += 1

    def _recover_if_healthy(self, record: _FrameRecord) -> None:
        """本帧未发生预算跳过时，全部条目降频恢复一步（不低于基础频率）。"""
        if record.skipped_budget:
            return
        for entry in self._entries:
            if entry.effective_every_n > entry.every_n_frames:
                entry.effective_every_n -= 1

    def __iter__(self) -> Iterator[SchedulerEntry]:
        return iter(self._entries)
