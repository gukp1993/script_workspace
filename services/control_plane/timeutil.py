"""时间工具（控制面内部共用）。

统一输出带时区的 UTC ISO-8601 字符串，用于对象 ``meta.updated_at``、
会话历史与事件时间戳；单调计时由 ``common.clock`` 提供，此处只负责墙钟格式化。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（毫秒精度，带时区）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
