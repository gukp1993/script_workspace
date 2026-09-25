"""TraceEvent：运行轨迹事件与链式哈希（TRC-001）。

- 事件类型覆盖：帧、感知、状态迁移、意图、策略决策、执行、急停、异常；
- 哈希链：hash = sha256(上一事件 hash + 本事件规范化 JSON)，
  首事件以上一 hash = GENESIS_HASH（全 0）开始；
- 规范化 JSON：sort_keys + 紧凑分隔符 + ensure_ascii=False，
  保证"同输入序列两次生成 -> 哈希序列完全一致"（FSM-012 契约前奏）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


#: 轨迹格式版本（TRC-001：版本化格式）。
TRACE_FORMAT_VERSION: str = "1"

#: 首事件的 prev_hash（64 个 0）。
GENESIS_HASH: str = "0" * 64


class TraceEventType(str, Enum):
    """轨迹事件类型（TRC-001）。"""

    FRAME_CAPTURED = "frame_captured"
    PERCEPTION_SNAPSHOT = "perception_snapshot"
    STATE_TRANSITION = "state_transition"
    INTENT_ISSUED = "intent_issued"
    POLICY_DECISION = "policy_decision"
    EXECUTED = "executed"
    ESTOP = "estop"
    ANOMALY = "anomaly"


#: 全部合法事件类型的值集合。
EVENT_TYPE_VALUES: frozenset[str] = frozenset(t.value for t in TraceEventType)

#: 哈希输入包含的事件字段（不含 hash 自身）。
_HASHED_FIELDS: tuple[str, ...] = (
    "seq",
    "ts_monotonic",
    "correlation_id",
    "session_id",
    "type",
    "payload",
    "prev_hash",
)


def canonical_json(data: Mapping[str, Any]) -> str:
    """规范化 JSON：键排序、紧凑分隔符、不转义非 ASCII（确定性序列化）。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_event_hash(data: Mapping[str, Any]) -> str:
    """计算事件哈希：sha256(prev_hash + 规范化 JSON(不含 hash 字段))。

    data 必须包含 prev_hash 与 _HASHED_FIELDS 的其余字段。
    """
    hashed = {k: data[k] for k in _HASHED_FIELDS}
    material = f"{data['prev_hash']}{canonical_json(hashed)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TraceEvent:
    """单条轨迹事件（TRC-001）。"""

    seq: int
    ts_monotonic: float
    correlation_id: str
    session_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    hash: str = ""

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        """转为可 JSON 序列化的 dict（写入 JSONL 用）。"""
        data: dict[str, Any] = {
            "seq": self.seq,
            "ts_monotonic": self.ts_monotonic,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "type": self.type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }
        if include_hash:
            data["hash"] = self.hash
        return data

    def compute_hash(self) -> str:
        """按本事件当前内容重算哈希（校验时与已存 hash 比对）。"""
        return compute_event_hash(self.to_dict(include_hash=False))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TraceEvent":
        """从 dict 反序列化；字段缺失或类型不可转换时抛 ValueError。"""
        required = set(_HASHED_FIELDS) | {"hash"}
        missing = required - set(data)
        if missing:
            raise ValueError(f"事件缺少字段: {sorted(missing)}")
        try:
            payload = data["payload"]
            if not isinstance(payload, dict):
                raise ValueError("payload 必须是对象")
            return cls(
                seq=int(data["seq"]),
                ts_monotonic=float(data["ts_monotonic"]),
                correlation_id=str(data["correlation_id"]),
                session_id=str(data["session_id"]),
                type=str(data["type"]),
                payload=dict(payload),
                prev_hash=str(data["prev_hash"]),
                hash=str(data["hash"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"事件字段类型非法: {exc}") from exc

    @classmethod
    def create(
        cls,
        *,
        seq: int,
        prev_hash: str,
        ts_monotonic: float,
        type: str | TraceEventType,
        correlation_id: str = "",
        session_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> "TraceEvent":
        """工厂：按链式规则自动计算 hash（seq/prev_hash 由调用方或 writer 提供）。"""
        data: dict[str, Any] = {
            "seq": int(seq),
            "ts_monotonic": float(ts_monotonic),
            "correlation_id": str(correlation_id),
            "session_id": str(session_id),
            "type": str(getattr(type, "value", type)),
            "payload": dict(payload or {}),
            "prev_hash": str(prev_hash),
        }
        return cls(hash=compute_event_hash(data), **data)
