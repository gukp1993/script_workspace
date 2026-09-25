"""确定性状态机运行时（FSM-003/004/005/006/007/008/009/010）。

tick 流程（每帧一次，顺序固定以保证确定性）：
1. 已取消 -> stopped 结果（不再产生任何意图）；
2. 已暂停 -> 空结果（不评估、不迁移、不产意图）；
3. entry 未完成 -> 按重试策略补完（退避基于注入 Clock，FSM-008）；
4. 终态 -> stopped 结果；
5. 断言评估（FSM-007）：wait_until 满足解除阻塞；assert_after 超时失败
   则迁入出口状态并保留证据（last_assert_failure）；
6. 守卫评估与确定性迁移选择（FSM-003，迁移级 stable_frames 计数）；
   人工闸门挂起（manual_gate）或 wait_until 阻塞时跳过本步；
7. 状态超时（FSM-005）：守卫未命中（或被阻塞）且超时已到 -> 迁移到
   首个 on_timeout_to（闸门失联/等待卡死的安全出口）；
8. 无事发生 -> 空结果。

安全约定：运行时只**构造** InputIntent（make_intent，cause=状态名）并经
on_intent 回调交出，绝不执行任何真实输入；意图的 TTL/调度/执行由
services/input_broker 负责。

确定性（FSM-009 / AC-P0-08）：decision_hash = sha256(规范化决策 JSON)。
规范化 JSON 包含机器 ID、tick、from/to 状态、选中迁移、守卫评估序列、
意图的 (kind, payload, cause)（**剔除** intent_id/时间戳等易变字段）与
快照关键字段；随机性只能来自注入 rng，不参与哈希。同版本 + 同轨迹 +
同种子 -> 完全相同的哈希序列。
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any, Callable

from common.clock import Clock
from domain_model.dsl import (
    CompiledAction,
    CompiledAssertion,
    CompiledMachine,
    CompiledTransition,
    ExprEval,
)
from domain_model.models import PerceptionSnapshot
from input_broker.intents import InputIntent, make_intent
from state_machine.assertions import AssertionManager
from state_machine.control import RuntimeController
from state_machine.coverage import CoverageTracker
from state_machine.retry import RetryController
from state_machine.semantics import evaluate_guards, select_transition
from state_machine.timers import StateTimer

__all__ = ["TickResult", "MachineRuntime", "canonical_snapshot"]

#: 异常路由的最大深度（防止错误出口互相路由成环时栈溢出；超出即停止）
_MAX_ERROR_ROUTE_DEPTH: int = 8

#: 合法鼠标按键
_MOUSE_BUTTONS: frozenset[str] = frozenset({"left", "right", "middle"})


@dataclass(frozen=True)
class TickResult:
    """单帧 tick 的决策结果（含决策哈希，FSM-009）。

    Attributes:
        tick:             帧序号（从 1 起，含暂停/取消的空帧）。
        from_state:       tick 开始时的状态名。
        to_state:         迁移目标状态名；None 表示本帧无迁移。
        guards_evaluated: 本帧按顺序评估过的条件表达式原文。
        emitted_intents:  本帧构造的意图（exit/entry 动作产生，只构造不执行）。
        decision_hash:    本 tick 决策哈希（sha256，规范化 JSON）。
        stopped:          运行是否已停止（终态/取消/未处理错误）。
        stop_reason:      停止原因：terminal / cancelled / error；未停止为 None。
    """

    tick: int
    from_state: str
    to_state: str | None
    guards_evaluated: tuple[str, ...]
    emitted_intents: tuple[InputIntent, ...]
    decision_hash: str
    stopped: bool
    stop_reason: str | None


def _json_safe(value: Any) -> Any:
    """把任意观测值归一为 JSON 可表示结构（其余 repr 为字符串）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return repr(value)


def canonical_snapshot(snapshot: PerceptionSnapshot) -> dict[str, Any]:
    """把感知快照规范化为可哈希 dict（字段按名称排序，FSM-009）。"""
    return {
        "frame_seq": snapshot.frame_seq,
        "ts_monotonic": float(snapshot.ts_monotonic),
        "values": {
            name: {
                "present": obs.present,
                "confidence": float(obs.confidence),
                "value": _json_safe(obs.value),
            }
            for name, obs in sorted(snapshot.values.items())
        },
    }


