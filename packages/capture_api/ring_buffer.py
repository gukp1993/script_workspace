"""帧环形缓冲（CAP-004）。

- 单生产者 ``put``，多消费者 ``latest``：读写共用一把锁，帧在锁内完成
  拷贝后发布/读取，**消费者永远不会读到半写帧**。
- ``seq`` 单调递增校验：回退帧直接拒绝（ValueError）。
- ``latest(max_age_s, clock)``：过期帧返回 ``None`` 并累加过期计数
  （``expired_count``/``last_expired_seq``），过期可识别（CAP-004 验收）。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from capture_api.frames import Frame
from common.clock import Clock, MonotonicClock

__all__ = ["FrameRingBuffer"]


class FrameRingBuffer:
    """固定容量帧环形缓冲（容量循环覆盖最旧帧）。"""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity 必须为正整数")
        self._capacity = capacity
        self._items: deque[Frame] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._last_seq: int | None = None  # 已入队最大 seq（单调性依据）
        # 统计（诊断/过期识别）
        self.put_count = 0
        self.overwritten = 0  # 因容量循环被覆盖的帧数
        self.expired_count = 0  # latest() 因过期被拒的次数
        self.last_expired_seq: int | None = None  # 最近一次被拒帧的 seq

    # -- 生产者 --------------------------------------------------------------
    def put(self, frame: Frame) -> None:
        """入队一帧（单生产者）。

        - 拒绝 ``seq`` 不大于上一帧的输入（单调性，CAP-004）。
        - 在锁内深拷贝像素后再入队：生产者手中的原帧后续任何改动
          都不会撕裂缓冲内已发布的帧。
        """
        with self._lock:
            if self._last_seq is not None and frame.meta.seq <= self._last_seq:
                raise ValueError(
                    f"seq 必须单调递增: 收到 {frame.meta.seq}, 已有 {self._last_seq}"
                )
            if len(self._items) == self._capacity:
                self.overwritten += 1
            self._items.append(frame.copy())  # 锁内拷贝 => 发布即完整
            self._last_seq = frame.meta.seq
            self.put_count += 1

    # -- 消费者 --------------------------------------------------------------
    def latest(
        self,
        max_age_s: float | None = None,
        clock: Clock | None = None,
    ) -> Frame | None:
        """取最新帧（锁内拷贝返回，消费侧改动互不影响）。

        - ``max_age_s`` 给定时，帧龄（当前单调时钟 - ts_monotonic）超过
          该值视为过期：返回 ``None``，并更新 ``expired_count`` 与
          ``last_expired_seq`` 供调用方识别过期事件。
        - 空缓冲返回 ``None``（不计过期）。
        """
        with self._lock:
            if not self._items:
                return None
            frame = self._items[-1]
            if max_age_s is not None:
                now = (clock or MonotonicClock()).now()
                if now - frame.meta.ts_monotonic > max_age_s:
                    self.expired_count += 1
                    self.last_expired_seq = frame.meta.seq
                    return None
            return frame.copy()  # 锁内拷贝 => 读到的一定是完整帧

    # -- 状态 ----------------------------------------------------------------
    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def last_seq(self) -> int | None:
        return self._last_seq

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            size = len(self._items)
            newest = self._items[-1].meta.seq if self._items else None
        return {
            "capacity": self._capacity,
            "size": size,
            "newest_seq": newest,
            "put_count": self.put_count,
            "overwritten": self.overwritten,
            "expired_count": self.expired_count,
            "last_expired_seq": self.last_expired_seq,
        }
