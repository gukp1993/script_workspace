"""运行控制：pause / cancel / 安全停止（FSM-006）。

安全语义：
- ``pause()``：暂停后 tick 只推进计数，不评估守卫、不迁移、不产生意图；
- ``resume()``：恢复前要求重新 ``verify()``（注入 callable）；返回 False
  时拒绝恢复并抛出 :class:`RuntimeError`，运行保持暂停；
- ``cancel()``：取消后 tick 恒返回 ``stopped=True / stop_reason="cancelled"``，
  不再产生任何意图，且不可恢复。
"""

from __future__ import annotations

from typing import Callable

__all__ = ["RuntimeController"]


class RuntimeController:
    """MachineRuntime 的运行状态旗标与恢复校验。"""

    def __init__(self, verify: Callable[[], bool] | None = None) -> None:
        self._verify = verify
        self.paused: bool = False
        self.cancelled: bool = False

    def pause(self) -> None:
        """暂停运行；已取消的运行不能再暂停。"""
        if self.cancelled:
            raise RuntimeError("运行已取消，不能再暂停")
        self.paused = True

    def resume(self) -> None:
        """恢复运行；恢复前必须通过 verify 校验（安全停止后的再启动闸门）。"""
        if self.cancelled:
            raise RuntimeError("运行已取消，无法恢复（cancel 是终态操作）")
        if not self.paused:
            return
        if self._verify is not None and not self._verify():
            raise RuntimeError("恢复被拒绝：verify() 返回 False（恢复前必须重新验证）")
        self.paused = False

    def cancel(self) -> None:
        """安全停止：取消后 tick 不再产生意图，且不可恢复。"""
        self.cancelled = True
        self.paused = False
