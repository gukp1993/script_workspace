"""DXcam 主采集适配器（CAP-002）。

基于桌面复制 API 的高帧率整屏/区域采集：

- ``grab()`` 返回 ``None`` 表示"暂无新帧"（DXcam 首帧常见），不是错误；
- 创建失败（无显示器/驱动不可用/region 非法）抛 ``AdapterUnavailableError``；
- ``release()`` 幂等，可安全重复调用；
- 本模块在导入层探测 dxcam 可用性，非 Windows/未安装时仅置标志，
  不影响包内其他模块导入。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

from capture_api.base import AdapterUnavailableError
from capture_api.frames import Frame, FrameMeta, Rect
from common.clock import Clock, MonotonicClock

try:  # 导入层探测：缺失/非 Windows 时保持可导入
    import dxcam as _dxcam

    DXCAM_AVAILABLE = True
except Exception:  # pragma: no cover - 仅在无 dxcam 环境触发
    _dxcam = None  # type: ignore[assignment]
    DXCAM_AVAILABLE = False

__all__ = ["DxcamAdapter", "DXCAM_AVAILABLE"]

_ADAPTER = "dxcam"


class DxcamAdapter:
    """DXcam 采集适配器（输出 RGB）。

    Attributes:
        region: 采集区域 ``(left, top, right, bottom)``（屏幕像素坐标），
            ``None`` 表示主输出全屏。
        max_grab_retries: 冒烟场景下"最多重试几次拿首帧"的建议值；
            适配器本身只如实返回 ``None``，重试策略归上层。

    注意：DXcam 实例非线程安全，``grab()`` 应固定在 ``start()`` 所在线程调用。
    """

    def __init__(
        self,
        region: Rect | None = None,
        *,
        device_id: int = 0,
        output_idx: int = 0,
        max_grab_retries: int = 10,
        clock: Clock | None = None,
    ) -> None:
        self._region = region
        self._device_id = device_id
        self._output_idx = output_idx
        self.max_grab_retries = max_grab_retries
        self._clock: Clock = clock or MonotonicClock()
        self._lock = threading.Lock()
        self._camera: Any = None
        self._started = False
        self._seq = 0
        self._latest: Frame | None = None
        self.grabbed_count = 0
        self.none_count = 0
        self.error_count = 0
        self.last_error: str | None = None

    # -- 生命周期 ------------------------------------------------------------
    def start(self) -> None:
        """创建 DXcam 相机；不可用抛 ``AdapterUnavailableError``。"""
        with self._lock:
            if self._started and self._camera is not None:
                return
        if not DXCAM_AVAILABLE or _dxcam is None:
            raise AdapterUnavailableError("dxcam 未安装或当前平台不支持", adapter=_ADAPTER)
        try:
            camera = _dxcam.create(
                device_idx=self._device_id,
                output_idx=self._output_idx,
                output_color="RGB",
                region=self._region,
                max_buffer_len=8,
            )
        except Exception as exc:  # 创建阶段异常统一归为"不可用"
            raise AdapterUnavailableError(f"dxcam.create 失败: {exc!r}", adapter=_ADAPTER) from exc
        if camera is None:
            raise AdapterUnavailableError(
                "dxcam.create 返回 None（设备被占用或无可用输出）", adapter=_ADAPTER
            )
        with self._lock:
            self._camera = camera
            self._started = True

    def stop(self) -> None:
        """释放相机（幂等）。"""
        with self._lock:
            camera, self._camera, self._started = self._camera, None, False
        if camera is not None:
            try:
                camera.release()
            except Exception as exc:  # 释放失败不阻断上层
                self.last_error = repr(exc)

    # 语义别名：任务书要求 release() 幂等
    release = stop

    # -- 抓帧 ----------------------------------------------------------------
    def grab(self) -> Frame | None:
        """抓一帧 RGB；DXcam 返回 ``None`` 时如实返回 ``None``（暂无新帧）。"""
        with self._lock:
            camera = self._camera
        if camera is None:
            raise AdapterUnavailableError("start() 未成功调用", adapter=_ADAPTER)
        try:
            raw = camera.grab()
        except Exception as exc:  # 设备级故障交给上层（采集链）处理
            self.error_count += 1
            self.last_error = repr(exc)
            raise
        if raw is None:
            self.none_count += 1
            return None
        pixels = np.ascontiguousarray(np.asarray(raw, dtype=np.uint8)[:, :, :3])
        height, width = pixels.shape[:2]
        self._seq += 1
        left, top = (self._region[0], self._region[1]) if self._region else (0, 0)
        client_rect: Rect | None = (
            (left, top, left + width, top + height) if self._region else None
        )
        frame = Frame(
            pixels,
            FrameMeta(
                seq=self._seq,
                ts_monotonic=self._clock.now(),
                adapter=_ADAPTER,
                source_width=width,
                source_height=height,
                client_rect=client_rect,
                window_title=None,  # 整屏采集无窗口归属
            ),
        )
        self.grabbed_count += 1
        self._latest = frame
        return frame

    def latest(self) -> Frame | None:
        return self._latest

    # -- 诊断 ----------------------------------------------------------------
    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            started = self._started
            region = self._region
        return {
            "adapter": _ADAPTER,
            "available": DXCAM_AVAILABLE,
            "started": started,
            "region": region,
            "color_format": "RGB",
            "grabbed": self.grabbed_count,
            "none_count": self.none_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "latest_seq": self._latest.meta.seq if self._latest is not None else None,
            "ts_monotonic": time.perf_counter(),
        }
