"""截图隐私遮罩（SEC-005，TM-06）。

设计目标：**遮罩不可被绕过**。

- :class:`PrivacyMask`：固定遮罩区（归一化 ROI），``apply`` 返回**新数组**
  并在返回前断言"填充区域内统计恒为填充色"；原数组不被修改；
- :class:`MaskedPixels`：已遮罩像素的类型化包装。内部数组被置为只读，
  防止拿到手后改回原文；:meth:`MaskedPixels.verify_fill` 可随时复核
  填充区域仍为填充色；
- :meth:`PrivacyMask.thumbnail_safe`：**先遮罩、再缩略、再遮罩**的组合
  函数——封死"只缩不放遮罩"（对未遮罩原图直接缩略导出）与"缩略时边界
  混色渗漏"（缩放插值会把遮罩边缘与内部像素混合）两条绕过路径；
- :func:`export_png`：只接受 :class:`MaskedPixels`；普通 ndarray 必须先
  经 :func:`assume_masked` 显式声明才能导出——**代码级强制**，类型不对
  直接 TypeError，导出侧不可能拿到未遮罩原图。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image

from capture_api.frames import Frame
from security_kit.errors import SecurityKitError

__all__ = [
    "ROI",
    "FillColor",
    "MaskedPixels",
    "PrivacyMask",
    "assume_masked",
    "export_png",
]

#: 归一化 ROI：(x, y, w, h)，取值 0..1，左上角原点。
ROI = tuple[float, float, float, float]

#: 填充色：(R, G, B)。
FillColor = tuple[int, int, int]

#: 默认填充色：纯黑。
DEFAULT_FILL: FillColor = (0, 0, 0)

#: 像素数组类型卫语句的错误消息前缀。
_PIXELS_HINT = "pixels 必须是 (H, W, 3) 的 uint8 numpy 数组"


def _validate_pixels(pixels: np.ndarray) -> np.ndarray:
    """校验像素数组形状/类型；返回原数组（不拷贝）。"""
    if not isinstance(pixels, np.ndarray):
        raise TypeError(f"{_PIXELS_HINT}，得到 {type(pixels).__name__}")
    if pixels.ndim != 3 or pixels.shape[2] != 3 or pixels.dtype != np.uint8:
        raise ValueError(f"{_PIXELS_HINT}，得到 shape={pixels.shape} dtype={pixels.dtype}")
    return pixels


def _clamp_roi(roi: ROI) -> ROI:
    """校验并夹取归一化 ROI（越界部分裁掉；空 ROI 拒绝）。"""
    x, y, w, h = (float(v) for v in roi)
    if not all(np.isfinite(v) for v in (x, y, w, h)):
        raise ValueError(f"ROI 含非有限数值：{roi!r}")
    if w <= 0 or h <= 0:
        raise ValueError(f"ROI 宽高必须为正：{roi!r}")
    x, y = max(0.0, x), max(0.0, y)
    x2, y2 = min(1.0, x + w), min(1.0, y + h)
    if x >= 1.0 or y >= 1.0 or x2 <= x or y2 <= y:
        raise ValueError(f"ROI 完全越出画面：{roi!r}")
    return (x, y, x2 - x, y2 - y)


@dataclass(frozen=True, slots=True)
class _Region:
    """像素坐标系下的填充矩形（右/下开区间）。"""

    x0: int
    y0: int
    x1: int
    y1: int


class MaskedPixels:
    """已遮罩像素的类型化包装（导出侧的类型级强制）。

    内部数组设为只读：持有者无法在原地恢复被遮内容。
    通过 :func:`PrivacyMask.apply` 产生（携带填充区域，可随时复核），
    或经 :func:`assume_masked` 显式信任声明产生（无填充区域记录）。
    """

    __slots__ = ("_array", "_regions", "_fill", "_provenance")

    def __init__(
        self,
        array: np.ndarray,
        *,
        regions: Iterable[_Region] = (),
        fill: FillColor = DEFAULT_FILL,
        provenance: str,
    ) -> None:
        _validate_pixels(array)
        array.setflags(write=False)
        self._array: np.ndarray = array
        self._regions: tuple[_Region, ...] = tuple(regions)
        self._fill: FillColor = fill
        self._provenance = str(provenance)

    # ------------------------------------------------------------------ 属性
    @property
    def pixels(self) -> np.ndarray:
        """只读像素视图 (H, W, 3) uint8。"""
        return self._array

    @property
    def provenance(self) -> str:
        """产生方式说明（apply / thumbnail_safe / assume_masked）。"""
        return self._provenance

    @property
    def fill(self) -> FillColor:
        """填充色。"""
        return self._fill

    @property
    def shape(self) -> tuple[int, ...]:
        return self._array.shape

    def writable_copy(self) -> np.ndarray:
        """可写深拷贝（供 Frame 等需要可写数组的下游使用）。"""
        out = self._array.copy()
        out.setflags(write=True)
        return out

    def checksum(self) -> int:
        """像素校验和（与 capture_api.Frame.checksum 同口径）。"""
        return int(self._array.sum(dtype=np.int64))

    def verify_fill(self) -> None:
        """复核全部填充区域恒为填充色；被破坏即抛 :class:`SecurityKitError`。"""
        for region in self._regions:
            block = self._array[region.y0:region.y1, region.x0:region.x1]
            if not np.all(block == self._fill):
                raise SecurityKitError(
                    f"遮罩完整性被破坏：区域 {region} 不再恒为填充色 {self._fill}"
                )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"MaskedPixels(shape={self._array.shape}, regions={len(self._regions)}, "
            f"provenance={self._provenance!r})"
        )


def assume_masked(pixels: np.ndarray, *, reason: str = "") -> MaskedPixels:
    """把已知已遮罩的普通 ndarray 显式标记为 :class:`MaskedPixels`。

    这是普通数组进入导出通道的**唯一**入口；调用方必须为该决定负责，
    ``reason`` 记录在 provenance 里供审计。
    """
    return MaskedPixels(
        np.array(pixels, copy=True),
        provenance=f"assume_masked:{reason}" if reason else "assume_masked",
    )


class PrivacyMask:
    """固定遮罩集合与统一的遮罩应用入口（SEC-005）。

    ROI 一律使用归一化坐标（0..1），与窗口/画面分辨率解耦；应用时换算为
    像素矩形并向外取整（宁可多遮不可漏遮）。
    """

    def __init__(self, *, fill: FillColor = DEFAULT_FILL) -> None:
        #: 默认填充色（apply/thumbnail_safe 可按次覆盖）。
        self.fill = fill
        self._masks: dict[str, ROI] = {}

    # ------------------------------------------------------------------ 配置
    def add_fixed_mask(self, name: str, roi: ROI) -> None:
        """新增固定遮罩区；名字重复或 ROI 非法即抛 ValueError。"""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("遮罩名必须是非空字符串")
        if name in self._masks:
            raise ValueError(f"遮罩 {name!r} 已存在")
        self._masks[name] = _clamp_roi(roi)

    def remove_fixed_mask(self, name: str) -> None:
        """删除固定遮罩区；不存在抛 KeyError。"""
        del self._masks[name]

    @property
    def mask_names(self) -> tuple[str, ...]:
        """已配置遮罩名（定义顺序）。"""
        return tuple(self._masks)

    def __len__(self) -> int:
        return len(self._masks)

    # ------------------------------------------------------------------ 应用
    def _pixel_regions(self, shape: tuple[int, ...]) -> list[_Region]:
        """归一化 ROI -> 像素矩形（边界向外取整，防止小图漏遮）。"""
        height, width = int(shape[0]), int(shape[1])
        regions: list[_Region] = []
        for x, y, w, h in self._masks.values():
            x0 = int(np.floor(x * width))
            y0 = int(np.floor(y * height))
            x1 = int(np.ceil((x + w) * width))
            y1 = int(np.ceil((y + h) * height))
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(width, x1), min(height, y1)
            if x1 > x0 and y1 > y0:
                regions.append(_Region(x0, y0, x1, y1))
        return regions

    def apply(self, pixels: np.ndarray, *, fill: FillColor | None = None) -> MaskedPixels:
        """对拷贝的像素数组应用全部遮罩；返回 :class:`MaskedPixels`。

        不可绕过约定：
        - 输入数组不被修改（内部 copy）；
        - 返回前断言每个填充区域统计恒为填充色；
        - 未配置任何遮罩时抛 ValueError（防止"空遮罩"伪装成已遮罩数据）。
        """
        _validate_pixels(pixels)
        if not self._masks:
            raise ValueError("未配置任何遮罩：拒绝把未遮罩数据标记为 MaskedPixels")
        used_fill = fill if fill is not None else self.fill
        regions = self._pixel_regions(pixels.shape)
        out = np.array(pixels, copy=True)
        for region in regions:
            out[region.y0:region.y1, region.x0:region.x1] = used_fill
        masked = MaskedPixels(out, regions=regions, fill=used_fill, provenance="privacy_mask.apply")
        masked.verify_fill()  # 后置条件：填充区域统计恒为填充色
        return masked

    def apply_to_frame(self, frame: Frame, *, fill: FillColor | None = None) -> Frame:
        """对一帧应用遮罩：meta 原样保留，pixels 替换为遮罩后的新数组。"""
        masked = self.apply(frame.pixels, fill=fill)
        return Frame(masked.writable_copy(), frame.meta)

    # ------------------------------------------------------------------ 缩略图
    def thumbnail_safe(
        self,
        pixels: np.ndarray,
        max_size: int = 320,
        *,
        fill: FillColor | None = None,
    ) -> MaskedPixels:
        """缩略图安全通道：先遮罩 -> 缩放 -> 再次遮罩。

        第二次遮罩封死缩放插值在遮罩边界处的混色渗漏；结果保证被遮区域
        在缩略图中同样恒为填充色。
        """
        masked = self.apply(pixels, fill=fill)
        used_fill = masked.fill
        height, width = int(masked.pixels.shape[0]), int(masked.pixels.shape[1])
        scale = min(1.0, float(max_size) / float(max(height, width, 1)))
        if scale < 1.0:
            image = Image.fromarray(masked.writable_copy(), mode="RGB")
            new_w = max(1, int(round(width * scale)))
            new_h = max(1, int(round(height * scale)))
            image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
            downscaled = np.asarray(image, dtype=np.uint8)
        else:
            downscaled = masked.pixels
        # 第二次遮罩：归一化 ROI 直接适用于缩放后尺寸
        regions = self._pixel_regions(downscaled.shape)
        out = np.array(downscaled, copy=True)
        for region in regions:
            out[region.y0:region.y1, region.x0:region.x1] = used_fill
        result = MaskedPixels(
            out, regions=regions, fill=used_fill, provenance="privacy_mask.thumbnail_safe"
        )
        result.verify_fill()
        return result

    def mask_thumbnail(
        self,
        pixels: np.ndarray,
        max_size: int = 320,
        *,
        fill: FillColor | None = None,
    ) -> MaskedPixels:
        """:meth:`thumbnail_safe` 的别名（语义化命名，防止误用裸缩略）。"""
        return self.thumbnail_safe(pixels, max_size, fill=fill)


def export_png(masked: MaskedPixels, path: str | Path) -> Path:
    """导出 PNG：**只接受** :class:`MaskedPixels`，导出前再复核填充完整性。

    传入普通 ndarray 直接 TypeError（代码级强制：导出侧拿不到未遮罩原图，
    除非调用方显式 :func:`assume_masked` 承担责任）。
    """
    if not isinstance(masked, MaskedPixels):
        raise TypeError(
            "export_png 只接受 MaskedPixels；普通 ndarray 请先经 PrivacyMask.apply "
            "遮罩，或显式 assume_masked() 声明已遮罩"
        )
    masked.verify_fill()
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(masked.pixels, mode="RGB").save(out, format="PNG")
    return out
