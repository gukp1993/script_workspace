"""父进程看门狗（INP-007）：控制面/宿主消失即自动停止输入。

- 后台线程按 interval 探测 parent_pid 是否存活；
- 探测失败（进程不存在 / 探针异常）连续达到 max_misses 次 -> on_dead()
  （调用方应在此释放全部按键并拒绝新意图，SAFE-010）；
- 探针可注入：测试用假探活函数，零 win32 依赖；
  默认探针是本文件内唯一的 ctypes kernel32 调用（静态守卫约定）。

默认拒绝原则：无法判定存活（探针异常）一律按"不存活"处理。
"""

from __future__ import annotations

import sys
import threading
from typing import Callable

#: 进程存活探针：True=存活 / False=已退出或无法判定。
AliveProbe = Callable[[int], bool]

#: 触发回调：父进程消失/心跳超时时调用（应触发急停与全键释放）。
DeadCallback = Callable[[], None]


def probe_alive_default(parent_pid: int) -> bool:
    """默认探针：kernel32.OpenProcess + 进程退出状态检查。

    - OpenProcess 失败（不存在/无权限到完全打不开）-> 不存活；
    - 句柄可用且 WaitForSingleObject(handle, 0) == WAIT_OBJECT_0 -> 已退出；
    - 其余情况视为存活。
    """
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000
    STILL_ACTIVE: int = 259
    WAIT_OBJECT_0: int = 0
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(parent_pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        if exit_code.value != STILL_ACTIVE:
            return False
        # 心跳语义：进程对象若已处于 signaled 状态即已终止。
        if kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0:
            return False
        return True
    finally:
        kernel32.CloseHandle(handle)


class ParentWatchdog:
    """父进程看门狗线程（INP-007）。

    参数：
    - parent_pid：被监视的父进程（通常是控制面/宿主进程）；
    - interval：探测间隔（秒，>0）；
    - on_dead：父进程消失/心跳超时回调（释放全部按键 + 拒绝新意图）；
    - probe：注入探活函数（默认 kernel32 探针）；
    - max_misses：连续判定"不存活"次数达到该值即触发（心跳容忍度，>=1）；
    - sleep：注入等待函数（测试确定性；默认 time.sleep）。

    on_dead 至多触发一次；之后 watchdog 自动停止（安全状态闩存）。
    """

    def __init__(
        self,
        parent_pid: int,
        interval: float,
        on_dead: DeadCallback,
        *,
        probe: AliveProbe | None = None,
        max_misses: int = 1,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval 必须 > 0")
        if max_misses < 1:
            raise ValueError("max_misses 必须 >= 1")
        self.parent_pid = int(parent_pid)
        self.interval = float(interval)
        self.on_dead = on_dead
        self.probe: AliveProbe = probe or probe_alive_default
        self.max_misses = int(max_misses)
        self._sleep = sleep if sleep is not None else _default_sleep
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._dead_fired = threading.Event()
        self.misses: int = 0

    # ------------------------------------------------------------------ 查询
    @property
    def running(self) -> bool:
        """看门狗线程是否仍在运行。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def dead_fired(self) -> bool:
        """on_dead 是否已触发过（至多一次）。"""
        return self._dead_fired.is_set()

    # ------------------------------------------------------------------ 探测
    def poll_once(self) -> bool:
        """同步执行一次探测（线程循环体；测试可确定性调用）。

        返回 True 表示本次探测判定为"不存活"且已触发 on_dead。
        """
        if self._dead_fired.is_set():
            return False
        try:
            alive = bool(self.probe(self.parent_pid))
        except Exception:  # noqa: BLE001 - 探针异常按"不存活"处理（默认拒绝）
            alive = False
        self.misses = 0 if alive else self.misses + 1
        if self.misses >= self.max_misses:
            self._dead_fired.set()
            self.on_dead()
            return True
        return False

    # ------------------------------------------------------------------ 线程
    def start(self) -> None:
        """启动看门狗线程；重复 start 无额外效果。"""
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="parent-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self, *, join_timeout: float = 2.0) -> None:
        """停止看门狗线程；幂等（不触发 on_dead）。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and join_timeout > 0:
            thread.join(timeout=join_timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self.poll_once():
                break
            if self._stop_event.is_set():
                break
            try:
                self._sleep(self.interval)
            except Exception:  # noqa: BLE001 - sleep 注入异常视为停止请求
                break


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
