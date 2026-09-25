"""确定性场景渲染器（LAB-001 / LAB-002）。

给定 :class:`SceneConfig` 与 :class:`SceneState`，用 numpy 纯矢量化绘制一帧
RGB uint8 图像。核心契约（LAB-001 验收）：

- 同 (seed, resolution, ui_scale, state) => 逐字节相同的帧；
- 帧是状态与配置的纯函数：不混入 uuid / 墙钟时间 / 任何随机数；
- 不做任何真实输入或窗口操作，仅生成图像。

场景元素（LAB-002）：背景渐变与标题/帧号文字块（内置 3x5 位图字体，
跨运行确定）、血条（绿-红插值）、资源条（蓝）、技能冷却图标（就绪亮 /
冷却暗）、目标标记、掉落标记、加载画面（整帧深色 + 进度条）、随机弹窗
（明确边框色）。每个 :class:`SceneState` 字段在像素上逐项可区分，
供检测器与单测量化验证。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "SUPPORTED_RESOLUTIONS",
    "SceneConfig",
    "SceneState",
    "Renderer",
    "health_color",
    "COLOR_HEALTH_LOW",
    "COLOR_HEALTH_HIGH",
    "COLOR_RESOURCE_FILL",
    "COLOR_PROGRESS_FILL",
]

# 矩形 (x0, y0, x1, y1)，半开区间 [x0, x1) x [y0, y1)，像素坐标。
Rect = tuple[int, int, int, int]

# 至少支持的三档分辨率（宽, 高）；其他正整数分辨率同样可用。
SUPPORTED_RESOLUTIONS: tuple[tuple[int, int], ...] = (
    (1280, 720),
    (1920, 1080),
    (2560, 1440),
)

# ---- 调色板（RGB，0-255） ------------------------------------------------
COLOR_BG_TOP: tuple[int, int, int] = (44, 52, 70)
COLOR_BG_BOTTOM: tuple[int, int, int] = (18, 20, 28)
COLOR_HEADER: tuple[int, int, int] = (24, 28, 38)
COLOR_ACCENT: tuple[int, int, int] = (96, 150, 235)
COLOR_TEXT: tuple[int, int, int] = (225, 228, 235)
COLOR_TEXT_DIM: tuple[int, int, int] = (140, 146, 158)
COLOR_BAR_SLOT: tuple[int, int, int] = (30, 32, 38)
COLOR_BAR_BORDER: tuple[int, int, int] = (78, 82, 94)
COLOR_HEALTH_LOW: tuple[int, int, int] = (214, 48, 49)
COLOR_HEALTH_HIGH: tuple[int, int, int] = (64, 196, 80)
COLOR_RESOURCE_FILL: tuple[int, int, int] = (52, 108, 235)
COLOR_ICON_READY: tuple[int, int, int] = (244, 196, 48)
COLOR_ICON_HIGHLIGHT: tuple[int, int, int] = (255, 232, 120)
COLOR_ICON_READY_BORDER: tuple[int, int, int] = (150, 110, 20)
COLOR_ICON_COOLDOWN: tuple[int, int, int] = (36, 38, 46)
COLOR_ICON_COOLDOWN_BORDER: tuple[int, int, int] = (70, 72, 80)
COLOR_TARGET_FILL: tuple[int, int, int] = (255, 238, 88)
COLOR_TARGET_BORDER: tuple[int, int, int] = (255, 255, 255)
COLOR_TARGET_CORE: tuple[int, int, int] = (40, 40, 40)
COLOR_LOOT_FILL: tuple[int, int, int] = (255, 176, 32)
COLOR_LOOT_BORDER: tuple[int, int, int] = (140, 90, 10)
COLOR_LOADING_BG: tuple[int, int, int] = (10, 12, 18)
COLOR_PROGRESS_FILL: tuple[int, int, int] = (90, 200, 250)
COLOR_POPUP_FILL: tuple[int, int, int] = (235, 235, 225)
COLOR_POPUP_BORDER: tuple[int, int, int] = (255, 128, 0)
COLOR_POPUP_TITLE: tuple[int, int, int] = (120, 124, 140)
COLOR_POPUP_TEXT: tuple[int, int, int] = (120, 124, 138)
COLOR_POPUP_BTN_OK: tuple[int, int, int] = (90, 160, 90)
COLOR_POPUP_BTN_CANCEL: tuple[int, int, int] = (170, 90, 90)

# 内置 3x5 位图字体（'#' 为实心像素）：仅覆盖本模块用到的字符，
# 不依赖系统字体，保证文字渲染跨运行/跨机器逐字节确定。
_GLYPHS: dict[str, tuple[str, str, str, str, str]] = {
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", ".##", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", ".#.", ".#.", ".#."),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    "A": (".#.", "#.#", "###", "#.#", "#.#"),
    "B": ("##.", "#.#", "##.", "#.#", "##."),
    "D": ("##.", "#.#", "#.#", "#.#", "##."),
    "E": ("###", "#..", "##.", "#..", "###"),
    "G": ("###", "#..", "#.#", "#.#", "###"),
    "I": ("###", ".#.", ".#.", ".#.", "###"),
    "L": ("#..", "#..", "#..", "#..", "###"),
    "N": ("#.#", "###", "###", "#.#", "#.#"),
    "O": ("###", "#.#", "#.#", "#.#", "###"),
    "R": ("##.", "#.#", "##.", "#.#", "#.#"),
}


@dataclass(frozen=True)
class SceneConfig:
    """渲染配置：种子、分辨率与 UI 缩放。

    seed 参与背景色调（确定性地派生自种子，不用 uuid/时间），使不同种子
    的帧在像素层面必然可区分；resolution 布局按宽高比例换算，三档支持
    分辨率见 :data:`SUPPORTED_RESOLUTIONS`。
    """

    seed: int
    resolution: tuple[int, int] = (1280, 720)
    ui_scale: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "seed", int(self.seed))
        object.__setattr__(
            self, "resolution", (int(self.resolution[0]), int(self.resolution[1]))
        )
        if self.resolution[0] <= 0 or self.resolution[1] <= 0:
            raise ValueError(f"resolution 必须为正宽高，收到 {self.resolution!r}")
        if not (float(self.ui_scale) > 0.0):
            raise ValueError(f"ui_scale 必须为正数，收到 {self.ui_scale!r}")
        object.__setattr__(self, "ui_scale", float(self.ui_scale))


@dataclass(frozen=True)
class SceneState:
    """一帧的场景真值状态（LAB-002 / LAB-007）。

    所有字段都会在渲染输出中逐项产生可量化的像素差异：
    血条/资源条宽度与颜色、冷却图标亮暗、目标/掉落标记、加载画面、
    弹窗、以及标题栏的帧号读数。
    """

    health_ratio: float = 1.0
    resource_ratio: float = 1.0
    cooldown_ready: bool = True
    target_present: bool = False
    loot_present: bool = False
    loading_active: bool = False
    loading_progress: float = 0.0
    popup_open: bool = False
    frame_index: int = 0

    def __post_init__(self) -> None:
        for name in ("health_ratio", "resource_ratio", "loading_progress"):
            value = float(getattr(self, name))
            if not (0.0 <= value <= 1.0):
                raise ValueError(f"{name} 必须在 [0, 1] 内，收到 {value!r}")
            object.__setattr__(self, name, value)
        if int(self.frame_index) < 0:
            raise ValueError(f"frame_index 不能为负，收到 {self.frame_index!r}")
        object.__setattr__(self, "frame_index", int(self.frame_index))


def health_color(ratio: float) -> tuple[int, int, int]:
    """血条填充色：ratio=1 为绿、0 为红，中间线性插值（确定性取整）。"""
    r = min(max(float(ratio), 0.0), 1.0)
    lo_r, lo_g, lo_b = COLOR_HEALTH_LOW
    hi_r, hi_g, hi_b = COLOR_HEALTH_HIGH
    return (
        int(round(lo_r + (hi_r - lo_r) * r)),
        int(round(lo_g + (hi_g - lo_g) * r)),
        int(round(lo_b + (hi_b - lo_b) * r)),
    )


def _seed_tint(seed: int) -> tuple[int, int, int]:
    """从种子整数确定性地派生一个小幅 RGB 色调偏移（每通道 0-7）。

    仅用整数算术（不用 hash()/uuid/时间），保证跨进程结果一致；偏移幅度
    足够小，不影响各元素的识别，但使不同种子的帧逐像素可区分。
    """
    value = (abs(int(seed)) * 2654435761 + 0x9E3779B9) & 0xFFFFFFFF
    return ((value >> 20) & 0x07, (value >> 12) & 0x07, (value >> 4) & 0x07)


def _tinted(color: tuple[int, int, int], tint: tuple[int, int, int]) -> tuple[int, int, int]:
    cr, cg, cb = color
    tr, tg, tb = tint
    return (min(255, cr + tr), min(255, cg + tg), min(255, cb + tb))


def _fill_rect(frame: np.ndarray, rect: Rect, color: tuple[int, int, int]) -> None:
    """半开矩形填充（自动裁剪到画面内），矢量化的切片赋值。"""
    height, width = frame.shape[:2]
    x0 = max(0, min(int(rect[0]), width))
    x1 = max(x0, min(int(rect[2]), width))
    y0 = max(0, min(int(rect[1]), height))
    y1 = max(y0, min(int(rect[3]), height))
    if x1 > x0 and y1 > y0:
        frame[y0:y1, x0:x1] = color


def _draw_border(frame: np.ndarray, rect: Rect, color: tuple[int, int, int], thickness: int) -> None:
    """在矩形内沿画 1px 以上的边框（不越出矩形）。"""
    x0, y0, x1, y1 = rect
    t = max(1, int(thickness))
    _fill_rect(frame, (x0, y0, x1, y0 + t), color)
    _fill_rect(frame, (x0, y1 - t, x1, y1), color)
    _fill_rect(frame, (x0, y0, x0 + t, y1), color)
    _fill_rect(frame, (x1 - t, y0, x1, y1), color)


class Renderer:
    """确定性渲染器：``render(state)`` 返回 (H, W, 3) uint8 RGB 帧。

    布局在构造时按分辨率与 ui_scale 一次性换算为像素矩形；绘制与
    各 ``*_rect`` 访问器共用同一份布局，保证外部量化测量与绘制一致。
    """

    def __init__(self, config: SceneConfig) -> None:
        self._config = config
        width, height = config.resolution
        self._width = int(width)
        self._height = int(height)
        scale = config.ui_scale
        w, h = self._width, self._height

        # 标题栏与文字（字体像素限制在标题栏内，避免溢出）。
        self._header_h = max(6, int(h * 0.08))
        self._margin_x = int(w * 0.045)
        font_px = max(2, int(h * 0.014 * scale))
        self._font_px = max(2, min(font_px, max(2, (self._header_h - 2) // 5)))

        # 血条 / 资源条。
        bar_h = max(4, int(h * 0.030 * scale))
        bar_w = int(w * 0.27)
        health_y = int(h * 0.105)
        gap = max(3, int(h * 0.018 * scale))
        self._bar_border = max(1, int(round(scale)))
        self._health_slot: Rect = (
            self._margin_x, health_y, self._margin_x + bar_w, health_y + bar_h,
        )
        resource_y = health_y + bar_h + gap
        self._resource_slot: Rect = (
            self._margin_x, resource_y, self._margin_x + bar_w, resource_y + bar_h,
        )

        # 技能冷却图标（右侧一排 3 个）。
        icon = max(6, int(min(w, h) * 0.045 * scale))
        icon_gap = max(2, int(icon * 0.25))
        icons_right = w - self._margin_x
        icon_y = health_y
        self._icon_rects: tuple[Rect, ...] = tuple(
            (
                icons_right - (k + 1) * icon - k * icon_gap,
                icon_y,
                icons_right - k * (icon + icon_gap),
                icon_y + icon,
            )
            for k in range(3)
        )

        # 目标标记（画面中央高对比方块）与掉落标记。
        target_half = max(6, int(min(w, h) * 0.06 * scale))
        cx, cy = w // 2, h // 2
        self._target_rect: Rect = (
            cx - target_half, cy - target_half, cx + target_half, cy + target_half,
        )
        loot_half = max(4, int(min(w, h) * 0.032 * scale))
        loot_cx, loot_cy = int(w * 0.68), int(h * 0.74)
        self._loot_rect: Rect = (
            loot_cx - loot_half, loot_cy - loot_half, loot_cx + loot_half, loot_cy + loot_half,
        )

        # 加载进度条与弹窗。
        progress_y = int(h * 0.58)
        self._progress_slot: Rect = (int(w * 0.25), progress_y, int(w * 0.75), progress_y + bar_h)
        self._popup_rect: Rect = (int(w * 0.34), int(h * 0.30), int(w * 0.66), int(h * 0.52))
        self._popup_border = max(2, int(round(2 * scale)))

        # 种子色调：仅影响背景/标题栏底色，保证异种子帧逐像素可区分。
        self._tint = _seed_tint(config.seed)
        self._bg_top = _tinted(COLOR_BG_TOP, self._tint)
        self._bg_bottom = _tinted(COLOR_BG_BOTTOM, self._tint)
        self._accent = _tinted(COLOR_ACCENT, self._tint)

    # ---- 属性与布局访问器（供检测器/测试做像素量化测量） ----------------

    @property
    def config(self) -> SceneConfig:
        return self._config

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def header_rect(self) -> Rect:
        return (0, 0, self._width, self._header_h)

    def health_bar_rect(self) -> Rect:
        return self._health_slot

    def health_bar_inner_rect(self) -> Rect:
        return self._inner(self._health_slot)

    def resource_bar_rect(self) -> Rect:
        return self._resource_slot

    def resource_bar_inner_rect(self) -> Rect:
        return self._inner(self._resource_slot)

    def cooldown_icon_rects(self) -> tuple[Rect, ...]:
        return self._icon_rects

    def target_rect(self) -> Rect:
        return self._target_rect

    def loot_rect(self) -> Rect:
        return self._loot_rect

    def progress_bar_rect(self) -> Rect:
        return self._progress_slot

    def progress_bar_inner_rect(self) -> Rect:
        return self._inner(self._progress_slot)

    def popup_rect(self) -> Rect:
        return self._popup_rect

    def _inner(self, rect: Rect) -> Rect:
        x0, y0, x1, y1 = rect
        t = self._bar_border
        return (x0 + t, y0 + t, x1 - t, y1 - t)

    # ---- 渲染 ------------------------------------------------------------

    def render(self, state: SceneState) -> np.ndarray:
        """渲染一帧：返回 (H, W, 3) uint8 RGB 数组（纯函数，无副作用）。"""
        frame = self._render_background()
        if state.loading_active:
            self._render_loading(frame, state)
            return frame
        self._render_header(frame, state)
        self._render_bar(frame, self._health_slot, state.health_ratio, health_color(state.health_ratio))
        self._render_bar(frame, self._resource_slot, state.resource_ratio, COLOR_RESOURCE_FILL)
        self._render_cooldown_icons(frame, state.cooldown_ready)
        if state.loot_present:
            self._render_loot(frame)
        if state.target_present:
            self._render_target(frame)
        if state.popup_open:
            self._render_popup(frame)
        return frame

    def _render_background(self) -> np.ndarray:
        """背景：对角线渐变（纯 numpy 矢量化，含种子色调）。"""
        h, w = self._height, self._width
        vv = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
        uu = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
        mix = vv * np.float32(0.75) + uu * np.float32(0.25)  # (H, W) float32
        top = np.array(self._bg_top, dtype=np.float32)
        bottom = np.array(self._bg_bottom, dtype=np.float32)
        img = top[None, None, :] * (np.float32(1.0) - mix[:, :, None]) + bottom[None, None, :] * mix[:, :, None]
        return np.clip(np.rint(img), 0.0, 255.0).astype(np.uint8)

    def _render_header(self, frame: np.ndarray, state: SceneState) -> None:
        """标题栏：标题文字块 + 种子色调强调线 + 右侧帧号读数。"""
        w = self._width
        _fill_rect(frame, (0, 0, w, self._header_h), COLOR_HEADER)
        accent_h = max(1, self._bar_border)
        _fill_rect(frame, (0, self._header_h - accent_h, w, self._header_h), self._accent)
        title_y = max(0, (self._header_h - 5 * self._font_px) // 2)
        self._draw_text(frame, "ARENA LAB", self._margin_x, title_y, COLOR_TEXT)
        digits = str(state.frame_index).zfill(6)
        digits_x = w - self._margin_x - self._text_width(digits)
        self._draw_text(frame, digits, digits_x, title_y, COLOR_TEXT_DIM)

    def _render_loading(self, frame: np.ndarray, state: SceneState) -> None:
        """加载画面：整帧深色 + 居中进度条 + LOADING 文字块 + 帧号。"""
        frame[:] = COLOR_LOADING_BG
        w = self._width
        progress_y = self._progress_slot[1]
        self._render_bar(frame, self._progress_slot, state.loading_progress, COLOR_PROGRESS_FILL)
        text_y = max(2, progress_y - 5 * self._font_px - max(4, int(self._height * 0.02)))
        self._draw_text(frame, "LOADING", (w - self._text_width("LOADING")) // 2, text_y, COLOR_TEXT)
        digits = str(state.frame_index).zfill(6)
        digits_x = w - self._margin_x - self._text_width(digits)
        self._draw_text(frame, digits, digits_x, max(2, int(self._height * 0.02)), COLOR_TEXT_DIM)

    def _render_bar(self, frame: np.ndarray, slot: Rect, ratio: float, fill_color: tuple[int, int, int]) -> None:
        """通用条形槽：槽底 + 边框 + 按 ratio 取整的填充宽度。"""
        _fill_rect(frame, slot, COLOR_BAR_SLOT)
        _draw_border(frame, slot, COLOR_BAR_BORDER, self._bar_border)
        ix0, _iy0, ix1, _iy1 = self._inner(slot)
        inner_w = ix1 - ix0
        if inner_w <= 0:
            return
        fill_w = int(round(min(max(ratio, 0.0), 1.0) * inner_w))
        x0, y0, _x1, y1 = slot
        t = self._bar_border
        _fill_rect(frame, (ix0, y0 + t, ix0 + fill_w, y1 - t), fill_color)

    def _render_cooldown_icons(self, frame: np.ndarray, ready: bool) -> None:
        """技能冷却图标：就绪=亮色块（含高光），冷却中=暗色块。"""
        t = max(1, self._bar_border)
        for rect in self._icon_rects:
            if ready:
                _fill_rect(frame, rect, COLOR_ICON_READY)
                x0, y0, x1, y1 = rect
                highlight_h = max(1, (y1 - y0) // 6)
                _fill_rect(frame, (x0 + t, y0 + t, x1 - t, y0 + t + highlight_h), COLOR_ICON_HIGHLIGHT)
                _draw_border(frame, rect, COLOR_ICON_READY_BORDER, t)
            else:
                _fill_rect(frame, rect, COLOR_ICON_COOLDOWN)
                _draw_border(frame, rect, COLOR_ICON_COOLDOWN_BORDER, t)

    def _render_target(self, frame: np.ndarray) -> None:
        """目标标记：画面中央高对比方块（亮黄填充 + 白边 + 深色核心）。"""
        rect = self._target_rect
        x0, y0, x1, y1 = rect
        t = self._popup_border
        _fill_rect(frame, (x0 + t, y0 + t, x1 - t, y1 - t), COLOR_TARGET_FILL)
        _draw_border(frame, rect, COLOR_TARGET_BORDER, t)
        half = max(2, ((x1 - x0 - 2 * t) // 2) // 3)
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        _fill_rect(frame, (cx - half, cy - half, cx + half, cy + half), COLOR_TARGET_CORE)

    def _render_loot(self, frame: np.ndarray) -> None:
        """掉落标记：金色小方块 + 深色边框。"""
        _fill_rect(frame, self._loot_rect, COLOR_LOOT_FILL)
        _draw_border(frame, self._loot_rect, COLOR_LOOT_BORDER, max(1, self._bar_border))

    def _render_popup(self, frame: np.ndarray) -> None:
        """随机弹窗：上层矩形框，明确的橙色边框 + 标题条 + 文本行 + 按钮。"""
        rect = self._popup_rect
        x0, y0, x1, y1 = rect
        t = self._popup_border
        h = self._height
        _fill_rect(frame, rect, COLOR_POPUP_FILL)
        _draw_border(frame, rect, COLOR_POPUP_BORDER, t)
        ix0, iy0, ix1, iy1 = x0 + t, y0 + t, x1 - t, y1 - t
        strip_h = max(4, int(h * 0.03))
        _fill_rect(frame, (ix0, iy0, ix1, iy0 + strip_h), COLOR_POPUP_TITLE)
        gap = max(3, int(h * 0.012))
        line_h = max(2, int(h * 0.012))
        line_w = int((ix1 - ix0) * 0.6)
        line_y = iy0 + strip_h + gap
        _fill_rect(frame, (ix0 + gap, line_y, ix0 + gap + line_w, line_y + line_h), COLOR_POPUP_TEXT)
        line2_y = line_y + line_h + max(2, gap // 2)
        _fill_rect(frame, (ix0 + gap, line2_y, ix0 + gap + int(line_w * 0.75), line2_y + line_h), COLOR_POPUP_TEXT)
        btn_h = max(3, int(h * 0.022))
        btn_w = int((ix1 - ix0) * 0.18)
        pad = max(2, int(h * 0.015))
        btn_y0 = iy1 - pad - btn_h
        right = ix1 - pad
        _fill_rect(frame, (right - btn_w, btn_y0, right, btn_y0 + btn_h), COLOR_POPUP_BTN_CANCEL)
        _fill_rect(frame, (right - 2 * btn_w - pad, btn_y0, right - btn_w - pad, btn_y0 + btn_h), COLOR_POPUP_BTN_OK)

    # ---- 文字（内置位图字体，跨运行确定） --------------------------------

    def _text_width(self, text: str) -> int:
        px = self._font_px
        return max(0, len(text) * 4 * px - px)

    def _draw_text(self, frame: np.ndarray, text: str, x: int, y: int, color: tuple[int, int, int]) -> None:
        """用 3x5 位图字体绘制文字；未知字符按空格宽度跳过。"""
        px = self._font_px
        cursor_x = int(x)
        for ch in text:
            glyph = _GLYPHS.get(ch)
            if glyph is not None:
                for row in range(5):
                    cells = glyph[row]
                    for col in range(3):
                        if cells[col] == "#":
                            _fill_rect(
                                frame,
                                (cursor_x + col * px, y + row * px,
                                 cursor_x + (col + 1) * px, y + (row + 1) * px),
                                color,
                            )
            cursor_x += 4 * px
