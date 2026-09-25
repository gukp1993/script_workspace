"""帧数据结构（CAP-001）。

统一像素格式、尺寸、时间戳、目标信息与帧序号：

- 像素一律为 ``(H, W, 3)`` 的 ``uint8`` **RGB** 数组（适配器负责转换）。
- 时间戳一律为单调时钟秒（``time.perf_counter`` 体系，见 ``common.clock``）。
- ``seq`` 由采集源头单调递增，是全链路帧序的唯一依据。

``client_rect`` 统一采用 ``(left, top, right, bottom)`` 语义；无窗口概念
的整屏采集器可为 ``None``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

Rect = tuple[int, int, int, int]  # (left, top, right, bottom)


@dataclass(frozen=True, slots=True)
class FrameMeta:
    """一帧的描述信息（不含像素）。

    Attributes:
        seq: 帧序号，源头内单调递增。
        ts_monotonic: 抓取时刻的单调时钟秒。
        adapter: 采集适配器名（如 ``"dxcam"`` / ``"mss"`` / ``"fake"``）。
        source_width/source_height: 源画面尺寸（裁剪/缩放前的整幅）。
        client_rect: 目标客户区/采集区几何 ``(left, top, right, bottom)``；
            整屏采集且无窗口信息时可为 ``None``。
        window_title: 采集时记录的目标窗口标题（可得时），否则 ``None``。
    """

    seq: int
    ts_monotonic: float
    adapter: str
    source_width: int
    source_height: int
    client_rect: Rect | None = None
    window_title: str | None = None


@dataclass(eq=False, slots=True)
class Frame:
    """一帧画面：像素 + 元信息。

    ``pixels`` 约束为 ``(H, W, 3)`` 的 ``uint8`` RGB；构造时不强制校验
    （热路径零开销），但本包提供的工厂与适配器都保证该格式。
    ``eq=False``：ndarray 不支持朴素相等比较，需要比较时用 ``checksum``。
    """

    pixels: np.ndarray  # (H, W, 3) uint8 RGB
    meta: FrameMeta

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])

    def copy(self) -> Frame:
        """深拷贝（像素复制，元信息不可变共享）。环形缓冲/队列据此保证隔离。"""
        return Frame(self.pixels.copy(), self.meta)

    def checksum(self) -> int:
        """像素校验和：用于并发测试中识别"半写帧"与内容比对。"""
        return int(self.pixels.sum(dtype=np.int64))

    def is_uniform(self) -> bool:
        """画面是否单一颜色（方差≈0），供黑帧/纯色检测复用。"""
        return bool(
            self.pixels.size == 0 or np.all(self.pixels == self.pixels[0, 0])
        )


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """采集源的静态几何描述（窗口/显示器几何，CAP-008 诊断页消费）。

    Attributes:
        adapter: 适配器名。
        source_width/source_height: 源画面尺寸。
        monitor_index: 显示器索引（整屏采集器使用，无则 ``None``）。
        client_rect: 采集区几何 ``(left, top, right, bottom)``，可得时。
        window_title: 目标窗口标题，可得时。
        extra: 适配器自定义的附加几何/格式信息（色彩格式、DPI 等）。
    """

    adapter: str
    source_width: int
    source_height: int
    monitor_index: int | None = None
    client_rect: Rect | None = None
    window_title: str | None = None
    extra: dict[str, object] = field(default_factory=dict)
