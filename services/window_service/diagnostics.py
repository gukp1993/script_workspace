"""目标窗口可访问性诊断（TGT-004 简化版）。

在人工确认目标实例前/会话启动前，检查目标窗口是否存在权限或 UIPI
障碍，并把**所有**发现的问题以机器可读原因返回：
- 绝不报告假成功：探针不可用、进程打不开、疑似提权差异都如实列出；
- 简化点：不做完整 GetTokenInformation/完整性级别比对，仅用
  "受限句柄可开 + 完整映像路径读不到"启发式判定 UIPI 受限；
  也不做 IsWindowEnabled/GetForegroundWindow 的交互性深度检测（M2 补齐）。

本模块不做任何 win32 调用——全部经由 enumerate_win 注入的探针完成，
探针可替换以便单元测试（零系统依赖）。
"""

from __future__ import annotations

from typing import Callable

from window_service.enumerate_win import (
    WINDOW_SERVICE_AVAILABLE,
    probe_process_access,
)
from window_service.models import WindowInfo

#: 进程访问探针：pid -> 失败原因或 None（可注入替换）。
AccessProbe = Callable[[int], str | None]


def check_access(window: WindowInfo, *, probe: AccessProbe | None = None) -> list[str]:
    """诊断目标窗口的权限/UIPI/可见性问题，返回全部问题原因（空列表 = 通过）。

    - ``window_not_visible``：目标窗口不可见（前台自动化无法继续）；
    - ``window_minimized``：目标窗口最小化（需人工恢复后再确认）；
    - ``process_open_denied``：无法打开进程读取句柄（权限不足或进程退出）；
    - ``target_elevated_uipi``：目标疑似以管理员运行而本进程未提权
      （UIPI 受限，SendInput 会被系统静默丢弃——必须先解决提权对齐）；
    - ``window_service_unavailable``：窗口服务不可用（pywin32 缺失等）。
    """
    reasons: list[str] = []
    effective_probe = probe or probe_process_access

    if not window.visible:
        reasons.append("window_not_visible")
    if window.minimized:
        reasons.append("window_minimized")

    try:
        probe_reason = effective_probe(window.pid)
    except Exception:  # noqa: BLE001 - 探针异常视为状态不确定，不报告假成功
        probe_reason = "process_probe_failed"
    if probe_reason:
        reasons.append(probe_reason)

    if not WINDOW_SERVICE_AVAILABLE and "window_service_unavailable" not in reasons:
        reasons.append("window_service_unavailable")

    return reasons
