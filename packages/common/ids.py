"""ID 生成（ENG-005）。

- new_id()：通用唯一短 ID。
- new_session_id() / new_correlation_id()：语义化前缀，便于日志检索。
"""

from __future__ import annotations

import uuid


def _short_uuid() -> str:
    return uuid.uuid4().hex


def new_id(prefix: str = "id") -> str:
    """生成 '<prefix>-<32位hex>' 形式的唯一 ID。"""
    return f"{prefix}-{_short_uuid()}"


def new_session_id() -> str:
    return new_id("sess")


def new_correlation_id() -> str:
    return new_id("cid")
