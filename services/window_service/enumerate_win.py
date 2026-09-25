"""Win32 窗口枚举：窗口服务唯一的 win32 调用集中地（TGT-001）。

静态守卫约定：window_service 内所有 win32gui/win32con/win32process/
win32api/pywintypes 调用只允许出现在本文件；其余模块一律通过本文件
提供的抽象入口（list_windows / foreground_source 工厂 / 进程探针）访问系统。

- list_windows()：枚举可见顶层窗口（EnumWindows + IsWindowVisible）；
- get_foreground_window()：查询当前前台窗口（GetForegroundWindow）；
- WINDOW_SERVICE_AVAILABLE：pywin32 缺失或非 Windows 平台时为 False，
  调用方据此进入"窗口服务不可用"的安全降级路径，绝不假装成功。
"""

from __future__ import annotations

from typing import Callable

from window_service.models import WindowInfo

# ---------------------------------------------------------------------------
# pywin32 可用性探测：缺失时 WINDOW_SERVICE_AVAILABLE=False（安全降级）。
# 静态守卫允许 win32 系模块 import 仅出现在本文件（window_service/enumerate_win.py）。
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 平台分支，本机 Windows 上为可用分支
    import win32api
    import win32con
    import win32gui
    import win32process
    import pywintypes  # noqa: F401  (re-export 给模块内异常处理使用)

    WINDOW_SERVICE_AVAILABLE: bool = True
except ImportError:  # pragma: no cover - 非 Windows / 未安装 pywin32
    WINDOW_SERVICE_AVAILABLE = False


#: 打开进程读取映像路径所需的权限组合（读取级，绝不请求写入类权限）。
_PROCESS_READ_ACCESS = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
if WINDOW_SERVICE_AVAILABLE:  # pragma: no branch - 常量对齐 win32con
    _PROCESS_READ_ACCESS = int(
        win32con.PROCESS_QUERY_LIMITED_INFORMATION | win32con.PROCESS_VM_READ
    )


def _query_exe_name(pid: int) -> str:
    """读取进程主程序文件名；失败（权限/进程退出）返回空串，不抛异常。"""
    handle = None
    try:
        handle = win32api.OpenProcess(_PROCESS_READ_ACCESS, False, int(pid))
        name = win32process.GetModuleFileNameEx(handle, 0)
        return str(name).replace("\\", "/").rsplit("/", 1)[-1]
    except Exception:  # noqa: BLE001 - 权限不足/进程退出等一律留空（简化点）
        return ""
    finally:
        if handle is not None:
            try:
                win32api.CloseHandle(handle)
            except Exception:  # noqa: BLE001 - 关闭句柄失败不影响结果
                pass


def _query_monitor_index(hwnd: int) -> int:
    """返回窗口所在显示器的 0 起序号；无法判定返回 -1。"""
    try:
        monitors = win32api.EnumDisplayMonitors()
        nearest = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        for index, entry in enumerate(monitors):
            if int(entry[0]) == int(nearest):
                return index
    except Exception:  # noqa: BLE001 - 枚举显示器失败不致命
        pass
    return -1


def _window_from_hwnd(hwnd: int) -> WindowInfo:
    """把裸 HWND 转成 WindowInfo 快照（所有 win32 细节集中在内部）。"""
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return WindowInfo(
        hwnd=int(hwnd),
        pid=int(pid),
        exe_name=_query_exe_name(int(pid)),
        title=str(win32gui.GetWindowText(hwnd) or ""),
        class_name=str(win32gui.GetClassName(hwnd) or ""),
        visible=bool(win32gui.IsWindowVisible(hwnd)),
        minimized=bool(win32gui.IsIconic(hwnd)),
        monitor_index=_query_monitor_index(int(hwnd)),
    )


def list_windows() -> list[WindowInfo]:
    """枚举当前全部顶层窗口（TGT-001，含不可见窗口，visible 字段区分）。

    - exe 名用 win32process.GetModuleFileNameEx，取不到则留空（权限受限时）；
    - 类名用 GetClassName；monitor_index 用 EnumDisplayMonitors 顺序；
    - 服务不可用（pywin32 缺失）时返回空列表——调用方应先看
      WINDOW_SERVICE_AVAILABLE，不要把空列表误读为"没有窗口"。
    """
    if not WINDOW_SERVICE_AVAILABLE:
        return []
    hwnds: list[int] = []

    def _collect(handle: int, _param: object) -> None:
        hwnds.append(int(handle))

    win32gui.EnumWindows(_collect, None)
    return [_window_from_hwnd(h) for h in hwnds]


def make_foreground_source() -> Callable[[], WindowInfo | None]:
    """构造"当前前台窗口快照"查询函数（TGT-003 / INP-005 使用）。

    无前台窗口（锁屏/切换会话等）返回 None；win32 调用失败时抛出
    RuntimeError，由 ForegroundMonitor 转成安全停止信号。
    """
    if not WINDOW_SERVICE_AVAILABLE:
        raise RuntimeError("window_service_unavailable")

    def _source() -> WindowInfo | None:
        hwnd = int(win32gui.GetForegroundWindow())
        if hwnd == 0:
            return None
        return _window_from_hwnd(hwnd)

    return _source


def client_rect_screen(hwnd: int) -> tuple[int, int, int, int] | None:
    """返回窗口客户区的屏幕坐标矩形 (left, top, right, bottom)（TGT-005/E2E 使用）。

    窗口无效或 win32 调用失败返回 None；win32 绑定集中在本模块。
    """
    if not WINDOW_SERVICE_AVAILABLE:
        return None
    try:
        cl, ct, cr, cb = win32gui.GetClientRect(int(hwnd))
        sx, sy = win32gui.ClientToScreen(int(hwnd), (cl, ct))
        return (int(sx), int(sy), int(sx) + int(cr - cl), int(sy) + int(cb - ct))
    except Exception:
        return None


def probe_process_access(pid: int) -> str | None:
    """进程可访问性探针（TGT-004 使用）：可读返回 None，否则返回原因。

    简化启发式：
    - 打不开 PROCESS_QUERY_LIMITED_INFORMATION 句柄 -> ``process_open_denied``；
    - 能开受限句柄但读不到映像路径（需更高权限/VM_READ）-> 判定为
      目标可能以管理员运行而本进程未提权（UIPI/完整性级别受限）
      -> ``target_elevated_uipi``。
    """
    if not WINDOW_SERVICE_AVAILABLE:
        return "window_service_unavailable"
    handle = None
    try:
        handle = win32api.OpenProcess(
            int(win32con.PROCESS_QUERY_LIMITED_INFORMATION), False, int(pid)
        )
    except Exception:  # noqa: BLE001 - 含 pywintypes.error：权限不足或进程不存在
        return "process_open_denied"
    if handle is None:
        return "process_open_denied"
    try:
        win32process.GetModuleFileNameEx(handle, 0)
    except Exception:  # noqa: BLE001 - 受限句柄读不到映像：UIPI/提权差异启发式
        return "target_elevated_uipi"
    finally:
        try:
            win32api.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - 忽略关闭失败
            pass
    return None
