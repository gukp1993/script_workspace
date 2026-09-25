"""采集故障检测与适配器切换（CAP-007 基础版）。

主适配器出现**黑帧 / 尺寸异常 / 抓帧异常**时，先停止输入（不返回坏帧、
不返回旧帧继续决策），再自动切到 fallback 适配器（AC-P0-13 的采集侧）。

切换后 ``needs_reconfirm=True``：恢复真实输入前，上层必须重新确认
目标/标定（本包只暴露标志与切换事件，确认动作由上层执行，完成后调用
``acknowledge_reconfirm()`` 清除标志）。基础版不做主源自动恢复：
确认动作完成后由上层重建采集链或调用 ``reset_baseline()``。
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from capture_api.base import AdapterUnavailableError, CaptureSource
from capture_api.frames import Frame
from common.clock import Clock, MonotonicClock

__all__ = [
    "CaptureChain",
    "REASON_BLACK_FRAME",
    "REASON_GRAB_ERROR",
    "REASON_SIZE_ANOMALY",
    "REASON_START_ERROR",
]

# 切换/故障原因（事件结构化字段）
REASON_BLACK_FRAME = "black_frame"
REASON_SIZE_ANOMALY = "size_anomaly"
REASON_GRAB_ERROR = "grab_error"
REASON_START_ERROR = "start_error"


class CaptureChain:
    """主/备采集链：故障检测 -> 停止输入 -> 切换 fallback。

    实现 ``CaptureSource`` 协议，可直接替代单一适配器接入管线。

    Args:
        primary: 主适配器（如 DXcam）。
        fallback: 备适配器（如 mss）；``None`` 时故障只记录并保持停止输入。
        black_variance_threshold: 黑帧判定之一——整帧方差（int16 计算）
            不超过该值（纯色画面）。
        black_luminance_max: 黑帧判定之二——整帧平均亮度不超过该值。
            两者同时满足才判黑帧，避免把纯色但非黑的健康画面
            （如纯色 UI 背景）误判为黑帧。
    """

    def __init__(
        self,
        primary: CaptureSource,
        fallback: CaptureSource | None = None,
        *,
        black_variance_threshold: float = 1.0,
        black_luminance_max: float = 8.0,
        clock: Clock | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._black_var_max = float(black_variance_threshold)
        self._black_lum_max = float(black_luminance_max)
        self._clock: Clock = clock or MonotonicClock()
        self._lock = threading.Lock()
        self._active: CaptureSource = primary
        # 每个源的尺寸基线独立记录（切到备源后以其自身首帧为基线，
        # 避免把"备源分辨率不同"误判为尺寸异常）
        self._baselines: dict[int, tuple[int, ...]] = {}
        self._events: list[dict[str, Any]] = []
        self.switched = False
        self.needs_reconfirm = False
        self.black_frame_count = 0
        self.size_anomaly_count = 0
        self.error_count = 0
        self.last_error: str | None = None

    # -- 状态 ----------------------------------------------------------------
    @property
    def active(self) -> CaptureSource:
        """当前生效的适配器。"""
        return self._active

    @property
    def events(self) -> list[dict[str, Any]]:
        """切换/故障事件列表（含异常详情，供审计与诊断页展示）。"""
        return list(self._events)

    @property
    def is_switched(self) -> bool:
        """是否已切换到 fallback。"""
        return self.switched

    def acknowledge_reconfirm(self) -> None:
        """上层完成目标/标定重新确认后调用，清除 ``needs_reconfirm``。"""
        self.needs_reconfirm = False

    def reset_baseline(self) -> None:
        """上层重新确认后重置尺寸基线（下次抓帧重新学习）。"""
        self._baselines.clear()

    # -- 生命周期 ------------------------------------------------------------
    def start(self) -> None:
        """启动主源与备源（备源预启动以便即时接管）。"""
        try:
            self._primary.start()
        except AdapterUnavailableError as exc:
            # 启动即不可用：直接以 fallback 运行（同样需要重新确认）
            self._record(
                REASON_START_ERROR,
                source=self._adapter_name(self._primary) or "primary",
                detail=exc.reason,
                switched=self._fallback is not None,
            )
            self.last_error = f"primary start: {exc.reason}"
            if self._fallback is None:
                raise
            self._active = self._fallback
            self.switched = True
            self.needs_reconfirm = True
            self._fallback.start()  # 备源启动失败则无源可用，如实上抛
            return
        if self._fallback is not None:
            try:
                self._fallback.start()
            except AdapterUnavailableError:
                # 备源不可用不阻断主源运行；真正切换时再暴露
                pass

    def stop(self) -> None:
        """停止主备两个源（幂等由各适配器保证）。"""
        for source in (self._primary, self._fallback):
            if source is None:
                continue
            try:
                source.stop()
            except Exception as exc:  # 停止失败记录后继续
                self.last_error = f"stop failed: {exc!r}"

    # -- 抓帧 ----------------------------------------------------------------
    def grab(self) -> Frame | None:
        """从当前适配器抓帧；坏帧/异常先停止输入再切换兜底。

        返回的帧保证不是黑帧、不是尺寸异常帧；异常时不返回旧帧，
        无兜底时保持"停止输入"（返回 None，AC-P0-13）。
        """
        source = self._active
        try:
            frame = source.grab()
        except Exception as exc:
            # 设备级故障：先"停止输入"（停掉故障源），再尝试兜底
            self._safe_stop(source)
            return self._handle_failure(REASON_GRAB_ERROR, source, repr(exc))
        if frame is None:
            return None  # 暂无新帧，如实透传
        # 黑帧检测：全零/方差≈0（int16 计算防 uint8 溢出）
        if self._is_black(frame):
            self.black_frame_count += 1
            return self._handle_failure(
                REASON_BLACK_FRAME, source, f"seq={frame.meta.seq} 方差≈0"
            )
        # 尺寸异常检测：与该源自身基线不符即几何变化，需重新标定
        key = id(source)
        baseline = self._baselines.get(key)
        if baseline is None:
            self._baselines[key] = frame.pixels.shape
        elif frame.pixels.shape != baseline:
            self.size_anomaly_count += 1
            return self._handle_failure(
                REASON_SIZE_ANOMALY,
                source,
                f"shape={frame.pixels.shape} 基线={baseline}",
            )
        return frame

    def latest(self) -> Frame | None:
        return self._active.latest()

    # -- 诊断 ----------------------------------------------------------------
    def diagnostics(self) -> dict[str, Any]:
        """链级诊断：当前适配器、切换标志、事件、各源统计。"""
        return {
            "adapter": self._adapter_name(self._active),
            "primary": self._adapter_name(self._primary),
            "fallback": self._adapter_name(self._fallback),
            "switched": self.switched,
            "needs_reconfirm": self.needs_reconfirm,
            "black_frame_count": self.black_frame_count,
            "size_anomaly_count": self.size_anomaly_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "event_count": len(self._events),
            "events": self._events[-10:],
            "active_diagnostics": self._active.diagnostics(),
        }

    # -- 内部 ----------------------------------------------------------------
    def _is_black(self, frame: Frame) -> bool:
        """黑帧 = 低方差（纯色）且低亮度；int16 计算防 uint8 溢出。"""
        pixels = frame.pixels
        if pixels.size == 0:
            return True
        pixels16 = pixels.astype(np.int16)
        variance = float(pixels16.var())
        if variance > self._black_var_max:
            return False
        return float(pixels16.mean()) <= self._black_lum_max

    def _handle_failure(
        self,
        reason: str,
        source: CaptureSource,
        detail: str,
    ) -> Frame | None:
        """统一故障处理：记录事件 -> 可切则切并立即从备源取帧。"""
        switched = False
        if source is self._primary and self._fallback is not None:
            self._safe_stop(self._primary)  # 先停止输入（AC-P0-13）
            self._active = self._fallback
            switched = True
            self.switched = True
            self.needs_reconfirm = True  # AC-P0-13：回退后需重新确认目标/标定
        self._record(reason, source=self._adapter_name(source), detail=detail, switched=switched)
        if reason == REASON_GRAB_ERROR:
            self.error_count += 1
            self.last_error = detail
        if not switched:
            return None  # 无兜底：保持"停止输入"状态
        try:
            return self._fallback.grab()  # type: ignore[union-attr]
        except Exception as exc:  # 兜底也失败：记录并停止输入
            self.error_count += 1
            self.last_error = f"fallback: {exc!r}"
            self._record(
                REASON_GRAB_ERROR,
                source=self._adapter_name(self._fallback) or "fallback",
                detail=f"fallback 也失败: {exc!r}",
                switched=False,
            )
            return None

    def _record(self, reason: str, *, source: str, detail: str, switched: bool) -> None:
        self._events.append(
            {
                "ts_monotonic": self._clock.now(),
                "reason": reason,
                "source": source,
                "detail": detail,
                "switched": switched,
            }
        )

    def _safe_stop(self, source: CaptureSource) -> None:
        """停止故障源（"先停止输入"），失败仅记录。"""
        try:
            source.stop()
        except Exception as exc:
            self.last_error = f"stop failed: {exc!r}"

    @staticmethod
    def _adapter_name(source: CaptureSource | None) -> str | None:
        if source is None:
            return None
        try:
            return str(source.diagnostics().get("adapter", "unknown"))
        except Exception:
            return "unknown"
