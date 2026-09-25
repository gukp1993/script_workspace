"""全局急停热键（INP-006）：独立于 UI 线程的 RegisterHotKey 监听。

- 默认组合 Ctrl+Alt+F12（不可取消的默认急停组合，可配置扩展但默认必须可用）；
- 独立后台线程注册并泵消息：即使 UI 线程/预览卡死也能触发急停；
- 注册失败（热键被占用等）必须给出明确错误（error 字段 + on_error 回调），
  绝不静默降级成"没有急停"；
- 注册器可注入：测试用假注册器触发回调，零 win32 依赖；
  真实注册器是本文件内唯一的 ctypes user32 调用（静态守卫约定）。
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Callable

#: 默认急停组合：Ctrl + Alt + F12。
DEFAULT_HOTKEY_LABEL: str = "Ctrl+Alt+F12"

#: winuser.h 常量（注册热键用）。
MOD_ALT: int = 0x0001
MOD_CONTROL: int = 0x0002
MOD_SHIFT: int = 0x0004
MOD_NOREPEAT: int = 0x4000
WM_HOTKEY: int = 0x0312
VK_F12: int = 0x7B

#: 注册失败的明确错误码（热键被其他进程占用是最常见原因）。
ERROR_REGISTER_FAILED: str = "hotkey_register_failed_in_use"

#: 触发回调：source 固定为 "hotkey"，由 EstopController 落审计。
TriggerCallback = Callable[[str], None]

#: 错误回调：注册失败等不可用状态（参数为机器可读错误码）。
ErrorCallback = Callable[[str], None]


@dataclass(frozen=True)
class HotkeyCombo:
    """急停组合键描述。"""

    modifiers: int = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
    virtual_key: int = VK_F12
    label: str = DEFAULT_HOTKEY_LABEL


class HotkeyRegistrar:
    """真实注册器：user32 RegisterHotKey + PeekMessageW 消息泵。

    线程约定：RegisterHotKey 与消息泵必须在同一线程调用，
    因此由 EstopHotkey 的监听线程统一完成注册、泵送与注销。
    """

    def __init__(self, combo: HotkeyCombo) -> None:
        self.combo = combo
        self.registered: threading.Event = threading.Event()

    def register(self) -> bool:
        """在当前线程注册热键；失败返回 False（调用方给出明确错误）。"""
        if sys.platform != "win32":
            return False
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        ok = user32.RegisterHotKey(None, 1, self.combo.modifiers, self.combo.virtual_key)
        if ok:
            self.registered.set()
        return bool(ok)

    def pump_once(self, timeout_ms: int) -> bool:
        """在至多 timeout_ms 毫秒内泵消息；热键触发返回 True。"""
        if sys.platform != "win32":
            return False
        import ctypes
        import time

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_ulong),
                ("wParam", ctypes.c_void_p),
                ("lParam", ctypes.c_void_p),
                ("time", ctypes.c_ulong),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        msg = MSG()
        while time.monotonic() < deadline:
            # PM_REMOVE：取走并分发消息；本线程只有热键消息，其余直接分发。
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001):
                if msg.message == WM_HOTKEY:
                    return True
                if msg.message == 0x0012:  # WM_QUIT
                    return False
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.005)
        return False

    def unregister(self) -> None:
        """注销热键（幂等）。"""
        if not self.registered.is_set():
            return
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        user32.UnregisterHotKey(None, 1)
        self.registered.clear()


class EstopHotkey:
    """急停热键监听器（INP-006）。

    - start()：后台线程注册热键并循环泵消息；触发即调用 on_trigger("hotkey")；
    - 注册失败 -> error 字段 + on_error 回调（明确错误，绝不静默）；
    - stop()：置停止事件并注销热键（幂等）；
    - registrar 注入点：测试提供假注册器（register/pump_once/unregister）。
    """

    def __init__(
        self,
        on_trigger: TriggerCallback,
        *,
        combo: HotkeyCombo | None = None,
        registrar: Any = None,
        on_error: ErrorCallback | None = None,
        poll_interval_ms: int = 50,
    ) -> None:
        self.combo = combo or HotkeyCombo()
        self.on_trigger = on_trigger
        self.on_error = on_error
        self.poll_interval_ms = int(poll_interval_ms)
        self._registrar = registrar if registrar is not None else HotkeyRegistrar(self.combo)
        self.error: str = ""
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 查询
    @property
    def running(self) -> bool:
        """监听线程是否仍在运行。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def failed(self) -> bool:
        """注册是否已失败（明确错误可见）。"""
        return bool(self.error)

    # ------------------------------------------------------------------ 线程
    def start(self) -> None:
        """启动监听线程；重复 start 无额外效果。"""
        if self.running:
            return
        self._stop_event.clear()
        self.error = ""
        self._thread = threading.Thread(
            target=self._run, name="estop-hotkey", daemon=True
        )
        self._thread.start()

    def stop(self, *, join_timeout: float = 2.0) -> None:
        """停止监听并注销热键；幂等。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and join_timeout > 0:
            thread.join(timeout=join_timeout)

    def trigger_for_test(self) -> None:
        """仅测试注入用：直接调用触发回调（绕过线程）。"""
        self.on_trigger("hotkey")

    # ------------------------------------------------------------------ 内部
    def _run(self) -> None:
        try:
            ok = bool(self._registrar.register())
        except Exception as exc:  # noqa: BLE001 - 注册器异常视为注册失败
            ok = False
            self._fail(f"hotkey_register_failed:{exc}")
            return
        if not ok:
            self._fail(ERROR_REGISTER_FAILED)
            return
        while not self._stop_event.is_set():
            try:
                fired = bool(self._registrar.pump_once(self.poll_interval_ms))
            except Exception:  # noqa: BLE001 - 泵异常退出，避免静默占用
                break
            if fired:
                self.on_trigger("hotkey")
                break
        try:
            self._registrar.unregister()
        except Exception:  # noqa: BLE001 - 注销失败不影响停止
            pass

    def _fail(self, code: str) -> None:
        self.error = code
        if self.on_error is not None:
            self.on_error(code)
