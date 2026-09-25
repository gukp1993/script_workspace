"""启动恢复扫描（CTL-009 基础版）。

进程启动时扫描会话目录，把所有非终态（created/running/paused/resumed）
会话标记为 ``interrupted`` 并发布恢复事件，供 UI 与轨迹提示"上次运行未
正常结束"。完整版恢复（安全清理、Broker 对账、轨迹封口）在 M2/M3 由
INP-007 / TRC-002 覆盖。
"""

from __future__ import annotations

from control_plane.errors import ControlPlaneError
from control_plane.sessions import TERMINAL_STATES, SessionManager


def recover_interrupted_sessions(manager: SessionManager) -> list[str]:
    """把全部非终态会话标记 interrupted；返回被恢复的会话 ID 列表。

    单个会话处理失败（如文件在扫描间隙被删除）不阻断整体恢复。
    """
    recovered: list[str] = []
    for record in manager.list_sessions():
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or record.get("state") in TERMINAL_STATES:
            continue
        try:
            manager.mark_interrupted(session_id)
        except ControlPlaneError:
            continue
        recovered.append(session_id)
    return recovered