class MachineRuntime:
    """状态机运行时：驱动 CompiledMachine 逐帧迁移并产出动作意图。

    Args:
        machine:   编译产物（:class:`CompiledMachine`）。
        clock:     注入时钟（生产 MonotonicClock / 测试 FakeClock）。
        on_intent: 意图回调——每产生一个意图立即调用（只回调不执行）。
        rng:       注入随机源；运行时自身不消费随机数（为将来扩展保留），
                   因此不注入也可保证哈希序列确定。
        session_id / target_id: 意图上的会话/目标标识。
        verify:    恢复校验 callable——resume() 前必须返回 True（FSM-006）。
    """

    def __init__(
        self,
        machine: CompiledMachine,
        clock: Clock,
        on_intent: Callable[[InputIntent], None] | None = None,
        rng: random.Random | None = None,
        *,
        session_id: str = "sess-machine",
        target_id: str = "target-unknown",
        verify: Callable[[], bool] | None = None,
    ) -> None:
        if not isinstance(machine, CompiledMachine):
            raise TypeError("machine 必须是 CompiledMachine（先用 compile_machine_* 编译）")
        self._machine = machine
        self._clock = clock
        self._on_intent = on_intent
        self._rng = rng
        self._session_id = session_id
        self._target_id = target_id
        self._evaluator = ExprEval()
        self._control = RuntimeController(verify)
        self._timer = StateTimer(clock)
        self._coverage = CoverageTracker()
        self._assertions = AssertionManager(clock)
        self._stable: dict[tuple[str, int], int] = {}
        self._tick_no: int = 0
        #: 最近一次断言失败的证据（快照/条件/耗时等）；无失败为 None
        self.last_assert_failure: dict[str, Any] | None = None

        initial = machine.states[machine.initial]
        self._current: str = machine.initial
        self._timer.arm(initial.timeout_seconds)
        self._retry = RetryController(initial.retry_spec, clock)
        self._entry_pending: bool = True
        self._gate_pending: bool = False
        self._gate_message: str = ""
        self._coverage.record_state_entered(machine.initial)

    # ------------------------------------------------------------------
    # 对外只读接口
    # ------------------------------------------------------------------

    def state(self) -> str:
        """当前状态名。"""
        return self._current

    def is_terminal(self) -> bool:
        """当前状态是否为终态。"""
        return self._machine.states[self._current].terminal

    def coverage(self) -> dict[str, dict[str, int]]:
        """覆盖率统计（FSM-010）：状态/迁移/守卫/异常路径计数。"""
        return self._coverage.as_dict()

    def pause(self) -> None:
        """暂停：tick 只推进计数，不评估/不迁移/不产意图。"""
        self._control.pause()

    def resume(self) -> None:
        """恢复：恢复前必须通过 verify()（False 则拒绝并抛 RuntimeError）。"""
        self._control.resume()

    def cancel(self) -> None:
        """安全停止：之后 tick 不再产生意图，且不可恢复。"""
        self._control.cancel()

    def approve_manual_gate(self) -> bool:
        """人工闸门放行：清除挂起标记；返回是否确有闸门在等待。"""
        if not self._gate_pending:
            return False
        self._gate_pending = False
        self._gate_message = ""
        return True

    @property
    def manual_gate_pending(self) -> bool:
        """是否有人工闸门在等待放行。"""
        return self._gate_pending

    # ------------------------------------------------------------------
    # tick 主流程
    # ------------------------------------------------------------------

    def tick(self, snapshot: PerceptionSnapshot) -> TickResult:
        """驱动一帧：按模块 docstring 的固定顺序执行。"""
        if not isinstance(snapshot, PerceptionSnapshot):
            raise TypeError("tick 需要 PerceptionSnapshot")
        self._tick_no += 1
        state = self._machine.states[self._current]
        intents: list[InputIntent] = []

        if self._control.cancelled:
            return self._result(snapshot, state.name, None, None, (), intents, True, "cancelled")
        if self._control.paused:
            return self._result(snapshot, state.name, None, None, (), intents, False, None)

        # 1) entry 未完成（首次进入或重试补完）
        if self._entry_pending:
            early = self._try_complete_entry(state, snapshot, intents, depth=0)
            if early is not None:
                return early
            state = self._machine.states[self._current]  # 异常路由可能已换状态

        # 2) 终态
        if state.terminal:
            return self._result(snapshot, state.name, None, None, (), intents, True, "terminal")

        # 3) 断言（失败会强制迁入出口状态并保留证据）
        forced = self._check_assertions(snapshot, intents)
        if forced is not None:
            return forced

        # 4) 人工闸门挂起 / wait_until 阻塞：不评估守卫、不选择迁移；
        #    状态超时在下方仍然生效（它是闸门失联/等待卡死时的安全出口）
        guards: tuple[str, ...] = ()
        blocked = self._gate_pending or self._assertions.blocking
        if not blocked:
            outcomes = evaluate_guards(
                state,
                snapshot,
                self._evaluator,
                self._stable,
                on_guard=lambda s, w, v: self._coverage.record_guard(s, w, v),
            )
            guards = tuple(o.transition.when_src for o in outcomes)
            selected = select_transition(outcomes)
            if selected is not None:
                return self._execute_transition(selected, snapshot, guards, intents, via="guard")

        # 5) 状态超时（守卫未命中或被阻塞时的兜底出口）：迁移到 on_timeout_to
        if self._timer.expired and state.timeout_transition is not None:
            return self._execute_transition(
                state.timeout_transition, snapshot, (), intents,
                via="timeout", target=state.timeout_transition.on_timeout_to,
            )

        # 6) 无事发生
        return self._result(snapshot, state.name, None, None, guards, intents, False, None)

    # ------------------------------------------------------------------
    # entry 补完 / 异常路由（FSM-008）
    # ------------------------------------------------------------------

    def _try_complete_entry(
        self,
        state: Any,
        snapshot: PerceptionSnapshot,
        intents: list[InputIntent],
        *,
        depth: int,
    ) -> TickResult | None:
        """尝试执行 entry 动作序列；失败走重试/路由。返回非 None 表示 tick 提前结束。"""
        if not self._retry.ready():
            return self._result(snapshot, state.name, None, None, (), intents, False, None)
        try:
            self._run_steps(state.entry, snapshot, state.name, intents)
        except Exception:
            self._coverage.record_exception(state.name)
            self._retry.register_failure()
            if self._retry.exhausted:
                return self._route_error(state, snapshot, intents, depth)
            # 未达上限：等待退避后由后续 tick 重试
            return self._result(snapshot, state.name, None, None, (), intents, False, None)
        self._entry_pending = False
        return None

    def _route_error(
        self,
        state: Any,
        snapshot: PerceptionSnapshot,
        intents: list[InputIntent],
        depth: int,
    ) -> TickResult:
        """重试耗尽后的异常路由：迁入 on_error_to 目标，否则安全停止。"""
        target = state.error_target
        if target is None or depth >= _MAX_ERROR_ROUTE_DEPTH:
            return self._result(snapshot, state.name, None, None, (), intents, True, "error")
        early = self._begin_enter(state.name, target, snapshot, intents, depth=depth + 1)
        if early is not None:
            return early
        return self._result(snapshot, state.name, target, None, (), intents, False, None, via="error")

    # ------------------------------------------------------------------
    # 迁移执行
    # ------------------------------------------------------------------

    def _execute_transition(
        self,
        tr: CompiledTransition,
        snapshot: PerceptionSnapshot,
        guards: tuple[str, ...],
        intents: list[InputIntent],
        *,
        via: str,
        target: str | None = None,
    ) -> TickResult:
        """执行一次迁移：exit 动作 -> 清理 -> 进入目标（entry 在进入时尝试）。

        ``target`` 缺省为迁移的 ``to``；状态超时路径传 ``on_timeout_to``。
        """
        source = self._machine.states[tr.source_state]
        target_name = target if target is not None else tr.target
        try:
            self._run_steps(source.exit, snapshot, source.name, intents)
        except Exception:
            # exit 动作失败不阻断迁移（安全优先：离开当前状态）
            self._coverage.record_exception(source.name)
        early = self._begin_enter(tr.source_state, target_name, snapshot, intents)
        if early is not None:
            return early
        return self._result(snapshot, tr.source_state, target_name, tr, guards, intents, False, None, via=via)

    def _begin_enter(
        self,
        source_name: str,
        target_name: str,
        snapshot: PerceptionSnapshot,
        intents: list[InputIntent],
        *,
        depth: int = 0,
    ) -> TickResult | None:
        """状态切换的公共路径：清理旧状态、装载新状态、尝试 entry。"""
        target = self._machine.states[target_name]
        self._coverage.record_transition(source_name, target_name)
        # 离开旧状态：断言与稳定计数都随驻留绑定
        self._assertions.clear()
        for key in [k for k in self._stable if k[0] == source_name]:
            del self._stable[key]
        self._gate_pending = False
        self._gate_message = ""
        self._current = target_name
        self._timer.arm(target.timeout_seconds)
        self._retry = RetryController(target.retry_spec, self._clock)
        self._coverage.record_state_entered(target_name)
        self._entry_pending = True
        return self._try_complete_entry(target, snapshot, intents, depth=depth)

    # ------------------------------------------------------------------
    # 断言（FSM-007）
    # ------------------------------------------------------------------

    def _check_assertions(
        self, snapshot: PerceptionSnapshot, intents: list[InputIntent]
    ) -> TickResult | None:
        """评估挂起断言；超时失败则强制迁入出口状态并保留证据。"""
        satisfied, failed = self._assertions.evaluate(snapshot, self._evaluator)
        for assertion in satisfied:
            self._assertions.remove(assertion)
        if failed is None:
            return None
        self._assertions.remove(failed)
        source = self._current
        self.last_assert_failure = {
            "kind": failed.kind,
            "when": failed.when_src,
            "message": failed.message,
            "timeout_seconds": failed.timeout_seconds,
            "elapsed_seconds": self._clock.now() - failed.created_at,
            "failure_state": failed.failure_state,
            "tick": self._tick_no,
            "state": source,
            "snapshot": canonical_snapshot(snapshot),
        }
        early = self._begin_enter(source, failed.failure_state, snapshot, intents)
        if early is not None:
            return early
        return self._result(
            snapshot, source, failed.failure_state, None, (), intents, False, None, via="assert"
        )

    # ------------------------------------------------------------------
    # 动作 -> 意图（只构造，绝不执行）
    # ------------------------------------------------------------------

    def _run_steps(
        self,
        steps: tuple[CompiledAction | CompiledAssertion, ...],
        snapshot: PerceptionSnapshot,
        cause: str,
        out: list[InputIntent],
    ) -> None:
        """执行动作序列：断言登记 / 人工闸门 / 输入意图构造。"""
        for step in steps:
            if isinstance(step, CompiledAssertion):
                self._assertions.register(
                    kind=step.kind,
                    ast=step.ast,
                    when_src=step.when_src,
                    timeout_seconds=step.timeout_seconds,
                    failure_state=step.failure_state,
                    message=step.message,
                )
                continue
            if step.kind == "manual_gate":
                message = str(step.params.get("message", ""))
                self._gate_pending = True
                self._gate_message = message
                self._emit(
                    make_intent(
                        self._session_id,
                        self._target_id,
                        "wait",
                        {"duration_ms": 0},
                        clock=self._clock,
                        cause=f"manual_gate:{message}",
                    ),
                    out,
                )
                continue
            self._build_action_intents(step, cause, out)

    def _build_action_intents(
        self, action: CompiledAction, cause: str, out: list[InputIntent]
    ) -> None:
        """把单个输入类动作构造为 InputIntent（参数不合法 -> ValueError -> 重试路径）。"""
        kind = action.kind
        params = action.params

        def _int(name: str, *, required: bool = True, default: int | None = None) -> int | None:
            value = params.get(name, None)
            if value is None and not required:
                return default
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"动作 {kind} 的参数 {name} 必须是数字，得到 {value!r}")
            return int(value)

        def _key() -> str:
            key = params.get("key")
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"动作 {kind} 必须提供非空字符串参数 key，得到 {key!r}")
            return key

        def _emit(one_kind: str, payload: dict[str, Any]) -> None:
            self._emit(
                make_intent(self._session_id, self._target_id, one_kind, payload,
                            clock=self._clock, cause=cause),
                out,
            )

        if kind == "press_key":
            key = _key()
            payload: dict[str, Any] = {"key": key}
            duration = _int("duration_ms", required=False)
            if duration is not None:
                payload["duration_ms"] = duration
            _emit("key_down", dict(payload))
            _emit("key_up", {"key": key})
            return
        if kind in ("key_down", "key_up"):
            _emit(kind, {"key": _key()})
            return
        if kind == "click":
            button = str(params.get("button", "left"))
            if button not in _MOUSE_BUTTONS:
                raise ValueError(f"动作 click 的 button 只允许 {sorted(_MOUSE_BUTTONS)}，得到 {button!r}")
            x = _int("x")
            y = _int("y")
            if x is None or y is None:
                raise ValueError("动作 click 必须提供整数参数 x / y")
            _emit("click", {"button": button, "x": x, "y": y})
            return
        if kind == "move":
            x = _int("x")
            y = _int("y")
            if x is None or y is None:
                raise ValueError("动作 move 必须提供整数参数 x / y")
            payload = {"x": x, "y": y}
            duration = _int("duration_ms", required=False)
            if duration is not None:
                payload["duration_ms"] = duration
            _emit("move", payload)
            return
        if kind == "wheel":
            delta = _int("delta")
            if delta is None:
                raise ValueError("动作 wheel 必须提供整数参数 delta（正=向上滚）")
            payload = {"delta": delta}
            delta_ms = _int("delta_ms", required=False)
            if delta_ms is not None:
                payload["delta_ms"] = delta_ms
            _emit("wheel", payload)
            return
        if kind == "wait":
            duration = _int("duration_ms")
            if duration is None or duration < 0:
                raise ValueError("动作 wait 必须提供非负整数参数 duration_ms")
            _emit("wait", {"duration_ms": duration})
            return
        raise ValueError(f"未知运行期动作 kind: {kind!r}")  # pragma: no cover - 编译期已拦截

    def _emit(self, intent: InputIntent, out: list[InputIntent]) -> None:
        """登记意图并交给回调（不执行任何真实输入）。"""
        out.append(intent)
        if self._on_intent is not None:
            self._on_intent(intent)

    # ------------------------------------------------------------------
    # 决策哈希（FSM-009）
    # ------------------------------------------------------------------

    def _result(
        self,
        snapshot: PerceptionSnapshot,
        from_state: str,
        to_state: str | None,
        transition: CompiledTransition | None,
        guards: tuple[str, ...],
        intents: list[InputIntent],
        stopped: bool,
        stop_reason: str | None,
        *,
        via: str | None = None,
    ) -> TickResult:
        """构造 TickResult：先计算规范化决策 JSON 的 sha256。"""
        payload = {
            "v": 1,
            "machine_id": self._machine.machine_id,
            "tick": self._tick_no,
            "from_state": from_state,
            "to_state": to_state,
            "transition": None
            if transition is None
            else {
                "state": transition.source_state,
                "index": transition.decl_index,
                "when": transition.when_src,
                "to": transition.target,
                "priority": transition.priority,
                "via": via,
            },
            "guards_evaluated": list(guards),
            # 剔除 intent_id / created / expires 等易变字段，保证跨次运行一致
            "intents": [
                {"kind": i.kind, "payload": _json_safe(dict(i.payload)), "cause": i.cause}
                for i in intents
            ],
            "stopped": stopped,
            "stop_reason": stop_reason,
            "snapshot": canonical_snapshot(snapshot),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return TickResult(
            tick=self._tick_no,
            from_state=from_state,
            to_state=to_state,
            guards_evaluated=tuple(guards),
            emitted_intents=tuple(intents),
            decision_hash=digest,
            stopped=stopped,
            stop_reason=stop_reason,
        )
