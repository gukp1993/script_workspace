"""window_service——目标窗口服务（E04：TGT 系列）。

TGT-001：list_windows 枚举顶层窗口（win32 调用集中在 enumerate_win）；
TGT-002：match_targets 按目标档案匹配窗口（多命中即歧义，不自动挑选）；
TGT-003：ForegroundMonitor 前台轮询监视（异常 -> 安全停止信号）；
TGT-004：diagnostics.check_access 权限/UIPI 诊断（简化版，不报告假成功）；
TGT-009：SessionLock 会话-实例锁（PID 消失/HWND 变化即失效，需人工重确认）；
INP-005：ForegroundVerifier 前台复核的真实实现。

安全边界：本服务只"看"窗口，绝不移动/关闭/注入任何窗口或进程；
所有 win32 系模块 import 集中在 ``enumerate_win``（静态守卫约定）。
"""

from window_service.diagnostics import check_access
from window_service.enumerate_win import (
    WINDOW_SERVICE_AVAILABLE,
    list_windows,
    make_foreground_source,
    probe_process_access,
)
from window_service.foreground import (
    BatchForegroundVerifier,
    ForegroundMonitor,
    ForegroundSource,
    ForegroundVerifier,
)
from window_service.matcher import (
    MatchResult,
    match_targets,
    match_targets_detailed,
)
from window_service.models import SessionBinding, VerifyResult, WindowInfo
from window_service.session_lock import NEEDS_MANUAL_RECONFIRM, SessionLock

__all__ = [
    "NEEDS_MANUAL_RECONFIRM",
    "WINDOW_SERVICE_AVAILABLE",
    "BatchForegroundVerifier",
    "ForegroundMonitor",
    "ForegroundSource",
    "ForegroundVerifier",
    "MatchResult",
    "SessionBinding",
    "SessionLock",
    "VerifyResult",
    "WindowInfo",
    "check_access",
    "list_windows",
    "make_foreground_source",
    "match_targets",
    "match_targets_detailed",
    "probe_process_access",
]
