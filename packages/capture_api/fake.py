"""Fake 采集源与合成帧工厂（CAP-001 配套，TST-001 的采集部分）。

供单测与本地演示在无真实桌面/无显示器环境下驱动整条帧管线；
后续由 test_kit re-export。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any

import numpy as np

from capture_api.base import CaptureSource
from capture_api.frames import Frame, FrameMeta, SourceInfo

__all__ = ["FakeCaptureSource", "make_frame"]


def make_frame(
    seq: int = 0,
    width: int = 64,
    height: int = 48,
    color: tuple[int, int, int] = (0, 0, 0),
    ts_monotonic: float = 0.0,
    adapter: str = "fake",
    client_rect: tuple[int, int, int, int] | None = None,
    window_title: str | None = None,
) -> Frame:
    """构造合成帧：整幅填充单一 RGB 颜色（可作黑帧或唯一标记色）。"""
    pixels = np.empty((height, width, 3), dtype=np.uint8)
    pixels[:, :] = np.asarray(color, dtype=np.uint8)
    meta = FrameMeta(
        seq=seq,
        ts_monotonic=ts_monotonic,
        adapter=adapter,
        source_width=width,
        source_height=height,
        client_rect=client_rect,
        window_title=window_title,
    )
    return Frame(pixels, meta)


class FakeCaptureSource:
    """可注入帧序列/生成器的假采集源。

    - 注入 ``frames`` 序列或 ``provider`` 回调（每抓一帧调用一次，可返回 ``None``）。
    - ``inject()`` 运行期追加帧（模拟生产者）。
    - ``fail_on_grab`` 注入异常，模拟设备级故障（采集链切换测试用）。
    - ``grab()`` 按 FIFO 消费注入帧；耗尽返回 ``None``（与真实适配器语义一致）。
    """

    def __init__(
        self,
        frames: Iterable[Frame] | None = None,
        provider: Callable[[], Frame | None] | None = None,
        *,
        fail_on_grab: Exception | None = None,
        name: str = "fake",
    ) -> None:
        self._lock = threading.Lock()
        self._queue: list[Frame] = list(frames or ())
        self._provider = provider
        self._fail_on_grab = fail_on_grab
        self._name = name
        self._latest: Frame | None = None
        self._started = False
        self._stopped = False
        self.grabbed_count = 0
        self.none_count = 0
        self.last_error: str | None = None
        self.source_info = SourceInfo(adapter=name, source_width=0, source_height=0)

    # -- 注入 ----------------------------------------------------------------
    def inject(self, frame: Frame) -> None:
        """运行期追加一帧到待抓队列。"""
        with self._lock:
            self._queue.append(frame)

    def set_fail(self, error: Exception | None) -> None:
        """设置/清除 grab 注入异常（线程安全）。"""
        with self._lock:
            self._fail_on_grab = error

    # -- CaptureSource 协议 --------------------------------------------------
    def start(self) -> None:
        self._started = True
        self._stopped = False

    def stop(self) -> None:
        self._started = False
        self._stopped = True

    def grab(self) -> Frame | None:
        with self._lock:
            error = self._fail_on_grab
            frame = self._queue.pop(0) if self._queue else None
        if error is not None:
            self.last_error = repr(error)
            raise error
        if frame is None and self._provider is not None:
            frame = self._provider()
        if frame is None:
            self.none_count += 1
            return None
        self.grabbed_count += 1
        self._latest = frame
        self.source_info = SourceInfo(
            adapter=self._name,
            source_width=frame.meta.source_width,
            source_height=frame.meta.source_height,
            client_rect=frame.meta.client_rect,
            window_title=frame.meta.window_title,
        )
        return frame

    def latest(self) -> Frame | None:
        return self._latest

    def diagnostics(self) -> dict[str, Any]:
        return {
            "adapter": self._name,
            "started": self._started,
            "stopped": self._stopped,
            "queued": len(self._queue),
            "grabbed": self.grabbed_count,
            "none_count": self.none_count,
            "last_error": self.last_error,
            "source_info": repr(self.source_info),
        }
