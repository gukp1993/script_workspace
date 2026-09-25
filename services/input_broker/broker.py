"""InputBroker：意图批次进入真实输入前的最后一道闸（INP-001/002/INP-004/005/009）。

安全设计（默认拒绝，纵深防御）：
- submit(batch, decision)：decision.deny 时零输入并逐意图返回拒绝原因；
- 即使 decision.allow，Broker 自身持有 mode（来自 policy 决策结果），
  非 real_input 模式下依然拒绝执行（防策略层被绕过）；
- 先过滤过期意图（expired_intent），过期即拒绝、不补发；
- real_input 模式下逐批二次核验前台（INP-005，可注入 ForegroundVerifier），
  不一致 -> 零输入（SAFE-001）；
- 取消屏障（INP-009）：cancel()/stop() 以 epoch 计数推进，任何批次在
  执行开始前复查 epoch，取消后旧批次绝不再开始执行，顺序可预测；
- cancel() / stop() 后新批次一律拒绝；stop() 幂等并 release_all；
- 停止路径的补偿 key_up 不经策略评估直接执行（安全释放优先于一切）；
- real_input 模式下会话独占：第二个 RealInput 会话被拒绝（SAFE-019）。

decision 为 duck-typing：只要求存在 allow: bool 与 reasons: Sequence[str]
（policy_engine.PolicyDecision 天然满足；本包不 import policy_engine）。
foreground_verifier 为 duck-typing：只要求 verify_batch(batch) 返回带
ok: bool 与 reasons: Sequence[str] 的对象（window_service 提供真实实现）。
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

    def __init__(
        self,
        sink: InputSink,
        *,
        clock: Clock | None = None,
        foreground_verifier: Any = None,
    ) -> None:
        self.sink = sink
        self.clock: Clock = clock if clock is not None else MonotonicClock()
        #: 前台复核器（INP-005）：duck-typing verify_batch(batch) -> (ok, reasons)。
        self.foreground_verifier = foreground_verifier
        #: 当前模式，默认最低风险 observe；随 policy 决策结果更新。
        self.mode: str = "observe"
        self.key_ledger = KeyLedger()
        self.cancel_reason: str = ""
        self._cancelled = False
        self._stopped = False
        self._real_session_id: str | None = None
        self._last_session_id = ""
        self._last_target_id = ""
        #: 取消屏障计数（INP-009）：cancel/stop 各推进一次；批次执行前复查。
        self._epoch = 0

    # ------------------------------------------------------------------ 状态
    @property
    def last_session_id(self) -> str:
        """最近一次提交的会话 ID（审计/急停路径使用）。"""
        return self._last_session_id

    @property
    def last_target_id(self) -> str:
        """最近一次提交的目标 ID。"""
        return self._last_target_id

    @property
    def cancelled(self) -> bool:
        """是否已取消/停止（注入 SendInputAdapter.cancelled 形成双层屏障）。"""
        return self._cancelled or self._stopped

    @property
    def epoch(self) -> int:
        """当前取消屏障计数（观测用）。"""
        return self._epoch

    # ------------------------------------------------------------------ 提交
    def submit(
        self,
        batch: InputBatch,
        decision: Any,
        *,
        ctx: SinkContext | None = None,
    ) -> SinkResult:
        """提交一个批次；任何拒绝路径都保证零真实输入。"""
        # 取消屏障基线（INP-009）：进入即捕获 epoch，流程中任何 cancel/stop
        # （策略评估、前台复核期间发生）都会被执行前的复查拦下。
        epoch = self._epoch
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
        # 每批二次核验前台（INP-005 / SAFE-001）：不一致 -> 零输入。
        if self.foreground_verifier is not None:
            verdict = self.foreground_verifier.verify_batch(batch)
            if not bool(getattr(verdict, "ok", False)):
                reasons = ";".join(
                    str(r) for r in getattr(verdict, "reasons", []) or ["foreground_mismatch"]
                )
                return self._reject(batch, f"foreground_mismatch:{reasons}")

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
            # 取消屏障（INP-009）：过滤期间发生 cancel/stop -> 不再开始执行。
            if self._epoch != epoch:
                rejected.extend(
                    SinkRejection(intent=i, reason="broker_cancelled") for i in fresh
                )
                return SinkResult(
                    batch_id=batch.batch_id, accepted_intents=[], rejected=rejected
                )
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
        """取消当前会话：拒绝新批次，并释放所有已按下键（INP-004）。

        epoch 推进即取消屏障生效（INP-009）：此后任何未开始的批次
        都不得再开始执行；已按下的键立即按序释放。
        """
        if self._cancelled or self._stopped:
            return []
        self._cancelled = True
        self.cancel_reason = reason
        self._epoch += 1
        return self._release(cause="cancel")

    def stop(self) -> list[InputIntent]:
        """停止并释放所有已按下键；幂等：重复调用无额外效果。"""
        if self._stopped:
            return []
        self._stopped = True
        if not self._cancelled:
            self._epoch += 1
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
