"""SessionEngine：一次会话的感知-决策-输入-轨迹闭环（E07/E10，M2）。

把一次单会话/单目标运行编排成固定顺序的逐帧闭环（每帧一 tick）::

    capture.grab()                      # None -> 跳过并计数
  -> DetectorScheduler 按预算跑检测器    # 频率/优先级/毫秒预算（VIS-008）
  -> PerceptionBuilder 聚合感知快照
  -> 写 perception_snapshot 轨迹事件     # frame_ref 在 FrameStore 启用时落盘像素
  -> MachineRuntime.tick（on_intent 收集意图）
  -> 写 state_transition 轨迹事件
  -> 有意图则 make_batch -> 写 intent_issued
  -> PolicyEvaluator.evaluate -> 写 policy_decision
  -> InputBroker.submit -> 写 executed

安全语义（纵深防御）：
- mode ∈ {observe, shadow, dry_run} 时策略必拒绝；即使策略漏判，
  InputBroker 自身持有的 mode 也会拒绝（真实输入恒为 0）；
- shadow/observe/dry_run 下批次经 ``sink.record_shadow`` 留痕（只记录不执行）；
- cancel/stop 后 run_tick 直接短路返回，不再产生任何新意图；
- pause 后必须以 verify() 为 True 的 callable 调 resume 才能恢复；
- stop() 幂等，并经 InputBroker 释放所有仍按下的键（补偿 key_up）；
- 预算耗尽（budget_exhausted / runtime_limit_reached）时会话按 stop 收尾；
- 全部轨迹事件携带同一 correlation_id（common.correlation_scope 体系）。

确定性（FSM-009 / AC-P0-08 的引擎侧保障）：调度只用帧序与检测器返回的
耗时，定时只依赖注入 Clock；同一帧序列 + 同一种子 -> 状态序列、意图
序列与 decision_hash 序列逐项一致，可随轨迹回放。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from capture_api.base import CaptureSource
from capture_api.frames import Frame
from common.clock import Clock
from common.ids import new_session_id
from common.logging import correlation_scope
from domain_model.dsl import CompiledMachine
from domain_model.models import PerceptionSnapshot
from domain_model.parsing import ProjectBundle
from input_broker import InputBroker, SinkContext, make_batch
from input_broker.fake_sink import InputSink, SinkResult
from input_broker.intents import InputIntent
from policy_engine import (
    WAIT_KIND,
    BudgetTracker,
    ForegroundContext,
    PolicyDecision,
    PolicyEvaluator,
    RunMode,
    TargetRef,
    parse_mode,
)
from state_machine import MachineRuntime, TickResult
from trace_format import JsonlTraceWriter, TraceEventType
from vision_core import DetectorScheduler, PerceptionBuilder, SchedulerEntry
from vision_core.base import Detector

__all__ = ["TickOutcome", "RunSummary", "FrameStore", "SessionEngine"]

#: 无帧 tick 的 frame_seq 占位值。
FRAME_SEQ_NONE: int = -1


def _json_safe(value: Any) -> Any:
    """把任意观测值归一为 JSON 可表示结构（其余 repr 为字符串）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return repr(value)


@dataclass(frozen=True)
class TickOutcome:
    """单帧闭环的结果快照（engine.run_tick 的返回值）。

    Attributes:
        frame_seq:       本 tick 消费的帧序号；无帧（跳过）为 ``FRAME_SEQ_NONE``。
        snapshot:        感知快照；无帧或控制短路时为 None。
        tick_result:     状态机 tick 决策结果；无帧或已取消/停止时为 None。
        policy_decision: 策略决策；本帧无意图时为 None。
        sink_result:     Broker 提交结果；本帧无意图时为 None。
    """

    frame_seq: int
    snapshot: PerceptionSnapshot | None
    tick_result: TickResult | None
    policy_decision: PolicyDecision | None
    sink_result: SinkResult | None


@dataclass(frozen=True)
class RunSummary:
    """一次运行（或停止）的累计摘要。

    Attributes:
        ticks:          已执行的 run_tick 总数（含跳过帧与终态帧）。
        states_visited: 按进入顺序排列的状态序列（含初始状态）。
        intents_total:  状态机构造的意图总数（无论策略是否放行）。
        real_executed:  经 Broker 真实执行（real_input 放行且被 sink 接受）
                        的非 wait 意图数。
        stopped:        会话是否已停止（终态/取消/停止/预算耗尽）。
        stop_reason:    停止原因：terminal / cancelled / stopped / error /
                        budget_exhausted* / runtime_limit_reached；未停止为 None。
        wall_elapsed:   自会话 setup（或构造）起的时钟秒数。
    """

    ticks: int
    states_visited: tuple[str, ...]
    intents_total: int
    real_executed: int
    stopped: bool
    stop_reason: str | None
    wall_elapsed: float


