"""test_kit——测试套件公共 fake（TST-001：M0 基础 + M1 整合）。

目标：单测无需真实桌面即可模拟时间、窗口、前台焦点、帧与输入结果。
- FakeClock：来自 common 包的确定性时钟（原样 re-export）；
- FakeCaptureSource / make_frame：来自 capture_api 的假采集源与合成帧
  （原样 re-export，M1 整合单一入口）；
- FakeForegroundContext：可编程前台上下文，模拟聚焦/失焦/错窗/换绑；
- FakeWindowSource：可编程窗口源（M1 新增），伪造 WindowInfo 列表 +
  可切换前台，直接驱动 ForegroundMonitor / ForegroundVerifier /
  BatchForegroundVerifier / SessionLock 等以 ForegroundSource 注入的组件；
- make_fake_sink / make_click_batch / make_session_binding：便捷工厂。

本包只做 re-export 与组合，绝不修改被测包；也不 import 任何系统级设施。
"""

from typing import Any

from common import FakeClock  # noqa: F401  (re-export)

from capture_api.fake import FakeCaptureSource, make_frame  # noqa: F401  (re-export)

from input_broker import (  # noqa: F401  (re-export)
    FakeInputSink,
    InputBatch,
    InputIntent,
    make_batch,
    make_intent,
)
from policy_engine import ForegroundContext
from window_service.models import SessionBinding, WindowInfo


class FakeForegroundContext(ForegroundContext):
    """可编程前台上下文：用方法切换前台状态，模拟失焦/错窗/进程换绑。"""

    def focus(
        self, target_id: str, *, pid: int | None = None, hwnd: int | None = None
    ) -> None:
        """模拟某个目标窗口获得前台焦点。"""
        self.target_id = target_id
        self.pid = pid
        self.hwnd = hwnd

    def blur(self) -> None:
        """模拟失焦：前台无目标（evaluator 应拒绝任何真实输入）。"""
        self.target_id = None
        self.pid = None
        self.hwnd = None

    def switch_to(self, target_id: str, *, pid: int | None = None) -> None:
        """模拟切到（可能错误的）其他窗口。"""
        self.focus(target_id, pid=pid)

    def bind(
        self,
        *,
        target_id: str | None = None,
        pid: int | None = None,
        session_id: str | None = None,
    ) -> None:
        """设置会话绑定的期望值（evaluator 逐项比对）。"""
        if target_id is not None:
            self.expected_target_id = target_id
        if pid is not None:
            self.expected_pid = pid
        if session_id is not None:
            self.expected_session_id = session_id


def make_fake_sink(**kwargs: Any) -> FakeInputSink:
    """FakeInputSink 便捷构造（参数原样透传）。"""
    return FakeInputSink(**kwargs)


def make_click_batch(
    session_id: str,
    target_id: str,
    count: int = 1,
    *,
    clock: Any,
    x: int = 0,
    y: int = 0,
    ttl_ms: float = 500.0,
    batch_ttl_ms: float = 1000.0,
    cause: str = "test",
) -> InputBatch:
    """构造包含 count 个 click 意图的批次（测试常用捷径）。"""
    intents = [
        make_intent(
            session_id,
            target_id,
            "click",
            {"button": "left", "x": x, "y": y},
            clock=clock,
            ttl_ms=ttl_ms,
            cause=cause,
        )
        for _ in range(count)
    ]
    return make_batch(
        session_id, target_id, intents, clock=clock, ttl_ms=batch_ttl_ms, cause=cause
    )


# ---------------------------------------------------------------------------
# M1 新增：窗口源与绑定工厂（TGT-003 / TGT-009 / INP-005 测试注入点）
# ---------------------------------------------------------------------------


