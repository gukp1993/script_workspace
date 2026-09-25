"""OpenCV 检测器内部公共工具（ROI 几何与像素分类）。

仅实现类内部使用，不属于对外契约：

- ROI 归一化 ``[x, y, w, h]`` -> 像素矩形（裁剪到帧内）与裁剪视图；
- bbox 从 ROI 局部坐标映射回帧坐标系；
- 灰度转换（RGB -> gray，cv2 需 BGR 序，统一在此处理）；
- 基于通道散布（max-min，对加性亮度变化不变）的"彩色填充"像素分类，
  供颜色条/颜色区检测器获得对亮度变化的基线鲁棒性。
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import cv2
import numpy as np

from capture_api.frames import Frame
from vision_core.base import NormRoi

__all__ = [
    "PixelRect",
    "Sequence4",
    "resolve_roi",
    "crop_roi",
    "offset_bbox",
    "to_gray",
    "channel_spread_mask",
    "rgb_to_hsv",
    "hsv_in_ranges",
    "rgb_in_ranges",
    "now_ms",
]

#: 像素矩形 (x0, y0, x1, y1)，半开区间，帧坐标系。
PixelRect = tuple[int, int, int, int]

#: 归一化 ROI 的 list/tuple 兼容别名（配置可能以 list 给出）。
Sequence4 = tuple[float, float, float, float]


def now_ms() -> float:
    """当前单调毫秒（耗时计量用，仅用于 ``elapsed_ms`` 统计）。"""
    return time.perf_counter() * 1000.0


def resolve_roi(
    frame: Frame, roi: NormRoi | None, default: NormRoi | Sequence4 | None = None
) -> PixelRect:
    """归一化 ROI -> 帧内像素矩形（半开，四舍五入后裁剪）。

    ``roi`` 优先；为 ``None`` 时用 ``default``（检测器配置 ROI）；
    两者皆空表示全帧。非法 ROI（负分量/越界/零宽高）抛 ``ValueError``。
    """
    picked = roi if roi is not None else default
    height = frame.height
    width = frame.width
    if picked is None:
        return (0, 0, width, height)
    x, y, w, h = (float(v) for v in picked)
    if x < 0.0 or y < 0.0 or w < 0.0 or h < 0.0:
        raise ValueError(f"ROI 分量不能为负：{picked!r}")
    if w == 0.0 or h == 0.0:
        raise ValueError(f"ROI 宽高必须大于 0：{picked!r}")
    if x + w > 1.0 + 1e-9 or y + h > 1.0 + 1e-9:
        raise ValueError(f"ROI 越界（x+w/y+h 不得超过 1）：{picked!r}")
    x0 = max(0, min(int(round(x * width)), width))
    y0 = max(0, min(int(round(y * height)), height))
    x1 = max(x0, min(int(round((x + w) * width)), width))
    y1 = max(y0, min(int(round((y + h) * height)), height))
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise ValueError(f"ROI 在该分辨率下退化为空：{picked!r} @ {width}x{height}")
    return (x0, y0, x1, y1)


def crop_roi(frame: Frame, rect: PixelRect) -> np.ndarray:
    """取 ROI 内的像素视图（不复制；检测器不得就地修改）。"""
    x0, y0, x1, y1 = rect
    return frame.pixels[y0:y1, x0:x1]


def offset_bbox(
    rect: PixelRect, local: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """把 ROI 内局部 bbox (x, y, w, h) 平移回帧坐标系。"""
    x0, y0, _x1, _y1 = rect
    lx, ly, lw, lh = local
    return (int(x0 + lx), int(y0 + ly), int(lw), int(lh))


def to_gray(rgb: np.ndarray) -> np.ndarray:
    """RGB -> 灰度（uint8；cv2 走 BGR 序）。"""
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def channel_spread_mask(
    rgb: np.ndarray, *, spread_min: float, value_min: float
) -> np.ndarray:
    """彩色填充像素掩码：通道散布 (max-min) ≥ spread_min 且亮度 ≥ value_min。

    通道散布对**加性**亮度变化不变（各通道同加同减，max-min 不变），
    相比绝对亮度/固定饱和度阈值有更好的亮度鲁棒性（VIS-004）。
    """
    mx = rgb.max(axis=2).astype(np.int16)
    mn = rgb.min(axis=2).astype(np.int16)
    spread = mx - mn
    return (spread >= spread_min) & (mx >= value_min)


def rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """RGB -> OpenCV HSV（H 0~179，S/V 0~255，uint8）。"""
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)


def _between(channel: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """单通道落入 [lo, hi] 的布尔掩码。"""
    return (channel >= lo) & (channel <= hi)


def hsv_in_ranges(
    hsv: np.ndarray, ranges: Sequence[tuple[Sequence[float], Sequence[float]]]
) -> np.ndarray:
    """HSV 像素是否落入任一范围（ranges 为 (lo3, hi3) 列表，OR 语义）。"""
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    out = np.zeros(hsv.shape[:2], dtype=bool)
    for lo, hi in ranges:
        out |= (
            _between(h, float(lo[0]), float(hi[0]))
            & _between(s, float(lo[1]), float(hi[1]))
            & _between(v, float(lo[2]), float(hi[2]))
        )
    return out


def rgb_in_ranges(
    rgb: np.ndarray, ranges: Sequence[tuple[Sequence[float], Sequence[float]]]
) -> np.ndarray:
    """RGB 像素是否落入任一范围（ranges 为 (lo3, hi3) 列表，OR 语义）。"""
    out = np.zeros(rgb.shape[:2], dtype=bool)
    for lo, hi in ranges:
        out |= (
            (rgb[:, :, 0] >= lo[0]) & (rgb[:, :, 0] <= hi[0])
            & (rgb[:, :, 1] >= lo[1]) & (rgb[:, :, 1] <= hi[1])
            & (rgb[:, :, 2] >= lo[2]) & (rgb[:, :, 2] <= hi[2])
        )
    return out
