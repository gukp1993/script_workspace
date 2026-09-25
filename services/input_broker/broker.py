"""InputBroker：意图批次进入真实输入前的最后一道闸（INP-001/002/INP-004 雏形）。

安全设计（默认拒绝，纵深防御）：
- submit(batch, decision)：decision.deny 时零输入并逐意图返回拒绝原因；
- 即使 decision.allow，Broker 自身持有 mode（来自 policy 决策结果），
  非 real_input 模式下依然拒绝执行（防策略层被绕过）；
- 先过滤过期意图（expired_intent），过期即拒绝、不补发；
- cancel() / stop() 后新批次一律拒绝；stop() 幂等并 release_all；
- 停止路径的补偿 key_up 不经策略评估直接执行（安全释放优先于一切）；
- real_input 模式下会话独占：第二个 RealInput 会话被拒绝（SAFE-019）。

decision 为 duck-typing：只要求存在 allow: bool 与 reasons: Sequence[str]
（policy_engine.PolicyDecision 天然满足；本包不 import policy_engine）。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Sequence

from common.clock import Clock, MonotonicClock
from common.ids import new_id

from input_broker.fake_sink import InputSink, SinkContext, SinkRejection, SinkResult
from input_broker.intents import InputBatch, InputIntent, make_batch
from input_broker.key_ledger import KeyLedger

#: 唯一允许执行真实输入的 mode 值（与 policy_engine.RunMode.REAL_INPUT 对齐）。
REAL_INPUT_MODE: str = "real_input"


class InputBroker:
    """持有运行模式与按键账本的最小输入代理。"""

    def __init__(self, sink: InputSink, *, clock: Clock | None = None) -> None:
        self.sink = sink
        self.clock: Clock = clock if clock is not None else MonotonicClock()
        #: 当前模式，默认最低风险 observe；随 policy 决策结果更新。
        self.mode: str = "observe"
        self.key_ledger = KeyLedger()
        self.cancel_reason: str = ""
        self._cancelled = False
        self._stopped = False
        self._real_session_id: str | None = None
        self._last_session_id = ""
        self._last_target_id = ""

    # ------------------------------------------------------------------ 提交
    def submit(
        self,
        batch: InputBatch,
        decision: Any,
        *,
        ctx: SinkContext | None = None,
    ) -> SinkResult:
        """提交一个批次；任何拒绝路径都保证零真实输入。"""
        mode_raw = getattr(decision, "mode", None)
        if mode_raw:
            self.mode = str(getattr(mode_raw, "value", mode_raw))
        self._last_session_id = batch.session_id
        self._last_target_id = batch.target_id

        if self._stopped:
            return self._reject(batch, "broker_stopped")
        if self._cancelled:
            return self._reject(batch, "broker_cancelled")
        if not bool(getattr(decision, "allow", False)):
            reasons = ";".join(str(r) for r in getattr(decision, "reasons", []))
            return self._reject(batch, reasons or "policy_denied")
        # 纵深防御：策略层允许但 Broker 模式非 real_input 时依然拒绝。
        if self.mode != REAL_INPUT_MODE:
            return self._reject(batch, "mode_not_allowed")
        # real_input 会话独占（SAFE-019）：第二个并发 RealInput 会话拒绝。
        if self._real_session_id is None:
            self._real_session_id = batch.session_id
        elif batch.session_id != self._real_session_id:
            return self._reject(batch, "session_mismatch")

        now = float(self.clock.now())
        if batch.expired(now):
            return self._reject(batch, "expired_intent")

        fresh: list[InputIntent] = []
        expired: list[InputIntent] = []
        for intent in batch.intents:
            (expired if intent.expired(now) else fresh).append(intent)

        accepted: list[InputIntent] = []
        rejected = [SinkRejection(intent=i, reason="expired_intent") for i in expired]
        if fresh:
            sub_batch = dataclasses.replace(batch, intents=fresh)
            result = self.sink.execute(sub_batch, ctx)
            accepted.extend(result.accepted_intents)
            rejected.extend(result.rejected)
            for intent in accepted:
                self.key_ledger.observe(intent)
        return SinkResult(
            batch_id=batch.batch_id, accepted_intents=accepted, rejected=rejected
        )

    def claim_real_session(self, session_id: str) -> bool:
        """显式抢占 real_input 会话席位；先到先得，重复/并发抢占失败。"""
        if self._real_session_id is None:
            self._real_session_id = session_id
            return True
        return self._real_session_id == session_id

    # ------------------------------------------------------------------ 停止
    def cancel(self, reason: str = "operator_cancel") -> list[InputIntent]:
        """取消当前会话：拒绝新批次，并释放所有已按下键（INP-004）。"""
        if self._cancelled or self._stopped:
            return []
        self._cancelled = True
        self.cancel_reason = reason
        return self._release(cause="cancel")

    def stop(self) -> list[InputIntent]:
        """停止并释放所有已按下键；幂等：重复调用无额外效果。"""
        if self._stopped:
            return []
        self._stopped = True
        self._cancelled = True
        return self._release(cause="stop")

    # ------------------------------------------------------------------ 内部
    def _release(self, *, cause: str) -> list[InputIntent]:
        """停止路径：生成补偿 key_up 并直接执行（不经策略评估）。"""
        now = float(self.clock.now())
        ups = self.key_ledger.release_all(
            now,
            session_id=self._last_session_id,
            target_id=self._last_target_id,
            cause=f"broker_{cause}",
        )
        if not ups:
            return []
        release_batch = make_batch(
            self._last_session_id,
            self._last_target_id,
            ups,
            now=now,
            ttl_ms=None,
            cause=f"broker_{cause}",
        )
        self.sink.execute(release_batch)
        return ups

    def _reject(self, batch: InputBatch, reason: str) -> SinkResult:
        return SinkResult(
            batch_id=batch.batch_id,
            accepted_intents=[],
            rejected=[SinkRejection(intent=i, reason=reason) for i in batch.intents],
        )
