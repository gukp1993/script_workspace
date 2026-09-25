"""python-mss 兼容回退采集适配器（CAP-003）。

基于 GDI BitBlt 的整屏采集，作为 DXcam 不可用/故障时的兼容兜底：

- 接口与 ``DxcamAdapter`` 完全一致（同一 ``CaptureSource`` 协议）；
- mss 输出 BGRA，本适配器负责转换为包统一的 RGB；
- ``monitor_index`` 直接沿用 mss 编号：``0``=全部显示器拼合虚拟屏，
  ``1..N``=各独立显示器（默认 0）；
- ``stop()`` 幂等。
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from capture_api.base import AdapterUnavailableError
from capture_api.frames import Frame, FrameMeta, SourceInfo
from common.clock import Clock, MonotonicClock

try:  # 导入层探测：缺失时保持可导入
    import mss as _mss
    import mss.tools as _mss_tools

    MSS_AVAILABLE = True
except Exception:  # pragma: no cover - 仅在无 mss 环境触发
    _mss = None  # type: ignore[assignment]
    _mss_tools = None  # type: ignore[assignment]
    MSS_AVAILABLE = False

__all__ = ["MssAdapter", "MSS_AVAILABLE"]

_ADAPTER = "mss"


class MssAdapter:
    """mss 整屏采集适配器（BGRA → RGB）。

    注意：mss 实例绑定创建线程，``grab()`` 应固定在 ``start()`` 所在线程
    调用（生产者单线程模型，与本包管线约定一致）。
    """

    def __init__(self, monitor_index: int = 0, *, clock: Clock | None = None) -> None:
        if monitor_index < 0:
            raise ValueError("monitor_index 不能为负")
        self._monitor_index = monitor_index
        self._clock: Clock = clock or MonotonicClock()
        self._lock = threading.Lock()
        self._sct: Any = None
        self._started = False
        self._seq = 0
        self._latest: Frame | None = None
        self.grabbed_count = 0
        self.none_count = 0
        self.error_count = 0
        self.last_error: str | None = None
        self.source_info: SourceInfo | None = None

    # -- 生命周期 ------------------------------------------------------------
    def start(self) -> None:
        """创建 mss 会话；不可用抛 ``AdapterUnavailableError``。"""
        with self._lock:
            if self._started and self._sct is not None:
                return
        if not MSS_AVAILABLE or _mss is None:
            raise AdapterUnavailableError("mss 未安装", adapter=_ADAPTER)
        try:
            # mss 10.x 推荐 MSS 类名；旧版回退到模块级工厂 mss.mss()
            factory = getattr(_mss, "MSS", None) or _mss.mss
            sct = factory()
            monitors = sct.monitors
            if self._monitor_index >= len(monitors):
                sct.close()
                raise AdapterUnavailableError(
                    f"monitor_index={self._monitor_index} 超出范围（共 {len(monitors) - 1} 个）",
                    adapter=_ADAPTER,
                )
        except AdapterUnavailableError:
            raise
        except Exception as exc:
            raise AdapterUnavailableError(f"mss 初始化失败: {exc!r}", adapter=_ADAPTER) from exc
        with self._lock:
            self._sct = sct
            self._started = True

    def stop(self) -> None:
        """关闭 mss 会话（幂等）。"""
        with self._lock:
            sct, self._sct, self._started = self._sct, None, False
        if sct is not None:
            try:
                sct.close()
            except Exception as exc:  # 关闭失败不阻断上层
                self.last_error = repr(exc)

    # 语义别名：与 DxcamAdapter 对齐
    release = stop

    # -- 抓帧 ----------------------------------------------------------------
    def grab(self) -> Frame | None:
        """抓一帧并转 RGB；mss 同步抓屏，正常不返回 ``None``。"""
        with self._lock:
            sct = self._sct
        if sct is None:
            raise AdapterUnavailableError("start() 未成功调用", adapter=_ADAPTER)
        try:
            monitor = sct.monitors[self._monitor_index]
            raw = sct.grab(monitor)
        except Exception as exc:
            self.error_count += 1
            self.last_error = repr(exc)
            raise
        if raw is None:  # mss 正常不会走到这里，防御性对齐协议
            self.none_count += 1
            return None
        # mss 原始布局 BGRA（H, W, 4）：取 BGR 三通道反转 -> RGB，并保证内存连续
        bgra = np.asarray(raw, dtype=np.uint8)
        pixels = np.ascontiguousarray(bgra[:, :, 2::-1])
        height, width = pixels.shape[:2]
        left, top = int(monitor["left"]), int(monitor["top"])
        self._seq += 1
        frame = Frame(
            pixels,
            FrameMeta(
                seq=self._seq,
                ts_monotonic=self._clock.now(),
                adapter=_ADAPTER,
                source_width=width,
                source_height=height,
                client_rect=(left, top, left + width, top + height),
                window_title=None,
            ),
        )
        self.grabbed_count += 1
        self._latest = frame
        self.source_info = SourceInfo(
            adapter=_ADAPTER,
            source_width=width,
            source_height=height,
            monitor_index=self._monitor_index,
            client_rect=frame.meta.client_rect,
        )
        return frame

    def latest(self) -> Frame | None:
        return self._latest

    # -- 诊断 ----------------------------------------------------------------
    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            started = self._started
            monitor_index = self._monitor_index
        info = self.source_info
        return {
            "adapter": _ADAPTER,
            "available": MSS_AVAILABLE,
            "started": started,
            "monitor_index": monitor_index,
            "color_format": "RGB(from BGRA)",
            "grabbed": self.grabbed_count,
            "none_count": self.none_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "latest_seq": self._latest.meta.seq if self._latest is not None else None,
            "source_size": (info.source_width, info.source_height) if info else None,
        }
