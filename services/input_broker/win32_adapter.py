"""Win32 SendInput 适配器：全平台唯一的真实输入出口（INP-003）。

安全边界（与 ADR-0004 决策 6 对齐，docstring 即契约）：
- 只支持前台输入：SendInput 的键盘（扫描码）与鼠标（绝对坐标）事件；
- **不提供** DLL 注入 / 驱动级输入 / 后台消息投递（PostMessage）/
  硬件模拟等任何绕过前台焦点的手段；目标窗口必须在前台；
- 每批执行前整批前台复核（注入 verifier），任一不符 -> 零真实发送；
- 逐意图 TTL 复核：过期意图跳过、不补发（SAFE-003）；
- 取消屏障：cancelled() 为真时绝不开始发送（INP-009）。

可测性：low_level 默认是真实 SendInput（本文件唯一的 ctypes 系统调用），
测试注入"记录型 sender"即可断言全部载荷（扫描码/标志/次数），
绝不触发真实系统输入。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from common.clock import Clock, MonotonicClock

from input_broker.fake_sink import InputSink, SinkContext, SinkRejection, SinkResult
from input_broker.intents import InputBatch, InputIntent, normalize_key

# ---------------------------------------------------------------------------
# Win32 输入事件标志（公开常量，值与 winuser.h 一致；测试据此断言载荷）。
# ---------------------------------------------------------------------------
KEYEVENTF_EXTENDEDKEY: int = 0x0001
KEYEVENTF_KEYUP: int = 0x0002
KEYEVENTF_SCANCODE: int = 0x0008

MOUSEEVENTF_MOVE: int = 0x0001
MOUSEEVENTF_LEFTDOWN: int = 0x0002
MOUSEEVENTF_LEFTUP: int = 0x0004
MOUSEEVENTF_RIGHTDOWN: int = 0x0008
MOUSEEVENTF_RIGHTUP: int = 0x0010
MOUSEEVENTF_MIDDLEDOWN: int = 0x0020
MOUSEEVENTF_MIDDLEUP: int = 0x0040
MOUSEEVENTF_WHEEL: int = 0x0800
MOUSEEVENTF_ABSOLUTE: int = 0x8000

#: MOUSEEVENTF_ABSOLUTE 的坐标归一化上限（主显示器全宽/全高）。
ABSOLUTE_MAX: int = 0xFFFF  # 65535

#: 鼠标按钮名 -> (按下标志, 释放标志)。
_BUTTON_FLAGS: dict[str, tuple[int, int]] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


class ScanCodes:
    """键盘扫描码表（Set 1 make code）：INP-003 支持的按键集合。

    覆盖：字母 / 数字 / 空格 / 回车 / Esc / 方向键 / Shift / Ctrl / Alt，
    外加 Tab、Backspace 与常用别名（esc/return/lshift…）。
    方向键为扩展键（发送时带 KEYEVENTF_EXTENDEDKEY）。
    """

    #: 按键名（归一化小写）-> (扫描码, 是否扩展键)
    TABLE: dict[str, tuple[int, bool]] = {
        "escape": (0x01, False),
        "esc": (0x01, False),
        "backspace": (0x0E, False),
        "tab": (0x0F, False),
        "enter": (0x1C, False),
        "return": (0x1C, False),
        "ctrl": (0x1D, False),
        "left_ctrl": (0x1D, False),
        "lctrl": (0x1D, False),
        "shift": (0x2A, False),
        "left_shift": (0x2A, False),
        "lshift": (0x2A, False),
        "alt": (0x38, False),
        "left_alt": (0x38, False),
        "lalt": (0x38, False),
        "space": (0x39, False),
        "1": (0x02, False), "2": (0x03, False), "3": (0x04, False),
        "4": (0x05, False), "5": (0x06, False), "6": (0x07, False),
        "7": (0x08, False), "8": (0x09, False), "9": (0x0A, False),
        "0": (0x0B, False),
        "a": (0x1E, False), "b": (0x30, False), "c": (0x2E, False),
        "d": (0x20, False), "e": (0x12, False), "f": (0x21, False),
        "g": (0x22, False), "h": (0x23, False), "i": (0x17, False),
        "j": (0x24, False), "k": (0x25, False), "l": (0x26, False),
        "m": (0x32, False), "n": (0x31, False), "o": (0x18, False),
        "p": (0x19, False), "q": (0x10, False), "r": (0x13, False),
        "s": (0x1F, False), "t": (0x14, False), "u": (0x16, False),
        "v": (0x2F, False), "w": (0x11, False), "x": (0x2D, False),
        "y": (0x15, False), "z": (0x2C, False),
        "up": (0x48, True),
        "left": (0x4B, True),
        "right": (0x4D, True),
        "down": (0x50, True),
    }

    @classmethod
    def lookup(cls, key: Any) -> tuple[int, bool] | None:
        """查扫描码：返回 (扫描码, 是否扩展键)；未知按键返回 None。"""
        normalized = normalize_key(key)
        if normalized is None:
            return None
        return cls.TABLE.get(normalized)


@dataclass(frozen=True)
class SendInputUnit:
    """一个真实的 SendInput 事件单元（注入 sender 收到的载荷）。

    type="key"  ：scancode/extended 生效（KEYBDINPUT + KEYEVENTF_SCANCODE）；
    type="mouse"：flags 为 MOUSEEVENTF 组合，dx/dy 为 0~65535 绝对坐标，
                  mouse_data 为滚轮增量。
    """

    type: str  # "key" | "mouse"
    flags: int
    scancode: int = 0
    extended: bool = False
    dx: int = 0
    dy: int = 0
    mouse_data: int = 0

    @property
    def is_release(self) -> bool:
        """是否为释放类事件（KEYEVENTF_KEYUP / *_UP 标志）。"""
        if self.type == "key":
            return bool(self.flags & KEYEVENTF_KEYUP)
        return bool(
            self.flags & (MOUSEEVENTF_LEFTUP | MOUSEEVENTF_RIGHTUP | MOUSEEVENTF_MIDDLEUP)
        )


#: 低层发送函数：units -> 成功插入的事件数（与真实 SendInput 返回值同义）。
LowLevelSender = Callable[[Sequence[SendInputUnit]], int]

#: 取消检查函数：返回 True 表示已取消，必须零发送。
CancelledCheck = Callable[[], bool]


def _default_screen_size() -> tuple[int, int]:
    """主显示器物理像素尺寸（真实路径；仅 Windows 可用）。"""
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    return (
        int(user32.GetSystemMetrics(0)),  # SM_CXSCREEN
        int(user32.GetSystemMetrics(1)),  # SM_CYSCREEN
    )


def send_units_real(units: Sequence[SendInputUnit]) -> int:
    """真实 SendInput 发送（本模块唯一的系统输入调用，INP-003）。

    - 键盘：KEYBDINPUT + KEYEVENTF_SCANCODE（扩展键加 KEYEVENTF_EXTENDEDKEY）；
    - 鼠标：MOUSEEVENTF 绝对坐标（0~65535 归一化）；
    - 返回成功插入的事件数（调用方必须核对数量，不符即整批失败）。
    ctypes 仅允许出现在本函数所在文件（静态守卫约定）。
    """
    if sys.platform != "win32":
        raise RuntimeError("sendinput_requires_windows")
    if not units:
        return 0

    import ctypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_ulong),
            ("wParamL", ctypes.c_ushort),
            ("wParamH", ctypes.c_ushort),
        ]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", INPUTUNION)]

    INPUT_MOUSE: int = 0
    INPUT_KEYBOARD: int = 1

    arr = (INPUT * len(units))()
    for i, unit in enumerate(units):
        if unit.type == "key":
            flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if unit.extended else 0)
            flags |= KEYEVENTF_KEYUP if unit.is_release else 0
            arr[i].type = INPUT_KEYBOARD
            arr[i].union.ki = KEYBDINPUT(
                0, unit.scancode & 0xFF, flags, 0, None
            )
        else:
            arr[i].type = INPUT_MOUSE
            arr[i].union.mi = MOUSEINPUT(
                unit.dx, unit.dy, unit.mouse_data & 0xFFFFFFFF, unit.flags, 0, None
            )
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    return int(user32.SendInput(len(units), arr, ctypes.sizeof(INPUT)))


class SendInputAdapter:
    """真实输入执行器：意图 -> SendInput 载荷（INP-003）。

    执行顺序（任一前置不符 -> 零真实发送并给原因）：
    1. ctx.mode 复核（纵深防御：非 real_input 拒绝）；
    2. 取消屏障 cancelled()（INP-009）；
    3. 整批前台复核 verifier.verify_batch(batch)（INP-005 注入）；
    4. 逐意图 TTL 复核（过期即拒绝，SAFE-003）；
    5. 载荷构建（未知按键/按钮给出错误码）；
    6. 再次取消检查 -> low_level 发送 -> 核对插入数量。

    InputSink 协议实现，可直接作为 InputBroker 的 sink。
    """

    def __init__(
        self,
        low_level: LowLevelSender | None = None,
        verifier: Any = None,
        *,
        clock: Clock | None = None,
        screen_size: tuple[int, int] | None = None,
        cancelled: CancelledCheck | None = None,
    ) -> None:
        self.low_level: LowLevelSender = low_level or send_units_real
        #: 前台复核器：duck-typing，要求 verify_batch(batch) -> (ok, reasons)。
        self.verifier = verifier
        self.clock: Clock = clock if clock is not None else MonotonicClock()
        self.screen_size = screen_size
        self.cancelled = cancelled
        #: 已提交给 low_level 的全部单元（观测/审计用，测试断言入口）。
        self.sent_units: list[SendInputUnit] = []
        self.sent_batches: int = 0

    # ------------------------------------------------------------------ 执行
    def execute(self, batch: InputBatch, ctx: SinkContext | None = None) -> SinkResult:
        """执行一个批次；任何拒绝路径保证零真实发送。

        补偿释放批次（cause 以 ``broker_`` 开头，来自 cancel/stop/急停路径）
        跳过前台复核与取消检查：停止路径的 key_up 必须无条件发出，
        否则失焦/急停时已按下的键将永远无法释放（安全释放优先于一切）。
        """
        compensation = batch.cause.startswith("broker_")
        if ctx is not None and ctx.mode and ctx.mode != "real_input":
            return self._reject(batch, "mode_not_allowed")
        if not compensation and self.cancelled is not None and self.cancelled():
            return self._reject(batch, "broker_cancelled")

        if self.verifier is not None and not compensation:
            verdict = self.verifier.verify_batch(batch)
            if not bool(getattr(verdict, "ok", False)):
                reasons = ";".join(
                    str(r) for r in getattr(verdict, "reasons", []) or ["foreground_mismatch"]
                )
                return self._reject(batch, f"foreground_mismatch:{reasons}")

        now = float(self.clock.now())
        accepted: list[InputIntent] = []
        rejected: list[SinkRejection] = []
        units: list[SendInputUnit] = []

        for intent in batch.intents:
            if intent.expired(now):
                rejected.append(SinkRejection(intent=intent, reason="expired_intent"))
                continue
            built = self._build_units(intent)
            if isinstance(built, str):
                rejected.append(SinkRejection(intent=intent, reason=built))
                continue
            units.extend(built)
            accepted.append(intent)

        # 取消屏障复核：构建期间可能已取消（INP-009，顺序可预测）。
        if (
            units
            and not compensation
            and self.cancelled is not None
            and self.cancelled()
        ):
            rejected.extend(SinkRejection(intent=i, reason="broker_cancelled") for i in accepted)
            return SinkResult(
                batch_id=batch.batch_id, accepted_intents=[], rejected=rejected
            )

        if units:
            try:
                inserted = int(self.low_level(units))
            except Exception as exc:  # noqa: BLE001 - 系统调用失败 -> 整批拒绝
                rejected.extend(
                    SinkRejection(intent=i, reason=f"sendinput_failed:{exc}") for i in accepted
                )
                return SinkResult(
                    batch_id=batch.batch_id, accepted_intents=[], rejected=rejected
                )
            if inserted != len(units):
                rejected.extend(
                    SinkRejection(intent=i, reason="sendinput_failed:count_mismatch")
                    for i in accepted
                )
                return SinkResult(
                    batch_id=batch.batch_id, accepted_intents=[], rejected=rejected
                )
            self.sent_units.extend(units)
            self.sent_batches += 1
        return SinkResult(
            batch_id=batch.batch_id, accepted_intents=accepted, rejected=rejected
        )

    # ------------------------------------------------------------------ 载荷
    def _normalize_point(self, x: Any, y: Any) -> tuple[int, int] | str:
        """像素坐标 -> MOUSEEVENTF_ABSOLUTE 的 0~65535 归一化坐标。"""
        try:
            px, py = float(x), float(y)
        except (TypeError, ValueError):
            return "invalid_coordinates"
        if px < 0 or py < 0:
            return "invalid_coordinates"
        width, height = self.screen_size or _default_screen_size()
        if width <= 0 or height <= 0:
            return "invalid_screen_size"
        dx = round(px * ABSOLUTE_MAX / float(width))
        dy = round(py * ABSOLUTE_MAX / float(height))
        return max(0, min(ABSOLUTE_MAX, dx)), max(0, min(ABSOLUTE_MAX, dy))

    def _build_units(self, intent: InputIntent) -> list[SendInputUnit] | str:
        """把单个意图转为发送单元；返回错误码字符串表示拒绝。"""
        kind = intent.kind
        if kind == "wait":
            # 等待是调度语义，不产生任何真实输入事件，直接接受。
            return []
        if kind in ("key_down", "key_up"):
            looked = ScanCodes.lookup(intent.payload.get("key"))
            if looked is None:
                return "unknown_key"
            scancode, extended = looked
            flags = KEYEVENTF_SCANCODE
            if kind == "key_up":
                flags |= KEYEVENTF_KEYUP
            return [
                SendInputUnit(
                    type="key", flags=flags, scancode=scancode, extended=extended
                )
            ]
        if kind == "click":
            button = str(intent.payload.get("button", "")).strip().lower()
            pair = _BUTTON_FLAGS.get(button)
            if pair is None:
                return "unknown_button"
            point = self._normalize_point(
                intent.payload.get("x"), intent.payload.get("y")
            )
            if isinstance(point, str):
                return point
            dx, dy = point
            down_flag, up_flag = pair
            base = MOUSEEVENTF_ABSOLUTE
            return [
                SendInputUnit(type="mouse", flags=base | down_flag, dx=dx, dy=dy),
                SendInputUnit(type="mouse", flags=base | up_flag, dx=dx, dy=dy),
            ]
        if kind == "move":
            point = self._normalize_point(
                intent.payload.get("x"), intent.payload.get("y")
            )
            if isinstance(point, str):
                return point
            dx, dy = point
            return [
                SendInputUnit(
                    type="mouse", flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                    dx=dx, dy=dy,
                )
            ]
        if kind == "wheel":
            try:
                delta = int(intent.payload.get("delta", 0))
            except (TypeError, ValueError):
                return "invalid_wheel_delta"
            return [
                SendInputUnit(type="mouse", flags=MOUSEEVENTF_WHEEL, mouse_data=delta)
            ]
        return "unknown_intent_kind"

    # ------------------------------------------------------------------ 内部
    def _reject(self, batch: InputBatch, reason: str) -> SinkResult:
        return SinkResult(
            batch_id=batch.batch_id,
            accepted_intents=[],
            rejected=[SinkRejection(intent=i, reason=reason) for i in batch.intents],
        )
