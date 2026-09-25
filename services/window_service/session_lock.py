"""会话-目标实例锁（TGT-009）。

绑定一经建立，运行期间必须持续满足：
- 目标进程仍然存活（PID 未消失）；
- 前台窗口仍是绑定的 HWND（未换窗/未失焦到其他实例）。

任一条件被打破 -> 锁失效并**闩存（latch）**：即使表面条件恢复，
也必须重新人工确认（重新 bind 生成新的 instance_token）后才能解锁。
"""

from __future__ import annotations

from typing import Callable

from window_service.models import SessionBinding, VerifyResult, WindowInfo

#: 进程存活探针：pid 存活返回 True；无法判定时必须返回 False（默认拒绝）。
ProcessAliveProbe = Callable[[int], bool]

#: 失效原因 -> 必须重新人工确认的统一原因码。
NEEDS_MANUAL_RECONFIRM: str = "needs_manual_reconfirm"


def _default_process_alive(pid: int) -> bool:
    """默认探针：经由 enumerate_win 的权限探针判断（读得到映像即存活）。"""
    from window_service.enumerate_win import probe_process_access

    return probe_process_access(pid) is None


class SessionLock:
    """会话与目标窗口实例的活性锁（TGT-009）。

    - bind(binding)：人工确认实例后建立/重置绑定；
    - verify()：进程消失或 HWND 变化 -> VerifyResult.fail 并闩存失效状态，
      后续 verify 一律返回 ``needs_manual_reconfirm``，直到重新 bind；
    - 探针注入：process_alive / foreground_source 均可替换，测试零 win32 依赖。
    """

    def __init__(
        self,
        *,
        process_alive: ProcessAliveProbe | None = None,
        foreground_source: Callable[[], WindowInfo | None] | None = None,
    ) -> None:
        self._process_alive = process_alive or _default_process_alive
        self._foreground_source = foreground_source
        self._binding: SessionBinding | None = None
        self._invalidated = False
        self._last_reasons: tuple[str, ...] = ()

    # ------------------------------------------------------------------ 绑定
    @property
    def binding(self) -> SessionBinding | None:
        """当前绑定（未绑定为 None）。"""
        return self._binding

    @property
    def invalidated(self) -> bool:
        """是否已失效待人工重确认。"""
        return self._invalidated

    def bind(self, binding: SessionBinding) -> None:
        """建立（或人工重确认后更新）绑定；清除失效闩存。"""
        self._binding = binding
        self._invalidated = False
        self._last_reasons = ()

    def unbind(self) -> None:
        """解除绑定（会话结束时调用）；幂等。"""
        self._binding = None
        self._invalidated = False
        self._last_reasons = ()

    # ------------------------------------------------------------------ 校验
    def verify(self) -> VerifyResult:
        """校验绑定实例仍存活且前台一致；失效即闩存，需人工重新确认。"""
        if self._invalidated:
            return VerifyResult.fail(NEEDS_MANUAL_RECONFIRM, *self._last_reasons)
        if self._binding is None:
            return VerifyResult.fail("not_bound")

        binding = self._binding
        # 1) 目标 PID 是否仍然存活（探针无法判定一律视为死亡：默认拒绝）。
        try:
            alive = bool(self._process_alive(binding.pid))
        except Exception:  # noqa: BLE001 - 探针异常视为状态不确定
            alive = False
        if not alive:
            return self._invalidate("pid_gone")

        # 2) 前台窗口是否仍是绑定 HWND（未注入前台源时跳过此项）。
        if self._foreground_source is not None:
            try:
                window = self._foreground_source()
            except Exception:  # noqa: BLE001 - 查询失败视为状态不确定
                window = None
            if window is None:
                return self._invalidate("no_foreground_window")
            if int(window.hwnd) != int(binding.hwnd):
                return self._invalidate("hwnd_changed")
            if int(window.pid) != int(binding.pid):
                return self._invalidate("pid_changed")

        return VerifyResult.pass_ok()

    # ------------------------------------------------------------------ 内部
    def _invalidate(self, *reasons: str) -> VerifyResult:
        """闩存失效状态：只有重新 bind（人工确认）才能解除。"""
        self._invalidated = True
        self._last_reasons = tuple(reasons)
        return VerifyResult.fail(*reasons)