class FakeWindowSource:
    """可编程窗口源：伪造 WindowInfo 列表 + 可切换前台（ForegroundSource 协议）。

    生产代码中 ``ForegroundSource = Callable[[], WindowInfo | None]``
    （window_service.foreground），本类即为该协议的可编程实现：

    - ``make_window(...)``：登记（或按 hwnd 整体替换）一个窗口快照；
    - ``focus(hwnd)``：把前台切到某个已登记窗口；``focus(None)`` 模拟
      锁屏/无前台；再 ``focus`` 到未登记 hwnd 视为"前台是未知窗口"；
    - ``fail_with(exc)``：注入查询异常（ForegroundMonitor 会转成安全停止
      信号，ForegroundVerifier 视为状态不确定）；
    - 实例可直接作为 ForegroundMonitor / ForegroundVerifier /
      SessionLock / BatchForegroundVerifier 的前台源注入，零 win32 依赖。
    """

    def __init__(self) -> None:
        self._windows: dict[int, WindowInfo] = {}
        self._foreground_hwnd: int | None = None
        self._error: Exception | None = None
        self.calls = 0

    # ------------------------------------------------------------------ 编排
    def make_window(
        self,
        *,
        hwnd: int,
        pid: int,
        exe_name: str = "app.exe",
        title: str = "",
        class_name: str = "",
        visible: bool = True,
        minimized: bool = False,
        monitor_index: int = 0,
        focus: bool = False,
    ) -> WindowInfo:
        """登记一个窗口快照；hwnd 相同则整体替换（模拟窗口句柄复用/换实例）。"""
        window = WindowInfo(
            hwnd=int(hwnd),
            pid=int(pid),
            exe_name=exe_name,
            title=title,
            class_name=class_name,
            visible=visible,
            minimized=minimized,
            monitor_index=monitor_index,
        )
        self._windows[int(hwnd)] = window
        if focus:
            self._foreground_hwnd = int(hwnd)
        return window

    def focus(self, hwnd: int | None) -> None:
        """切换前台；None 表示无前台（锁屏/用户切换/RDP 断开）。"""
        self._foreground_hwnd = None if hwnd is None else int(hwnd)

    def fail_with(self, error: Exception | None) -> None:
        """注入/清除查询异常（下一次调用起生效）。"""
        self._error = error

    # ------------------------------------------------------------------ 查询
    @property
    def foreground_hwnd(self) -> int | None:
        """当前前台 hwnd（测试断言用）。"""
        return self._foreground_hwnd

    def windows(self) -> list[WindowInfo]:
        """已登记的全部窗口快照（按 hwnd 排序，稳定断言用）。"""
        return [self._windows[h] for h in sorted(self._windows)]

    def get(self, hwnd: int) -> WindowInfo | None:
        """按 hwnd 取窗口快照（未登记返回 None）。"""
        return self._windows.get(int(hwnd))

    # -------------------------------------------------- ForegroundSource 协议
    def __call__(self) -> WindowInfo | None:
        """返回当前前台窗口快照；无前台返回 None；注入异常时抛出。"""
        self.calls += 1
        if self._error is not None:
            raise self._error
        if self._foreground_hwnd is None:
            return None
        return self._windows.get(self._foreground_hwnd)


def make_session_binding(
    session_id: str,
    target_id: str,
    *,
    pid: int | None = None,
    hwnd: int | None = None,
    instance_token: str = "tok-1",
    exe_name: str = "app.exe",
    window: WindowInfo | None = None,
) -> SessionBinding:
    """构造 SessionBinding（TGT-009）。

    - 传入 ``window`` 时从窗口快照派生 pid/hwnd/exe_name（模拟"人工确认
      该实例后绑定"）；
    - 不传 window 时按 pid/hwnd 显式参数构造（默认 pid=1000/hwnd=100）。
    """
    if window is not None:
        pid = window.pid if pid is None else pid
        hwnd = window.hwnd if hwnd is None else hwnd
        exe_name = window.exe_name or exe_name
    return SessionBinding(
        session_id=session_id,
        target_id=target_id,
        pid=int(pid if pid is not None else 1000),
        hwnd=int(hwnd if hwnd is not None else 100),
        instance_token=instance_token,
        exe_name=exe_name,
    )
