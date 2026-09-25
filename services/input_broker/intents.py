"""InputIntent / InputBatch：所有动作的统一入口契约（INP-001）。

M0 说明：本模块自带最小输入结构（dataclass，duck-typing），字段与
domain_model 中的 InputIntent / InputBatch 对齐，M1 契约测试统一；
本包不 import domain_model（与并行开发解耦）。

安全约定（默认拒绝）：
- 运行时（状态机等）只产生意图，绝不直接调用任何 OS 输入 API；
- 每个意图 / 每个批次都带单调时间 TTL，过期即拒绝、不补发；
- kind -> payload 字段约定：
    key_down / key_up : {"key": str}                      （如 "a"、"ctrl"）
    click             : {"button": "left"|"right"|"middle", "x": int, "y": int}
    move              : {"x": int, "y": int, "duration_ms"?: int}
    wheel             : {"delta": int, "delta_ms"?: int}   （delta 正值向上滚）
    wait              : {"duration_ms": int}
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from common.clock import Clock
from common.ids import new_id

#: 意图默认 TTL（毫秒）：前台自动化意图生命周期极短，过期即拒绝。
DEFAULT_INTENT_TTL_MS: float = 500.0

#: 批次默认 TTL（毫秒）。
DEFAULT_BATCH_TTL_MS: float = 1000.0


class IntentKind(str, Enum):
    """受支持的意图种类（INP-001：键盘、鼠标、滚轮、等待）。"""

    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    CLICK = "click"
    MOVE = "move"
    WHEEL = "wheel"
    WAIT = "wait"


_KIND_VALUES: frozenset[str] = frozenset(k.value for k in IntentKind)

#: 等待类意图：不消耗"动作数"预算，也不是真实输入动作。
WAIT_KIND: str = IntentKind.WAIT.value


def normalize_key(key: Any) -> str | None:
    """归一化按键名（去空白、转小写）；非法输入返回 None。"""
    if not isinstance(key, str):
        return None
    normalized = key.strip().lower()
    return normalized or None


@dataclass(frozen=True)
class InputIntent:
    """单个抽象动作意图：运行时产出的最小可审计动作单元。

    字段与 domain_model.InputIntent 对齐，M1 契约测试统一。
    """

    intent_id: str
    session_id: str
    target_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_monotonic: float = 0.0
    expires_monotonic: float = math.inf
    cause: str = ""

    def expired(self, now: float) -> bool:
        """TTL 语义：now 达到或超过过期时刻即为过期（过期即拒绝、不补发）。"""
        return now >= self.expires_monotonic


@dataclass
class InputBatch:
    """意图批次：一次策略评估与执行调度的原子单位（INP-001）。

    字段与 domain_model.InputBatch 对齐；整批统一过期判断，
    同时逐意图的 TTL 在过滤阶段仍然生效。
    """

    batch_id: str
    session_id: str
    target_id: str
    intents: list[InputIntent] = field(default_factory=list)
    ttl_ms: float = DEFAULT_BATCH_TTL_MS
    created_monotonic: float = 0.0
    cause: str = ""

    def expired(self, now: float) -> bool:
        """整批 TTL 语义：now 达到 created + ttl 即整批过期。"""
        return now >= self.created_monotonic + self.ttl_ms / 1000.0

    @property
    def action_intents(self) -> list[InputIntent]:
        """非 wait 的动作意图（预算计数口径：wait 不算动作）。"""
        return [i for i in self.intents if i.kind != WAIT_KIND]


def _resolve_now(clock: Clock | None, now: float | None) -> float:
    """工厂函数的时间来源：必须且只能提供 clock / now 之一（保证可测确定性）。"""
    if (clock is None) == (now is None):
        raise ValueError("必须且只能提供 clock 或 now 之一")
    return float(now) if now is not None else float(clock.now())  # type: ignore[union-attr]


def make_intent(
    session_id: str,
    target_id: str,
    kind: IntentKind | str,
    payload: Mapping[str, Any] | None = None,
    *,
    clock: Clock | None = None,
    now: float | None = None,
    ttl_ms: float | None = DEFAULT_INTENT_TTL_MS,
    cause: str = "unspecified",
) -> InputIntent:
    """工厂函数：自动生成 intent_id 与时间戳（INP-001）。

    - 时间来源：clock（注入 Clock，推荐测试用 FakeClock）或显式 now，二选一；
    - ttl_ms=None 表示永不过期（仅用于停止路径的补偿 key_up）；
    - key_down/key_up 强制要求 payload["key"]，未知 kind 直接拒绝。
    """
    kind_value = str(getattr(kind, "value", kind))
    if kind_value not in _KIND_VALUES:
        raise ValueError(f"未知意图 kind: {kind_value!r}")
    if kind_value in (IntentKind.KEY_DOWN.value, IntentKind.KEY_UP.value):
        if normalize_key((payload or {}).get("key")) is None:
            raise ValueError(f"{kind_value} 意图必须提供非空 payload['key']")

    created = _resolve_now(clock, now)
    expires = math.inf if ttl_ms is None else created + float(ttl_ms) / 1000.0
    return InputIntent(
        intent_id=new_id("intent"),
        session_id=session_id,
        target_id=target_id,
        kind=kind_value,
        payload=dict(payload or {}),
        created_monotonic=created,
        expires_monotonic=expires,
        cause=cause,
    )


def make_batch(
    session_id: str,
    target_id: str,
    intents: list[InputIntent] | None = None,
    *,
    clock: Clock | None = None,
    now: float | None = None,
    ttl_ms: float | None = DEFAULT_BATCH_TTL_MS,
    cause: str = "unspecified",
) -> InputBatch:
    """工厂函数：自动生成 batch_id 与批次时间戳（INP-001）。"""
    created = _resolve_now(clock, now)
    batch_ttl = math.inf if ttl_ms is None else float(ttl_ms)
    return InputBatch(
        batch_id=new_id("batch"),
        session_id=session_id,
        target_id=target_id,
        intents=list(intents or []),
        ttl_ms=batch_ttl,
        created_monotonic=created,
        cause=cause,
    )