class FrameStore:
    """内容寻址的帧像素留痕存储（回放取证用，TRC 配套）。

    - ``put(frame)``：以像素 SHA-256 为引用落盘 ``.npy``，返回 frame_ref；
      相同内容只存一份（内容寻址，天然去重）；
    - ``get(ref)``：按引用回取像素数组；未命中返回 None；
    - 超出 ``max_frames`` 时按插入顺序淘汰最旧引用及其文件；
    - ``enabled`` 恒为 True——引擎以 ``frame_store is None`` 表示禁用。
    """

    def __init__(self, root: str | Path, *, max_frames: int = 16) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max = max(1, int(max_frames))
        self._order: list[str] = []

    @property
    def enabled(self) -> bool:
        """是否启用（构造即启用；引擎用 None 表示禁用）。"""
        return True

    @property
    def root(self) -> Path:
        """存储根目录。"""
        return self._root

    def put(self, frame: Frame) -> str:
        """落盘一帧像素并返回内容寻址引用（sha256 十六进制）。"""
        pixels = np.ascontiguousarray(frame.pixels)
        ref = hashlib.sha256(pixels.tobytes()).hexdigest()
        path = self._root / f"{ref}.npy"
        if not path.exists():
            np.save(path, pixels)
        if ref in self._order:
            self._order.remove(ref)
        self._order.append(ref)
        while len(self._order) > self._max:
            oldest = self._order.pop(0)
            (self._root / f"{oldest}.npy").unlink(missing_ok=True)
        return ref

    def get(self, ref: str) -> np.ndarray | None:
        """按引用回取像素；未命中返回 None。"""
        path = self._root / f"{ref}.npy"
        if not path.exists():
            return None
        return np.load(path)


