"""帧管线：ROI 裁剪、缩放、色彩转换与背压订阅者模型（CAP-005/006）。

- ROI 采用**归一化坐标** ``[x, y, w, h]``（各分量 0..1，相对整幅帧），
  与 TGT-005 坐标体系一致；越界默认严格报错，也可配置安全裁剪（CAP-009）。
- 订阅者模型：每个订阅者一条**独立有界队列**（默认 4），慢消费者触发
  丢最旧帧并独立计数（``dropped``），生产者永不被阻塞（CAP-006 背压）。
- ``diagnostics()``：滑动窗口 fps、latest 延迟、各订阅者 dropped、
  适配器名、最后错误（CAP-008 诊断页消费）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from capture_api.base import CaptureSource
from capture_api.frames import Frame
from common.clock import Clock, MonotonicClock

__all__ = [
    "FramePipeline",
    "SubscriberStats",
    "convert_bgr_rgb",
    "resize",
    "roi_crop",
]

# ---------------------------------------------------------------------------
# 纯函数：ROI / 缩放 / 色彩转换（CAP-005）
# ---------------------------------------------------------------------------

# on_out_of_bounds 行为："strict" 越界即 ValueError；"clip" 安全裁剪
RoiMode = str  # "strict" | "clip"


def roi_crop(
    frame: Frame,
    roi: tuple[float, float, float, float],
    *,
    on_out_of_bounds: RoiMode = "strict",
) -> Frame:
    """按归一化 ROI ``[x, y, w, h]``（0..1）裁剪帧，返回新 Frame。

    Args:
        frame: 输入帧（RGB）。
        roi: 归一化坐标 ``[x, y, w, h]``，各分量必须在 ``[0, 1]``。
        on_out_of_bounds: ``"strict"``（默认）空 ROI 或越界直接 ``ValueError``；
            ``"clip"`` 安全裁剪到画面内（裁剪后为空仍报错）。

    越界定义（归一化转像素后）：左/上为负，或右/下边界超出画面宽高。
    """
    if len(roi) != 4:
        raise ValueError(f"ROI 必须是 [x, y, w, h] 四元组，收到: {roi!r}")
    rx, ry, rw, rh = (float(v) for v in roi)
    for name, value in (("x", rx), ("y", ry), ("w", rw), ("h", rh)):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"ROI 分量 {name}={value} 超出归一化范围 [0, 1]")
    if rw <= 0.0 or rh <= 0.0:
        raise ValueError(f"ROI 宽高必须为正: w={rw}, h={rh}")

    height, width = frame.pixels.shape[:2]
    left = int(round(rx * width))
    top = int(round(ry * height))
    right = int(round((rx + rw) * width))
    bottom = int(round((ry + rh) * height))

    if on_out_of_bounds == "strict":
        if left < 0 or top < 0 or right > width or bottom > height:
            raise ValueError(
                f"ROI 越界: 像素区域 ({left}, {top}, {right}, {bottom})"
                f" 超出画面 {width}x{height}"
            )
    elif on_out_of_bounds != "clip":
        raise ValueError(
            f"on_out_of_bounds 仅支持 'strict'/'clip'，收到: {on_out_of_bounds!r}"
        )

    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    if right - left <= 0 or bottom - top <= 0:
        raise ValueError(f"ROI 裁剪后为空: ({left}, {top}, {right}, {bottom})")

    # seq/时间戳沿用；source_* 仍记录裁剪前的源尺寸（meta 原样共享）
    return Frame(np.ascontiguousarray(frame.pixels[top:bottom, left:right]), frame.meta)


def resize(pixels: np.ndarray, width: int, height: int) -> np.ndarray:
    """cv2 缩放到指定像素尺寸（缩小用 INTER_AREA，放大用 INTER_LINEAR）。"""
    if width <= 0 or height <= 0:
        raise ValueError(f"目标尺寸必须为正: {width}x{height}")
    if pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError(f"resize 仅接受 (H, W, 3) 像素，收到 shape={pixels.shape}")
    if (height, width) == pixels.shape[:2]:
        return pixels
    interpolation = cv2.INTER_AREA if height < pixels.shape[0] else cv2.INTER_LINEAR
    return np.ascontiguousarray(cv2.resize(pixels, (width, height), interpolation=interpolation))


def convert_bgr_rgb(pixels: np.ndarray) -> np.ndarray:
    """BGR ↔ RGB 互换（cv2 通道翻转可逆，双向共用同一函数）。"""
    if pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError(f"convert_bgr_rgb 仅接受 (H, W, 3) 像素，收到 shape={pixels.shape}")
    return np.ascontiguousarray(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB))


# ---------------------------------------------------------------------------
# FramePipeline：订阅者队列 + 背压丢帧（CAP-006）
# ---------------------------------------------------------------------------

SubscriberCallback = Callable[[Frame], None]


@dataclass
class SubscriberStats:
    """单个订阅者的队列、回调与运行统计（CAP-006 丢帧隔离观测）。"""

    name: str
    queue_size: int
    callback: SubscriberCallback
    queued: int = 0  # 快照字段：diagnostics 读取时刷新
    processed: int = 0
    dropped: int = 0
    error_count: int = 0
    last_error: str | None = None
    last_processed_seq: int | None = None
    _queue: deque[Frame] = field(default_factory=deque, repr=False)
    _cond: threading.Condition = field(default_factory=threading.Condition, repr=False)
    _closed: bool = field(default=False, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)


def _stats_snapshot(stats: SubscriberStats) -> dict[str, Any]:
    """导出订阅者统计的 JSON 可序列化快照。"""
    return {
        "queue_size": stats.queue_size,
        "queued": len(stats._queue),
        "processed": stats.processed,
        "dropped": stats.dropped,
        "error_count": stats.error_count,
        "last_error": stats.last_error,
        "last_processed_seq": stats.last_processed_seq,
    }


class FramePipeline:
    """采集 -> 订阅者分发管线（生产者不阻塞，CAP-006）。

    用法::

        pipe = FramePipeline(source)
        pipe.subscribe("fast", fast_cb)
        pipe.subscribe("slow", slow_cb, queue_size=2)
        pipe.start()
        pipe.run(30)               # 或逐帧 pump_once()
        pipe.diagnostics()         # fps / dropped / 延迟
    """

    def __init__(
        self,
        source: CaptureSource,
        *,
        queue_size: int = 4,
        fps_window_s: float = 5.0,
        clock: Clock | None = None,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size 必须为正整数")
        self._source = source
        self._default_queue_size = queue_size
        self._fps_window_s = max(0.001, fps_window_s)
        self._clock: Clock = clock or MonotonicClock()
        self._subs: dict[str, SubscriberStats] = {}
        self._subs_lock = threading.Lock()
        self._fps_ticks: deque[float] = deque()
        self._latest: Frame | None = None
        self.pumped = 0
        self.none_count = 0
        self.last_source_error: str | None = None
        self._running = False

    # -- 订阅 ----------------------------------------------------------------
    def subscribe(
        self,
        name: str,
        callback: SubscriberCallback,
        queue_size: int | None = None,
    ) -> None:
        """注册订阅者：独立有界队列 + 独立消费线程（慢者只丢自己的帧）。"""
        if queue_size is not None and queue_size <= 0:
            raise ValueError("queue_size 必须为正整数")
        with self._subs_lock:
            if name in self._subs:
                raise ValueError(f"订阅者重名: {name}")
            self._subs[name] = SubscriberStats(
                name=name,
                queue_size=queue_size or self._default_queue_size,
                callback=callback,
            )

    def unsubscribe(self, name: str) -> None:
        """移除订阅者并停止其消费线程。"""
        with self._subs_lock:
            stats = self._subs.pop(name, None)
        if stats is not None:
            self._close_sub(stats)

    # -- 生命周期 ------------------------------------------------------------
    def start(self) -> None:
        """启动源与各订阅者消费线程（幂等）。"""
        if self._running:
            return
        self._source.start()
        with self._subs_lock:
            subs = list(self._subs.values())
        for stats in subs:
            self._start_sub(stats)
        self._running = True

    def stop(self) -> None:
        """停止各订阅者线程与源（幂等）。"""
        self._running = False
        with self._subs_lock:
            subs = list(self._subs.values())
        for stats in subs:
            self._close_sub(stats)
        try:
            self._source.stop()
        except Exception:  # 源停止失败不阻断上层
            pass

    # -- 生产 ----------------------------------------------------------------
    def pump_once(self) -> Frame | None:
        """从源抓一帧并分发给全部订阅者（**永不阻塞**，CAP-006）。

        队列满的订阅者丢最旧帧腾位并累加自身 ``dropped``；
        源返回 None 记数并透传 None；源抛异常记录后原样上抛。
        """
        try:
            frame = self._source.grab()
        except Exception as exc:
            self.last_source_error = repr(exc)
            raise
        if frame is None:
            self.none_count += 1
            return None
        now = self._clock.now()
        self._latest = frame
        self.pumped += 1
        self._fps_ticks.append(now)
        self._trim_fps_window(now)
        with self._subs_lock:
            subs = list(self._subs.values())
        for stats in subs:
            self._offer(stats, frame)
        return frame

    def run(self, frames: int, interval_s: float = 0.0) -> int:
        """连续泵 ``frames`` 次，返回实际拿到的帧数（None 不计）。

        ``interval_s`` 为帧间隔（模拟真实采集节奏，测试用小间隔给
        快订阅者留出排空时间；慢订阅者依旧积压丢帧）。
        """
        got = 0
        for _ in range(max(0, frames)):
            if self.pump_once() is not None:
                got += 1
            if interval_s > 0.0:
                time.sleep(interval_s)
        return got

    # -- 诊断 ----------------------------------------------------------------
    def latest(self) -> Frame | None:
        return self._latest

    def subscriber_stats(self, name: str) -> SubscriberStats:
        """按名取订阅者统计对象（测试/诊断用）。"""
        with self._subs_lock:
            return self._subs[name]

    def diagnostics(self) -> dict[str, Any]:
        """fps（滑动窗口）、latest 延迟、各订阅者 dropped、适配器名、最后错误。"""
        now = self._clock.now()
        self._trim_fps_window(now)
        fps = len(self._fps_ticks) / self._fps_window_s
        latest = self._latest
        latency = None if latest is None else max(0.0, now - latest.meta.ts_monotonic)
        with self._subs_lock:
            subs = {name: _stats_snapshot(s) for name, s in self._subs.items()}
        latest_meta = latest.meta if latest is not None else None
        return {
            "adapter": latest_meta.adapter if latest_meta else None,
            "fps": fps,
            "fps_window_s": self._fps_window_s,
            "pumped": self.pumped,
            "none_count": self.none_count,
            "latest_seq": latest_meta.seq if latest_meta else None,
            "latest_latency_s": latency,
            "subscribers": subs,
            "last_error": self.last_source_error,
            "running": self._running,
        }

    # -- 内部 ----------------------------------------------------------------
    def _offer(self, stats: SubscriberStats, frame: Frame) -> None:
        """有界队列入队：满则丢最旧（背压），生产者绝不等待。"""
        with stats._cond:
            if len(stats._queue) >= stats.queue_size:
                stats._queue.popleft()
                stats.dropped += 1
            stats._queue.append(frame.copy())  # 每个订阅者独立拷贝，互不撕裂
            stats._cond.notify()

    def _start_sub(self, stats: SubscriberStats) -> None:
        if stats._thread is not None and stats._thread.is_alive():
            return
        stats._closed = False
        stats._thread = threading.Thread(
            target=self._sub_worker,
            args=(stats,),
            name=f"capture-pipeline-{stats.name}",
            daemon=True,
        )
        stats._thread.start()

    def _sub_worker(self, stats: SubscriberStats) -> None:
        """订阅者消费线程：取帧 -> 回调；回调异常只计数不退出。"""
        while True:
            with stats._cond:
                while not stats._queue and not stats._closed:
                    stats._cond.wait(timeout=0.05)
                if not stats._queue and stats._closed:
                    return
                frame = stats._queue.popleft()
            try:
                stats.callback(frame)
                stats.processed += 1
                stats.last_processed_seq = frame.meta.seq
            except Exception as exc:  # 隔离故障：仅记录，继续消费
                stats.error_count += 1
                stats.last_error = repr(exc)

    def _close_sub(self, stats: SubscriberStats) -> None:
        with stats._cond:
            stats._closed = True
            stats._cond.notify_all()
        thread = stats._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        stats._thread = None

    def _trim_fps_window(self, now: float) -> None:
        cutoff = now - self._fps_window_s
        while self._fps_ticks and self._fps_ticks[0] < cutoff:
            self._fps_ticks.popleft()
