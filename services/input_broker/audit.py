"""安全审计（INP-010）：把安全相关结果写入可验证的哈希链轨迹。

写入 trace_format.JsonlTraceWriter（TRC-001/002），事件类型映射：
- 策略拒绝            -> policy_decision（payload.outcome="denied"）；
- 急停                -> estop（source/reason/released 键数）；
- 失焦 / 预算耗尽 /
  按键释放 / 其他异常 -> anomaly（kind 区分，payload 带细节）。

审计是"事后不可抵赖"层：只追加、不改写；时间取注入时钟的单调值。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from common.clock import Clock, MonotonicClock
from trace_format.writer import JsonlTraceWriter

from input_broker.intents import InputIntent


class AuditLogger:
    """安全事件审计器（INP-010）。

    writer 注入 JsonlTraceWriter；clock 注入单调时钟（默认生产时钟）。
    每个方法返回写入的 TraceEvent，便于调用方串联 correlation_id。
    """

    def __init__(self, writer: JsonlTraceWriter, *, clock: Clock | None = None) -> None:
        self.writer = writer
        self.clock: Clock = clock if clock is not None else MonotonicClock()

    # ------------------------------------------------------------------ 事件
    def log_policy_denied(
        self,
        batch_id: str,
        session_id: str,
        reasons: Sequence[str],
        *,
        correlation_id: str = "",
        target_id: str = "",
    ) -> Any:
        """策略拒绝：逐条原因进入 policy_decision 事件（机器可读）。"""
        return self.writer.append(
            "policy_decision",
            ts_monotonic=float(self.clock.now()),
            correlation_id=correlation_id,
            session_id=session_id,
            payload={
                "outcome": "denied",
                "batch_id": batch_id,
                "target_id": target_id,
                "reasons": [str(r) for r in reasons],
            },
        )

    def log_estop(
        self,
        source: str,
        reason: str = "",
        *,
        released_count: int = 0,
        session_id: str = "",
    ) -> Any:
        """急停事件：来源/原因/释放键数一次性入链。"""
        return self.writer.append(
            "estop",
            ts_monotonic=float(self.clock.now()),
            session_id=session_id,
            payload={
                "source": str(source),
                "reason": str(reason),
                "released_count": int(released_count),
            },
        )

    def log_focus_lost(
        self,
        *,
        session_id: str = "",
        expected: Mapping[str, Any] | None = None,
        actual: Mapping[str, Any] | None = None,
        correlation_id: str = "",
    ) -> Any:
        """失焦异常（SAFE-001 关联）：期望与实际前台标识入链。"""
        return self.writer.append(
            "anomaly",
            ts_monotonic=float(self.clock.now()),
            correlation_id=correlation_id,
            session_id=session_id,
            payload={
                "kind": "focus_lost",
                "expected": dict(expected or {}),
                "actual": dict(actual or {}),
            },
        )

    def log_budget_exhausted(
        self, *, session_id: str = "", detail: Mapping[str, Any] | None = None
    ) -> Any:
        """预算耗尽异常（SAFE-004 关联）。"""
        return self.writer.append(
            "anomaly",
            ts_monotonic=float(self.clock.now()),
            session_id=session_id,
            payload={"kind": "budget_exhausted", "detail": dict(detail or {})},
        )

    def log_release(
        self,
        released: Iterable[InputIntent],
        *,
        cause: str,
        session_id: str = "",
    ) -> Any:
        """按键释放结果：释放的键名列表与触发路径入链（无卡键证据）。"""
        keys = sorted(
            str(getattr(i, "payload", {}).get("key", ""))
            for i in released
        )
        return self.writer.append(
            "anomaly",
            ts_monotonic=float(self.clock.now()),
            session_id=session_id,
            payload={"kind": "keys_released", "cause": str(cause), "keys": keys},
        )

    def log_anomaly(self, kind: str, payload: Mapping[str, Any] | None = None, *, session_id: str = "") -> Any:
        """通用异常事件（状态不确定、探针失败等）。"""
        merged = {"kind": str(kind)}
        merged.update(dict(payload or {}))
        return self.writer.append(
            "anomaly",
            ts_monotonic=float(self.clock.now()),
            session_id=session_id,
            payload=merged,
        )