class SessionEngine:
    """感知-策略-输入-轨迹的单会话闭环编排器。

    Args:
        project:     项目聚合根（检测器清单决定调度与字段映射顺序）。
        machine:     已编译状态机（调用方先经 compile_* 编译）。
        detectors:   detector_id -> Detector 实例（VIS-001 协议）。
        policy:      策略求值器（默认拒绝）。
        broker:      输入代理（唯一真实输入出口）。
        capture:     采集源。
        trace:       JSONL 轨迹写器（逐事件哈希链）。
        clock:       注入时钟（生产 Monotonic / 测试 Fake）。
        mode:        运行模式（observe/shadow/dry_run/real_input）。
        target:      目标引用（策略与批次目标标识）。
        foreground:  前台上下文（real_input 必需；None 时策略拒绝）。
        frame_store: 帧像素留痕存储；None 表示禁用（frame_ref=None）。
        max_ticks:   run() 的默认 tick 上限；None 表示不设上限。
        session_id:  会话 ID；缺省自动生成。
        rng:         透传给状态机的随机源（运行时自身不消费，为扩展保留）。
        budget_ms:   每帧检测计算预算（毫秒，VIS-008）。
    """

    def __init__(
        self,
        *,
        project: ProjectBundle,
        machine: CompiledMachine,
        detectors: dict[str, Detector],
        policy: PolicyEvaluator,
        broker: InputBroker,
        capture: CaptureSource,
        trace: JsonlTraceWriter,
        clock: Clock,
        mode: RunMode | str,
        target: TargetRef,
        foreground: ForegroundContext | None = None,
        frame_store: FrameStore | None = None,
        max_ticks: int | None = None,
        session_id: str | None = None,
        rng: random.Random | None = None,
        budget_ms: float = 12.0,
    ) -> None:
        self._project = project
        self._machine = machine
        self._policy = policy
        self._broker = broker
        self._capture = capture
        self._trace = trace
        self._clock = clock
        parsed_mode = parse_mode(mode)
        if parsed_mode is None:
            raise ValueError(f"未知运行模式: {mode!r}")
        self._mode = parsed_mode
        self._target = target
        self._foreground = foreground
        self._frame_store = frame_store
        self._max_ticks = max_ticks
        self._session_id = session_id or new_session_id()
        self._correlation_id = f"cid-{self._session_id}"

        # 检测器调度：项目检测器清单顺序优先，其余按 detector_id 排序补充。
        entries: list[SchedulerEntry] = []
        used: list[Detector] = []
        seen: set[str] = set()
        for detector_id, _cfg in project.detectors.items():
            detector = detectors.get(detector_id)
            if detector is not None:
                entries.append(SchedulerEntry(detector))
                used.append(detector)
                seen.add(detector_id)
        for detector_id in sorted(detectors):
            if detector_id not in seen:
                entries.append(SchedulerEntry(detectors[detector_id]))
                used.append(detectors[detector_id])
        if not entries:
            raise ValueError("detectors 为空：至少提供一个检测器才能驱动感知闭环")
        self._scheduler = DetectorScheduler(entries, budget_ms=budget_ms)
        self._builder = PerceptionBuilder({d.detector_id: d.field_name for d in used})

        # 状态机运行时：意图只构造、经回调收集，绝不执行。
        self._pending: list[InputIntent] = []
        self._runtime = MachineRuntime(
            machine,
            clock,
            self._collect_intent,
            rng,
            session_id=self._session_id,
            target_id=target.target_id,
        )
        # 预算跟踪：限制只来自策略配置（POL-004/005）。
        self._budget = BudgetTracker(clock=clock, policy=policy.policy)

        # 运行状态旗标。
        self._paused = False
        self._cancelled = False
        self._stopped = False
        self._auto_stop_reason: str | None = None
        self._tick_count = 0
        self._skipped_frames = 0
        self._intents_total = 0
        self._real_executed = 0
        self._states_visited: list[str] = [machine.initial]
        self._started_at: float | None = None
        self._last_outcome: TickOutcome | None = None
        self._last_summary: RunSummary | None = None
        self._stopped_summary: RunSummary | None = None

    # ------------------------------------------------------------------ 只读视图
    @property
    def session_id(self) -> str:
        """会话 ID。"""
        return self._session_id

    @property
    def correlation_id(self) -> str:
        """全部轨迹事件共用的关联 ID。"""
        return self._correlation_id

    @property
    def mode(self) -> RunMode:
        """当前运行模式。"""
        return self._mode

    @property
    def target(self) -> TargetRef:
        """会话绑定的目标引用。"""
        return self._target

    @property
    def trace(self) -> JsonlTraceWriter:
        """轨迹写器。"""
        return self._trace

    @property
    def broker(self) -> InputBroker:
        """输入代理。"""
        return self._broker

    @property
    def capture(self) -> CaptureSource:
        """采集源。"""
        return self._capture

    @property
    def runtime(self) -> MachineRuntime:
        """状态机运行时（覆盖率/人工闸门等高级操作的出口）。"""
        return self._runtime

    @property
    def state(self) -> str:
        """当前状态名。"""
        return self._runtime.state()

    @property
    def states_visited(self) -> tuple[str, ...]:
        """按进入顺序的状态序列（含初始状态）。"""
        return tuple(self._states_visited)

    @property
    def skipped_frames(self) -> int:
        """grab() 返回 None 而跳过的 tick 计数。"""
        return self._skipped_frames

    @property
    def intents_total(self) -> int:
        """状态机已构造的意图总数（无论策略是否放行）。"""
        return self._intents_total

    @property
    def is_stopped(self) -> bool:
        """会话是否已取消或停止（此后 run_tick 短路，不再产生意图）。"""
        return self._cancelled or self._stopped

    @property
    def last_outcome(self) -> TickOutcome | None:
        """最近一次 run_tick 的结果。"""
        return self._last_outcome

    @property
    def manual_gate_pending(self) -> bool:
        """是否有人工闸门在等待放行。"""
        return self._runtime.manual_gate_pending

    # ------------------------------------------------------------------ 生命周期
    def setup(self) -> None:
        """启动会话：启动采集、绑定 correlation、把模式同步给 Broker。"""
        with correlation_scope(self._correlation_id):
            self._capture.start()
            self._broker.mode = self._mode.value
            if self._started_at is None:
                self._started_at = self._clock.now()

    def run_tick(self) -> TickOutcome:
        """执行一帧闭环；流程与事件顺序见模块 docstring。"""
        with correlation_scope(self._correlation_id):
            # 取消/停止屏障：不再抓帧、不再产生任何意图或事件。
            if self._cancelled or self._stopped:
                return TickOutcome(FRAME_SEQ_NONE, None, None, None, None)
            self._tick_count += 1

            frame = self._capture.grab()
            if frame is None:
                # 暂无新帧不是错误：跳过本帧并计数（与真实适配器语义一致）。
                self._skipped_frames += 1
                self._last_outcome = TickOutcome(FRAME_SEQ_NONE, None, None, None, None)
                return self._last_outcome

            # 1) 感知：按预算调度检测器并聚合快照。
            now = self._clock.now()
            results = self._scheduler.process_frame(frame)
            snapshot = self._builder.build(results, frame.meta.seq, now)
            frame_ref: str | None = None
            if self._frame_store is not None and self._frame_store.enabled:
                frame_ref = self._frame_store.put(frame)
            self._trace.append(
                TraceEventType.PERCEPTION_SNAPSHOT,
                ts_monotonic=self._clock.now(),
                correlation_id=self._correlation_id,
                session_id=self._session_id,
                payload={
                    "fields": {
                        name: {
                            "present": obs.present,
                            "confidence": float(obs.confidence),
                            "value": _json_safe(obs.value),
                        }
                        for name, obs in sorted(snapshot.values.items())
                    },
                    "frame_ref": frame_ref,
                    "ts": float(snapshot.ts_monotonic),
                },
            )

            # 2) 状态机 tick：on_intent 回调收集本帧意图（只构造不执行）。
            self._pending = []
            tick_result = self._runtime.tick(snapshot)
            self._trace.append(
                TraceEventType.STATE_TRANSITION,
                ts_monotonic=self._clock.now(),
                correlation_id=self._correlation_id,
                session_id=self._session_id,
                payload={
                    "from": tick_result.from_state,
                    "to": tick_result.to_state,
                    "decision_hash": tick_result.decision_hash,
                },
            )
            if tick_result.to_state is not None:
                self._states_visited.append(tick_result.to_state)

            # 3) 意图 -> 批次 -> 策略 -> Broker -> 轨迹。
            outcome = self._dispatch_intents(tick_result)
            self._last_outcome = TickOutcome(
                frame.meta.seq, snapshot, tick_result,
                outcome[0], outcome[1],
            )
            return self._last_outcome

    def run(self, max_ticks: int | None = None) -> RunSummary:
        """循环执行 run_tick 直到终态/取消/停止/预算耗尽/tick 上限/暂停。"""
        with correlation_scope(self._correlation_id):
            effective = max_ticks if max_ticks is not None else self._max_ticks
            stopped = False
            reason: str | None = None
            while True:
                if self._cancelled:
                    stopped, reason = True, "cancelled"
                    break
                if self._stopped:
                    stopped, reason = True, "stopped"
                    break
                if self._auto_stop_reason is not None:
                    stopped, reason = True, self._auto_stop_reason
                    break
                if effective is not None and self._tick_count >= effective:
                    stopped, reason = False, None
                    break
                if self._paused:
                    stopped, reason = False, "paused"
                    break
                try:
                    outcome = self.run_tick()
                except Exception as exc:
                    # 异常留痕后原样上抛；清理交给 recovery.close_and_finalize。
                    self._trace.append(
                        TraceEventType.ANOMALY,
                        ts_monotonic=self._clock.now(),
                        correlation_id=self._correlation_id,
                        session_id=self._session_id,
                        payload={"error": repr(exc), "stage": "tick"},
                    )
                    raise
                if outcome.tick_result is not None and outcome.tick_result.stopped:
                    stopped, reason = True, outcome.tick_result.stop_reason
                    break
            summary = self._build_summary(stopped=stopped, stop_reason=reason)
            self._last_summary = summary
            return summary

    # ------------------------------------------------------------------ 控制
    def pause(self) -> None:
        """暂停：tick 仍跑感知留痕，但不评估守卫、不迁移、不产意图。"""
        with correlation_scope(self._correlation_id):
            self._paused = True
            self._runtime.pause()

    def resume(self, verify: Callable[[], bool] | None = None) -> None:
        """恢复运行：必须传入 verify 且其返回 True（恢复前重新验证）。"""
        if verify is None:
            raise RuntimeError("恢复被拒绝：resume 必须传入 verify 校验")
        with correlation_scope(self._correlation_id):
            if not verify():
                raise RuntimeError("恢复被拒绝：verify() 返回 False（恢复前必须重新验证）")
            self._paused = False
            self._runtime.resume()

    def cancel(self) -> None:
        """取消会话：此后 run_tick 不再产生新意图，且 Broker 释放已按下键。"""
        with correlation_scope(self._correlation_id):
            if self._cancelled:
                return
            self._cancelled = True
            self._runtime.cancel()
            self._broker.cancel(reason="session_cancelled")

    def stop(self) -> RunSummary:
        """停止会话：幂等；经 Broker 释放所有仍按下的键（补偿 key_up）。"""
        with correlation_scope(self._correlation_id):
            if self._stopped and self._stopped_summary is not None:
                return self._stopped_summary  # 幂等：重复调用返回同一摘要
            self._stopped = True
            self._runtime.cancel()
            self._broker.stop()
            self._stopped_summary = self._build_summary(stopped=True, stop_reason="stopped")
            return self._stopped_summary

    def approve_manual_gate(self) -> bool:
        """人工闸门放行透传；返回是否确有闸门在等待。"""
        return self._runtime.approve_manual_gate()

    # ------------------------------------------------------------------ 内部
    def _collect_intent(self, intent: InputIntent) -> None:
        """on_intent 回调：登记本帧意图并累计总数（绝不执行）。"""
        self._pending.append(intent)
        self._intents_total += 1

    def _dispatch_intents(
        self, tick_result: TickResult
    ) -> tuple[PolicyDecision | None, SinkResult | None]:
        """把本帧意图走完 批次->策略->Broker->轨迹 的下半段闭环。"""
        if not self._pending:
            return None, None
        batch = make_batch(
            self._session_id,
            self._target.target_id,
            list(self._pending),
            clock=self._clock,
            cause=f"tick:{tick_result.tick}",
        )
        self._trace.append(
            TraceEventType.INTENT_ISSUED,
            ts_monotonic=self._clock.now(),
            correlation_id=self._correlation_id,
            session_id=self._session_id,
            payload={
                "batch_id": batch.batch_id,
                "intents": [
                    {"kind": i.kind, "payload": _json_safe(dict(i.payload)), "cause": i.cause}
                    for i in batch.intents
                ],
            },
        )
        decision = self._policy.evaluate(
            batch,
            self._mode,
            self._target,
            foreground=self._foreground,
            budget_state=self._budget,
            clock=self._clock,
        )
        self._trace.append(
            TraceEventType.POLICY_DECISION,
            ts_monotonic=self._clock.now(),
            correlation_id=self._correlation_id,
            session_id=self._session_id,
            payload={
                "batch_id": batch.batch_id,
                "allow": bool(decision.allow),
                "reasons": list(decision.reasons),
                "mode": str(decision.mode),
            },
        )
        sink_result = self._broker.submit(
            batch,
            decision,
            ctx=SinkContext(correlation_id=self._correlation_id, mode=str(decision.mode)),
        )
        real = sum(1 for i in sink_result.accepted_intents if i.kind != WAIT_KIND)
        self._real_executed += real
        self._trace.append(
            TraceEventType.EXECUTED,
            ts_monotonic=self._clock.now(),
            correlation_id=self._correlation_id,
            session_id=self._session_id,
            payload={
                "batch_id": batch.batch_id,
                "accepted": len(sink_result.accepted_intents),
                "rejected": len(sink_result.rejected),
                "real": real,
            },
        )
        # 非真实输入模式：批次经 sink 的 shadow 记录路径留痕（只记录不执行）。
        if self._mode is not RunMode.REAL_INPUT:
            sink = self._broker.sink
            record_shadow = getattr(sink, "record_shadow", None)
            if callable(record_shadow):
                record_shadow(
                    batch,
                    SinkContext(correlation_id=self._correlation_id, mode=self._mode.value),
                )
        # 预算/时长到限：会话按 stop 收尾（SAFE-012/014）。
        for reason in decision.reasons:
            if reason == "runtime_limit_reached" or reason.startswith("budget_exhausted"):
                self._auto_stop_reason = reason
                break
        return decision, sink_result

    def _build_summary(self, *, stopped: bool, stop_reason: str | None) -> RunSummary:
        """汇总累计运行指标。"""
        start = self._started_at if self._started_at is not None else self._clock.now()
        return RunSummary(
            ticks=self._tick_count,
            states_visited=tuple(self._states_visited),
            intents_total=self._intents_total,
            real_executed=self._real_executed,
            stopped=stopped,
            stop_reason=stop_reason,
            wall_elapsed=max(0.0, self._clock.now() - start),
        )
