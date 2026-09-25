"""窗口服务的数据模型（TGT-001/002/003/009）。

- WindowInfo：一次窗口枚举/查询得到的只读窗口快照；
- SessionBinding：会话与目标窗口实例的绑定（人工确认后建立，TGT-009）；
- VerifyResult：前台/绑定复核结果（INP-005），失败时必须给出机器可读原因。

安全约定：
- 绑定必须来自人工确认的实例（CAP-012：同名窗口不自动挑选）；
- VerifyResult 默认不通过（ok=False 且带原因），杜绝"假成功"。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WindowInfo:
    """单个顶层窗口的只读快照（TGT-001）。

    Attributes:
        hwnd:          窗口句柄（Windows 原生 HWND 数值）。
        pid:           拥有该窗口的进程 ID。
        exe_name:      进程主程序文件名（不含路径）；取不到时为空串。
        title:         窗口标题。
        class_name:    Win32 窗口类名；取不到时为空串。
        visible:       窗口是否可见（IsWindowVisible）。
        minimized:     窗口是否最小化（IsIconic）。
        monitor_index: 所在显示器的序号（EnumDisplayMonitors 顺序，0 起）；
                       无法判定时为 -1。
    """

    hwnd: int
    pid: int
    exe_name: str
    title: str
    class_name: str
    visible: bool
    minimized: bool
    monitor_index: int

    def describe(self) -> str:
        """人类可读的一行描述（诊断与人工确认界面复用）。"""
        return (
            f"hwnd=0x{self.hwnd:08X} pid={self.pid} exe={self.exe_name!r} "
            f"title={self.title!r} class={self.class_name!r} "
            f"visible={self.visible} minimized={self.minimized} "
            f"monitor={self.monitor_index}"
        )


@dataclass(frozen=True)
class SessionBinding:
    """会话与目标窗口实例的绑定（TGT-009）。

    绑定在人工确认目标实例之后建立；instance_token 用于区分
    "同一窗口的不同绑定代次"——重新确认必须产生新 token，
    旧 token 的绑定一经失效不得自动复活。

    Attributes:
        session_id:     所属会话 ID。
        target_id:      目标档案 ID（TargetProfile.target_id）。
        pid:            绑定的目标进程 ID。
        hwnd:           绑定的目标窗口句柄。
        instance_token: 实例代次令牌（人工确认时生成）。
        exe_name:       绑定时记录的进程映像名（可空；用于 exe 复核）。
    """

    session_id: str
    target_id: str
    pid: int
    hwnd: int
    instance_token: str
    exe_name: str = ""


@dataclass(frozen=True)
class VerifyResult:
    """复核结果（INP-005 / TGT-009）。

    ok=False 时 reasons 至少包含一条机器可读原因（如
    ``hwnd_mismatch`` / ``pid_gone`` / ``no_foreground_window``）；
    ok=True 时 reasons 为空。默认构造即"不通过"，杜绝假成功。
    """

    ok: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def pass_ok(cls) -> "VerifyResult":
        """构造通过结果。"""
        return cls(ok=True, reasons=())

    @classmethod
    def fail(cls, *reasons: str) -> "VerifyResult":
        """构造失败结果；至少要有一条原因（不报告假成功）。"""
        cleaned = tuple(r for r in reasons if r)
        if not cleaned:
            cleaned = ("unspecified_mismatch",)
        return cls(ok=False, reasons=cleaned)

    @property
    def reason_text(self) -> str:
        """把原因合并为单行字符串（审计/日志用）。"""
        return ";".join(self.reasons)
