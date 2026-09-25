"""FakeInputSink：M0 唯一的输入执行器（INP-002）。

安全边界：
- 绝不调用任何真实系统输入：本模块不 import ctypes / win32 / pyautogui /
  pydirectinput 等任何系统输入设施，只把意图按顺序写入内存账本；
- 可注入每动作延迟（仅记录，不真实睡眠，保证确定性）与失败率 / 失败点；
- Shadow / DryRun 记录路径（record_shadow）：意图被记录但永不执行；
- RealInputSink 仅为接口占位（M1 由 Win32 SendInput 适配器实现，INP-003），
  M0 阶段任何执行请求都显式抛出 NotImplementedError。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from common.clock import Clock

from input_broker.intents import InputBatch, InputIntent, normalize_key

#: FakeInputSink 的"真实执行"路径标记。
PATH_EXECUTE: str = "execute"

#: Shadow/DryRun 的"仅记录"路径标记。
PATH_SHADOW: str = "shadow"


@dataclass(frozen=True)
class SinkContext:
    """执行上下文：仅承载审计字段，绝不包含任何系统调用句柄。"""

    correlation_id: str = ""
    mode: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SinkRecord:
    """账本单条记录：一个意图的执行/记录结果（顺序保留）。"""

    index: int
    batch_id: str
    intent: InputIntent
    accepted: bool
    reason: str | None
    path: str
    planned_delay_ms: float
    executed_monotonic: float
    correlation_id: str = ""


@dataclass(frozen=True)
class SinkRejection:
    """被拒绝的意图及原因。"""

    intent: InputIntent
    reason: str


@dataclass
class SinkResult:
    """一次批次执行的返回：被接受意图（顺序保留）与被拒绝清单。"""

    batch_id: str
    accepted_intents: list[InputIntent] = field(default_factory=list)
    rejected: list[SinkRejection] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted_intents)


@runtime_checkable
class InputSink(Protocol):
    """输入执行器协议：M0 只有 FakeInputSink，M1 增加 Win32 适配器。"""

    def execute(self, batch: InputBatch, ctx: SinkContext | None = None) -> SinkResult:
        """执行一个批次并返回逐意图结果。"""
        ...


class FakeInputSink:
    """把每个意图原样记录到内存账本的假执行器（顺序保留）。

    - per_action_delay_ms：注入的每动作延迟，仅记入 planned_delay_ms，
      不做任何真实等待（测试确定性优先，真实时序由 M1 适配器测试覆盖）；
    - fail_points：{全局动作序号: 拒绝原因}，确定性失败点；
    - failure_rate + rng_seed：确定性失败率（同种子同序列）。
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        per_action_delay_ms: float = 0.0,
        fail_points: Mapping[int, str] | None = None,
        failure_rate: float = 0.0,
        rng_seed: int = 0,
    ) -> None:
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError("failure_rate 必须在 [0, 1] 内")
        self._clock = clock
        self._per_action_delay_ms = float(per_action_delay_ms)
        self._fail_points: dict[int, str] = dict(fail_points or {})
        self._failure_rate = float(failure_rate)
        self._rng = random.Random(rng_seed)
        self.records: list[SinkRecord] = []
        self._pressed: set[str] = set()
        self._counter = 0

    # ------------------------------------------------------------------ 查询
    @property
    def pressed(self) -> frozenset[str]:
        """当前模拟按下的键（与 KeyLedger 观察口径一致，可交叉验证）。"""
        return frozenset(self._pressed)

    @property
    def real_executed_count(self) -> int:
        """经 execute() 路径被接受的意图数（即"真实输入执行数"）。"""
        return sum(1 for r in self.records if r.path == PATH_EXECUTE and r.accepted)

    @property
    def shadow_recorded_count(self) -> int:
        """经 record_shadow() 路径记录的意图数。"""
        return sum(1 for r in self.records if r.path == PATH_SHADOW)

    def accepted_intents(self) -> list[InputIntent]:
        """按执行顺序返回全部被接受的意图。"""
        return [r.intent for r in self.records if r.path == PATH_EXECUTE and r.accepted]

    # ------------------------------------------------------------------ 执行
    def _injected_failure(self, index: int) -> str | None:
        reason = self._fail_points.get(index)
        if reason is not None:
            return reason
        if self._failure_rate > 0.0 and self._rng.random() < self._failure_rate:
            return f"injected_failure(rate={self._failure_rate})"
        return None

    def execute(self, batch: InputBatch, ctx: SinkContext | None = None) -> SinkResult:
        """把批次中的每个意图按顺序记入账本；绝不调用任何 OS 输入。"""
        correlation_id = ctx.correlation_id if ctx is not None else ""
        now = float(self._clock.now()) if self._clock is not None else 0.0
        accepted: list[InputIntent] = []
        rejected: list[SinkRejection] = []
        for intent in batch.intents:
            index = self._counter
            self._counter += 1
            reason = self._injected_failure(index)
            if reason is None:
                key = normalize_key(intent.payload.get("key"))
                if intent.kind == "key_down" and key is not None:
                    self._pressed.add(key)
                elif intent.kind == "key_up" and key is not None:
                    self._pressed.discard(key)
                accepted.append(intent)
            self.records.append(
                SinkRecord(
                    index=index,
                    batch_id=batch.batch_id,
                    intent=intent,
                    accepted=reason is None,
                    reason=reason,
                    path=PATH_EXECUTE,
                    planned_delay_ms=self._per_action_delay_ms,
                    executed_monotonic=now,
                    correlation_id=correlation_id,
                )
            )
            if reason is not None:
                rejected.append(SinkRejection(intent=intent, reason=reason))
        return SinkResult(
            batch_id=batch.batch_id, accepted_intents=accepted, rejected=rejected
        )

    def record_shadow(
        self, batch: InputBatch, ctx: SinkContext | None = None
    ) -> int:
        """Shadow/DryRun 记录路径：意图入账本但标记为未执行，返回记录条数。"""
        correlation_id = ctx.correlation_id if ctx is not None else ""
        now = float(self._clock.now()) if self._clock is not None else 0.0
        for intent in batch.intents:
            index = self._counter
            self._counter += 1
            self.records.append(
                SinkRecord(
                    index=index,
                    batch_id=batch.batch_id,
                    intent=intent,
                    accepted=False,
                    reason="mode_not_allowed",
                    path=PATH_SHADOW,
                    planned_delay_ms=self._per_action_delay_ms,
                    executed_monotonic=now,
                    correlation_id=correlation_id,
                )
            )
        return len(batch.intents)


class RealInputSink:
    """真实输入适配器占位类（仅接口契约）。

    M1 由 Win32 SendInput 适配器（INP-003）实现 execute()；
    M0 阶段禁止任何真实系统输入，任何执行请求都会显式失败。
    """

    def execute(self, batch: InputBatch, ctx: SinkContext | None = None) -> SinkResult:
        """占位实现：直接失败，绝不产生真实输入。"""
        raise NotImplementedError(
            "RealInputSink 仅是接口占位：M1 由 Win32 SendInput 适配器实现（INP-003）；"
            "M0 禁止任何真实系统输入。"
        )
