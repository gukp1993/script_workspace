"""前台窗口监视与复核（TGT-003 / INP-005）。

- ForegroundMonitor：后台线程轮询前台窗口，变化时回调 on_change(old, new)；
  轮询异常（win32 调用失败/状态不确定）-> 以 new=None 回调，触发安全停止；
- ForegroundVerifier：把"当前前台 hwnd/pid/exe"与 SessionBinding 逐项比对
  （INP-005 的真实实现），不一致即拒绝，绝不自动恢复焦点；
- BatchForegroundVerifier：按 session_id 查绑定并把整批复核暴露为
  ``verify_batch(batch)``，供 InputBroker / SendInputAdapter 注入使用。

线程约定：clock/sleep 均可注入，测试无需真实等待；stop() 幂等。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Protocol

from common.clock import Clock, MonotonicClock

from window_service.models import SessionBinding, VerifyResult, WindowInfo

#: 前台窗口查询函数：返回当前前台快照；无前台（锁屏等）返回 None；失败抛异常。
ForegroundSource = Callable[[], WindowInfo | None]

#: 前台变化回调：new 为 None 表示查询失败/状态不确定（调用方必须安全停止）。
OnChangeCallback = Callable[[WindowInfo | None, WindowInfo | None], None]

#: 默认轮询间隔（秒）。
DEFAULT_POLL_INTERVAL: float = 0.2


class ForegroundMonitor:
    """前台窗口轮询监视器（TGT-003）。

    - source 注入窗口查询函数（生产用 enumerate_win.make_foreground_source）；
    - clock/sleep 注入保证测试确定性；interval 为轮询间隔（秒）；
    - 变化检测按 WindowInfo 全字段相等比较；
    - 查询异常 -> on_change(old, None)：状态不确定必须安全停止（TGT-003），
      绝不吞掉异常后继续输入。
    """

    def __init__(
        self,
        source: ForegroundSource,
        *,
        interval: float = DEFAULT_POLL_INTERVAL,
        on_change: OnChangeCallback | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval 必须 > 0")
        self.source = source
        self.interval = float(interval)
        self.on_change = on_change
        self._clock: Clock = clock if clock is not None else MonotonicClock()
        self._sleep = sleep if sleep is not None else _default_sleep
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: WindowInfo | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 查询
    def current(self) -> WindowInfo | None:
        """最近一次轮询到的前台窗口（尚未轮询过返回 None）。"""
        with self._lock:
            return self._current

    @property
    def running(self) -> bool:
        """监视线程是否仍在运行。"""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ 轮询
    def poll_once(self) -> WindowInfo | None:
        """同步执行一次轮询（线程循环体；测试可确定性调用）。

        返回最新前台快照；查询异常时返回 None 并以 (old, None) 触发回调。
        """
        old = self.current()
        try:
            new = self.source()
        except Exception:  # noqa: BLE001 - win32 失败/状态不确定 -> 安全停止信号
            new = None
            self._publish(old, new)
            return new
        if new != old:
            self._publish(old, new)
        else:
            with self._lock:
                self._current = new
        return new

    # ------------------------------------------------------------------ 线程
    def start(self) -> None:
        """启动后台轮询线程；重复 start 无额外效果。"""
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="foreground-monitor", daemon=True
        )
        self._thread.start()

    def stop(self, *, join_timeout: float = 2.0) -> None:
        """停止轮询线程；幂等。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and join_timeout > 0:
            thread.join(timeout=join_timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            if self._stop_event.is_set():
                break
            try:
                self._sleep(self.interval)
            except Exception:  # noqa: BLE001 - sleep 注入异常视为停止请求
                break

    def _publish(self, old: WindowInfo | None, new: WindowInfo | None) -> None:
        with self._lock:
            self._current = new
        if self.on_change is not None:
            self.on_change(old, new)


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


# ---------------------------------------------------------------------------
# 前台复核（INP-005）
# ---------------------------------------------------------------------------


class ForegroundVerifier:
    """把当前前台窗口与会话绑定逐项比对（INP-005 真实实现）。

    校验项（全部一致才通过）：
    - 当前存在前台窗口；
    - hwnd 与绑定一致（窗口句柄复用/换窗即拒绝）；
    - pid 与绑定一致（进程重启/换实例即拒绝）；
    - exe 名与绑定一致（绑定记录了 exe 时；大小写不敏感）。
    """

    def __init__(self, foreground_source: ForegroundSource) -> None:
        self.foreground_source = foreground_source

    def verify(self, binding: SessionBinding) -> VerifyResult:
        """校验当前前台是否仍是绑定实例；任一不符即拒绝。"""
        try:
            window = self.foreground_source()
        except Exception:  # noqa: BLE001 - 查询失败视为状态不确定
            return VerifyResult.fail("foreground_query_failed")
        if window is None:
            return VerifyResult.fail("no_foreground_window")
        reasons: list[str] = []
        if int(window.hwnd) != int(binding.hwnd):
            reasons.append("hwnd_mismatch")
        if int(window.pid) != int(binding.pid):
            reasons.append("pid_mismatch")
        if binding.exe_name and window.exe_name:
            if window.exe_name.strip().lower() != binding.exe_name.strip().lower():
                reasons.append("exe_mismatch")
        if reasons:
            return VerifyResult.fail(*reasons)
        return VerifyResult.pass_ok()


# ---------------------------------------------------------------------------
# 批次级复核胶水：供 InputBroker / SendInputAdapter 以 verify_batch(batch) 注入
# ---------------------------------------------------------------------------


class _BatchLike(Protocol):
    """批次 duck-typing：只要求提供 session_id（input_broker.InputBatch 满足）。"""

    @property
    def session_id(self) -> str:  # pragma: no cover - 结构性协议
        ...


class BatchForegroundVerifier:
    """按 session_id 查绑定并复核整批的前台一致性。

    依赖注入：bindings 为 session_id -> SessionBinding 注册表（人工确认
    实例后写入）；foreground 为任一具有 ``verify(binding)`` 的复核器。
    任何 object（含 input_broker.InputBatch）只要带 session_id 即可复核。
    """

    def __init__(
        self,
        bindings: dict[str, SessionBinding],
        foreground: Any,
    ) -> None:
        self.bindings = bindings
        self.foreground = foreground

    def verify_batch(self, batch: _BatchLike) -> VerifyResult:
        """整批复核：会话未绑定或前台不符都给出机器可读原因。"""
        binding = self.bindings.get(str(batch.session_id))
        if binding is None:
            return VerifyResult.fail("session_not_bound")
        return self.foreground.verify(binding)
