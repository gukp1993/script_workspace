"""控制面统一业务异常（CTL-001~009 共用）。

HTTP 层把 :class:`ControlPlaneError` 渲染为统一响应体::

    {"detail": {"error": <错误码>, "message": <中文描述>, ...extra, "issues": [...]}}

其中 ``issues`` 为 domain_model 的 :class:`Issue` 列表（含规则 ID 与字段路径），
仅在校验失败（422）时出现。
"""

from __future__ import annotations

from typing import Any


class ControlPlaneError(Exception):
    """带 HTTP 状态码、错误码与中文消息的业务异常。

    Attributes:
        status_code: HTTP 状态码（401/403/404/409/413/415/422/500...）。
        code:        机器可读错误码，如 ``manual_gate_required``。
        message:     人类可读中文描述（不包含令牌等敏感细节）。
        extra:       附加结构化字段（如 ``current_version``）。
        issues:      domain_model 的 Issue 列表（校验失败时携带规则 ID 与字段路径）。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        extra: dict[str, Any] | None = None,
        issues: list[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra: dict[str, Any] = dict(extra or {})
        self.issues: list[Any] = list(issues or [])
