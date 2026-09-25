"""SAFE-001~022 安全测试矩阵（任务文档 §9.1 安全与输入代理用例表）。

一用例一函数；断言一律引用被测模块的真实行为，不绕过被测代码：
- 输入执行统一用 RecordingSender（记录型 low_level）+ SendInputAdapter，
  绝不触发真实系统输入；
- 前台/窗口统一用 test_kit.FakeWindowSource（ForegroundSource 协议假源）；
- 时间统一用 test_kit.FakeClock（确定性，无真实等待）；
- 控制面会话/设置用 SessionManager / SettingsManager + ProjectStore 直连
  （tmp_path 持久化，无真实网络）。

无法在 M1 单测覆盖的项在 docstring 中注明 M2 归属；完整场景级验证
（Windows E2E / FSM 联动）按 §12.2 属 M1 验收的 E2E 报告与 M2 TST-007。
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from test_kit import (
    FakeClock,
    FakeForegroundContext,
    FakeInputSink,
    FakeWindowSource,
    InputBatch,
    make_click_batch,
    make_intent,
    make_session_binding,
)

from input_broker import (
    AuditLogger,
    EstopController,
    InputBroker,
    KeyLedger,
    ParentWatchdog,
    SendInputAdapter,
    KEYEVENTF_KEYUP,
    make_batch,
)
from policy_engine import (
    BudgetTracker,
    ForegroundContext,
    ModeGate,
    PolicyEvaluator,
    PolicyInput,
    PROTECTED_LOCK,
    RunMode,
    TargetRef,
)
from window_service import (
    NEEDS_MANUAL_RECONFIRM,
    BatchForegroundVerifier,
    ForegroundMonitor,
    ForegroundVerifier,
    SessionLock,
    check_access,
)
from control_plane.errors import ControlPlaneError
from control_plane.events import EventBroker
from control_plane.sessions import SessionManager
from control_plane.settings import DEFAULT_SETTINGS, SettingsManager
from control_plane.storage import ProjectStore
from domain_model.validation import validate_project_dir
from trace_format import JsonlTraceReader, JsonlTraceWriter

ROOT = Path(__file__).resolve().parents[2]
SCREEN = (1920, 1080)

#: 与 tests/unit/test_control_plane.py 对齐的合法目标载荷（写入 ProjectStore 用）
VALID_TARGET: dict[str, Any] = {
    "schema_version": 1,
    "target_id": "arena-lab",
    "executable": "ArenaLab.exe",
    "title_regex": "^ArenaLab",
    "protected_online": False,
    "allowed_display_modes": ["windowed"],
    "notes": "",
}


# ---------------------------------------------------------------- 测试替身


class RecordingSender:
    """记录型低层发送器：只记录 SendInputUnit 载荷，绝不触碰系统。"""

    def __init__(self) -> None:
        self.units: list[Any] = []
        self.calls = 0

    def __call__(self, units: list[Any]) -> int:
        self.calls += 1
        self.units.extend(units)
        return len(units)


class _FakeGate:
    """闸门替身：evaluator 只调用 is_confirmed（duck-typing 契约）。"""

    def __init__(self, *confirmed: str) -> None:
        self.confirmed = set(confirmed)

    def is_confirmed(self, session_id: str) -> bool:
        return session_id in self.confirmed


def _allow(mode: str = "real_input") -> SimpleNamespace:
    """构造一个"策略允许"决策（duck-typing，供 InputBroker.submit 使用）。"""
    return SimpleNamespace(allow=True, reasons=[], mode=mode)


def _key_batch(
    clock: FakeClock,
    keys: list[tuple[str, str]],
    *,
    session_id: str = "sess-1",
    target_id: str = "target-1",
) -> InputBatch:
    """构造按键批次（默认长 TTL，专注非 TTL 场景）。"""
    intents = [
        make_intent(session_id, target_id, kind, {"key": key}, clock=clock, ttl_ms=5000.0)
        for kind, key in keys
    ]
    return make_batch(session_id, target_id, intents, clock=clock, ttl_ms=10_000.0)


def _workspace(tmp_path: Path, *, protected: bool = False) -> SimpleNamespace:
    """构建最小控制面工作区：项目 + 目标（可选受保护）。"""
    root = tmp_path / "ws"
    store = ProjectStore(root / "projects")
    store.create_project("demo", "Demo")
    payload = {**VALID_TARGET, "protected_online": protected}
    store.create_object("demo", "targets", payload)
    sessions = SessionManager(
        root / "sessions", broker=EventBroker(buffer_size=8), store=store
    )
    settings = SettingsManager(root, store=store)
    return SimpleNamespace(root=root, store=store, sessions=sessions, settings=settings)


def _bound_broker(
    clock: FakeClock,
    sender: RecordingSender,
    source: FakeWindowSource,
    *,
    session_id: str = "sess-1",
    target_id: str = "target-1",
) -> tuple[InputBroker, Any]:
    """构建带前台复核（真实 ForegroundVerifier + 批次胶水）的 InputBroker。"""
    binding = make_session_binding(session_id, target_id, window=source())
    verifier = BatchForegroundVerifier(
        {session_id: binding}, ForegroundVerifier(source)
    )
    adapter = SendInputAdapter(sender, verifier, clock=clock, screen_size=SCREEN)
    broker = InputBroker(adapter, clock=clock, foreground_verifier=verifier)
    return broker, verifier


# ==================================================================
# SAFE-001 前台窗口不是目标
# ==================================================================


def test_safe_001_foreground_mismatch_rejects_batch_zero_real_input() -> None:
    """SAFE-001：当前前台窗口不是目标 -> 批次拒绝 + foreground_mismatch + 真实输入 0。

    双层断言：InputBroker/SendInputAdapter 的逐批复核（注入真实
    ForegroundVerifier + FakeWindowSource）与 PolicyEvaluator 的前台校验。
    """
    clock = FakeClock()
    sender = RecordingSender()
    source = FakeWindowSource()
    source.make_window(hwnd=0x100, pid=1000, exe_name="game.exe", focus=True)
    broker, _ = _bound_broker(clock, sender, source)

    first = make_click_batch("sess-1", "target-1", 2, clock=clock)
    assert broker.submit(first, _allow()).accepted_count == 2
    assert sender.calls == 1  # 前台一致：正常执行

    # 前台切到别的窗口（非目标）
    source.make_window(hwnd=0x200, pid=2000, exe_name="other.exe", focus=True)
    second = make_click_batch("sess-1", "target-1", 1, clock=clock)
    result = broker.submit(second, _allow())
    assert result.accepted_intents == []
    assert all(r.reason.startswith("foreground_mismatch") for r in result.rejected)
    assert sender.calls == 1  # 真实输入仍为第一批，错窗批次真实输入 0

    # 策略层同样给出 foreground_mismatch（失焦/错窗）
    evaluator = PolicyEvaluator(PolicyInput(mode="real_input"), gate=_FakeGate("sess-1"))
    fg = FakeForegroundContext()
    fg.focus("target-1", pid=1000)
    fg.bind(target_id="target-1", session_id="sess-1")
    assert evaluator.evaluate(first, "real_input", TargetRef("target-1"), fg, clock=clock).allow
    fg.blur()
    decision = evaluator.evaluate(first, "real_input", TargetRef("target-1"), fg, clock=clock)
    assert not decision.allow and "foreground_mismatch" in decision.reasons


# ==================================================================
# SAFE-002 HWND 相同但 PID/目标实例已变化
# ==================================================================


def test_safe_002_same_hwnd_changed_pid_requires_reconfirm() -> None:
    """SAFE-002：HWND 相同但 PID/实例已变 -> 拒绝，且必须重新人工确认。"""
    source = FakeWindowSource()
    source.make_window(hwnd=0x100, pid=1000, exe_name="game.exe", focus=True)
    binding = make_session_binding("sess-1", "target-1", window=source())
    verifier = ForegroundVerifier(source)
    assert verifier.verify(binding).ok

    # 窗口句柄复用：同 HWND，换了进程实例（PID 变化）
    source.make_window(hwnd=0x100, pid=2000, exe_name="game.exe", focus=True)
    result = verifier.verify(binding)
    assert not result.ok and "pid_mismatch" in result.reasons

    # SessionLock：失效并闘存——即使表面恢复也必须重新 bind（新 instance_token）
    lock = SessionLock(process_alive=lambda pid: True, foreground_source=source)
    source.make_window(hwnd=0x100, pid=1000, exe_name="game.exe", focus=True)  # 恢复现场
    lock.bind(binding)
    assert lock.verify().ok
    source.make_window(hwnd=0x100, pid=2000, exe_name="game.exe", focus=True)
    assert lock.verify().reasons == ("pid_changed",)
    source.make_window(hwnd=0x100, pid=1000, exe_name="game.exe", focus=True)
    reverify = lock.verify()  # 表面恢复：仍拒绝，要求重新人工确认
    assert not reverify.ok and reverify.reasons[0] == NEEDS_MANUAL_RECONFIRM
    lock.bind(make_session_binding("sess-1", "target-1", window=source(), instance_token="tok-2"))
    assert lock.verify().ok and lock.binding.instance_token == "tok-2"


# ==================================================================
# SAFE-003 意图已过 TTL
# ==================================================================


def test_safe_003_expired_intent_dropped_not_replayed() -> None:
    """SAFE-003：过期意图/批次丢弃、不补发，不影响后续新批次/新会话。"""
    clock = FakeClock(start=0.0)
    sink = FakeInputSink(clock=clock)
    broker = InputBroker(sink, clock=clock)

    stale = make_intent("sess-1", "target-1", "key_down", {"key": "a"}, clock=clock, ttl_ms=100.0)
    batch = make_batch("sess-1", "target-1", [stale], clock=clock, ttl_ms=10_000.0)
    clock.advance(0.5)  # 意图过期（批次仍新鲜）——逐意图拒绝
    result = broker.submit(batch, _allow())
    assert [r.reason for r in result.rejected] == ["expired_intent"]
    assert sink.real_executed_count == 0

    # 整批过期：同样拒绝；重复提交同一过期批次（"补发"尝试）仍被拒
    clock.advance(10.0)
    for _ in range(2):
        result = broker.submit(batch, _allow())
        assert all(r.reason == "expired_intent" for r in result.rejected)
    assert sink.real_executed_count == 0  # 没有任何补发/延迟执行
    assert sink.records == []  # 队列无积压：过期路径完全不触达执行器

    # 新鲜批次照常执行（会话连续性不受影响）
    fresh = _key_batch(clock, [("key_down", "a")])
    assert broker.submit(fresh, _allow()).accepted_count == 1
    assert sink.real_executed_count == 1

    # 新会话（Broker 重启后的新实例）不受旧过期队列影响
    sink2 = FakeInputSink(clock=clock)
    broker2 = InputBroker(sink2, clock=clock)
    new_session = _key_batch(clock, [("key_down", "b")], session_id="sess-new")
    assert broker2.submit(new_session, _allow()).accepted_count == 1


# ==================================================================
# SAFE-004 Shadow 模式产生动作意图
# ==================================================================


def test_safe_004_shadow_mode_records_intent_zero_os_input() -> None:
    """SAFE-004：Shadow 模式 -> Trace 记录意图，OS 真实输入 0。"""
    clock = FakeClock()
    sink = FakeInputSink(clock=clock)
    batch = make_click_batch("sess-1", "target-1", 2, clock=clock)

    # 策略层：shadow 永远不允许真实输入
    evaluator = PolicyEvaluator(PolicyInput(mode="shadow"))
    decision = evaluator.evaluate(
        batch, "shadow", TargetRef("target-1"),
        ForegroundContext(target_id="target-1", pid=1000), clock=clock,
    )
    assert not decision.allow and decision.reasons == ["mode_not_allowed"]

    # Shadow 记录路径：意图入账本（Trace）但标记未执行
    assert sink.record_shadow(batch) == 2
    assert sink.shadow_recorded_count == 2
    assert sink.real_executed_count == 0
    assert sink.pressed == frozenset()
    assert [r.intent.kind for r in sink.records] == ["click", "click"]

    # 纵深防御：即使决策对象被伪造为 allow，Broker 模式非 real_input 仍拒绝
    broker = InputBroker(sink, clock=clock)
    result = broker.submit(batch, SimpleNamespace(allow=True, reasons=[], mode="shadow"))
    assert all(r.reason == "mode_not_allowed" for r in result.rejected)
    assert sink.real_executed_count == 0 and len(sink.records) == 2  # 无 execute 路径记录


# ==================================================================
# SAFE-005 Dry Run 单步调试
# ==================================================================


def test_safe_005_dry_run_step_viewable_zero_os_input() -> None:
    """SAFE-005：Dry Run 单步调试 -> 可查看状态和意图，OS 输入 0。"""
    clock = FakeClock()
    sink = FakeInputSink(clock=clock)
    batch = _key_batch(clock, [("key_down", "ctrl"), ("key_up", "ctrl")])

    evaluator = PolicyEvaluator(PolicyInput(mode="dry_run"))
    decision = evaluator.evaluate(
        batch, "dry_run", TargetRef("target-1"),
        ForegroundContext(target_id="target-1", pid=1000), clock=clock,
    )
    assert not decision.allow and decision.reasons == ["mode_not_allowed"]

    # 单步记录：每条意图（kind/payload/时间）完整可查看
    sink.record_shadow(batch)
    down, up = sink.records
    assert down.intent.payload == {"key": "ctrl"} and up.intent.kind == "key_up"
    assert all(r.path == "shadow" and not r.accepted for r in sink.records)
    assert sink.real_executed_count == 0 and sink.pressed == frozenset()


# ==================================================================
# SAFE-006 RealInput 未经过人工闸门
# ==================================================================


def test_safe_006_real_input_without_manual_gate_rejected(tmp_path: Path) -> None:
    """SAFE-006：RealInput 未经过人工闸门 -> 启动被拒（策略层 + 控制面层）。"""
    clock = FakeClock()
    batch = _key_batch(clock, [("key_down", "a")])

    # 策略层：无闸门 -> manual_gate_required
    evaluator = PolicyEvaluator(PolicyInput(mode="real_input"), gate=None)
    decision = evaluator.evaluate(
        batch, "real_input", TargetRef("target-1"),
        ForegroundContext(target_id="target-1", pid=1000), clock=clock,
    )
    assert not decision.allow and "manual_gate_required" in decision.reasons

    # 同一闸门确认后即可通过（对照组）
    evaluator2 = PolicyEvaluator(PolicyInput(mode="real_input"), gate=_FakeGate("sess-1"))
    assert evaluator2.evaluate(
        batch, "real_input", TargetRef("target-1"),
        ForegroundContext(target_id="target-1", pid=1000), clock=clock,
    ).allow

    # 控制面层：real_input 会话未 confirm 直接 start -> 409 manual_gate_required
    ws = _workspace(tmp_path)
    session = ws.sessions.create("demo", "arena-lab", "real_input")
    with pytest.raises(ControlPlaneError) as excinfo:
        ws.sessions.start(session["session_id"])
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "manual_gate_required"

    # 人工确认后 start 成功（闸门即唯一解锁途径）
    ws.sessions.confirm(session["session_id"], "operator")
    assert ws.sessions.start(session["session_id"])["state"] == "running"


# ==================================================================
# SAFE-007 按键按下期间目标失焦
# ==================================================================


def test_safe_007_focus_lost_cancels_queue_and_releases_keys() -> None:
    """SAFE-007：按键按下期间目标失焦 -> 取消队列并释放所有已按下键。"""
    clock = FakeClock()
    sender = RecordingSender()
    source = FakeWindowSource()
    source.make_window(hwnd=0x100, pid=1000, exe_name="game.exe", focus=True)
    broker, _ = _bound_broker(clock, sender, source)

    assert broker.submit(
        _key_batch(clock, [("key_down", "ctrl")]), _allow()
    ).accepted_count == 1
    assert broker.key_ledger.pressed == frozenset({"ctrl"})

    # 失焦：新批次被拒（队列取消语义）
    source.make_window(hwnd=0x200, pid=2000, exe_name="other.exe", focus=True)
    queued = make_click_batch("sess-1", "target-1", 3, clock=clock)
    result = broker.submit(queued, _allow())
    assert all(r.reason.startswith("foreground_mismatch") for r in result.rejected)

    # 失焦停止路径：cancel 释放全部已按下键（补偿 key_up 不经策略/复核）
    released = broker.cancel("focus_lost")
    assert [i.payload["key"] for i in released] == ["ctrl"]
    assert broker.key_ledger.pressed == frozenset()
    assert sender.calls == 2  # 原批次 + 补偿释放
    assert sender.units[-1].flags & KEYEVENTF_KEYUP
    # 取消后旧队列残余批次绝不再执行
    assert all(
        r.reason == "broker_cancelled"
        for r in broker.submit(make_click_batch("sess-1", "target-1", 1, clock=clock), _allow()).rejected
    )


# ==================================================================
# SAFE-008 全局急停（UI 正常）
# ==================================================================


def test_safe_008_global_estop_enters_stopped_with_audit(tmp_path: Path) -> None:
    """SAFE-008：全局急停（UI 正常）-> 状态进入 stopped，真实输入停止，审计入链。

    延迟门槛（热键事件到禁止新输入 p95 ≤ 150 ms）属 §7.2 性能口径，
    由基准机性能测试度量；单测断言触发时刻被单调时钟审计记录。
    """
    clock = FakeClock(start=7.0)
    sender = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sender, clock=clock, screen_size=SCREEN), clock=clock
    )
    trace = tmp_path / "trace.jsonl"
    estop = EstopController(broker, AuditLogger(JsonlTraceWriter(trace), clock=clock), clock=clock)

    assert broker.submit(
        _key_batch(clock, [("key_down", "ctrl"), ("key_down", "shift")]), _allow()
    ).accepted_count == 2

    assert estop.trigger("hotkey", "operator pressed Ctrl+Alt+F12") is True
    # 状态进入 stopped：新批次一律拒绝
    result = broker.submit(_key_batch(clock, [("key_down", "a")]), _allow())
    assert all(r.reason == "broker_stopped" for r in result.rejected)
    assert broker.cancelled is True
    # 全量释放（无卡键）
    assert broker.key_ledger.pressed == frozenset()
    assert len([u for u in sender.units if u.flags & KEYEVENTF_KEYUP]) == 2
    # 审计：estop 事件带来源/释放数，触发时刻 = 单调时钟值
    assert estop.records[0].source == "hotkey"
    assert estop.records[0].ts_monotonic == pytest.approx(7.0)
    events = JsonlTraceReader(trace).read()
    estop_events = [e for e in events if e.type == "estop"]
    assert len(estop_events) == 1
    assert estop_events[0].payload["source"] == "hotkey"
    assert estop_events[0].payload["released_count"] == 2


# ==================================================================
# SAFE-009 全局急停（UI 主线程卡死）
# ==================================================================


def test_safe_009_estop_effective_while_ui_thread_blocked() -> None:
    """SAFE-009：UI 主线程卡死 -> 急停仍生效、真实输入停止。

    EstopController 与 UI 完全解耦（来源只是字符串）：模拟 UI 线程
    阻塞在"消息循环"中，从另一线程直接触发急停，不经过任何 UI 路径。
    """
    clock = FakeClock()
    sender = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sender, clock=clock, screen_size=SCREEN), clock=clock
    )
    estop = EstopController(broker, clock=clock)
    assert broker.submit(_key_batch(clock, [("key_down", "a")]), _allow()).accepted_count == 1

    # 模拟 UI 主线程卡死：进入后停在"消息循环"里不再处理任何事件
    ui_entered = threading.Event()
    ui_released = threading.Event()
    def wedged_ui_loop() -> None:
        ui_entered.set()
        ui_released.wait(timeout=5.0)  # 卡死中：不响应任何 UI 请求

    ui_thread = threading.Thread(target=wedged_ui_loop, name="ui-main", daemon=True)
    ui_thread.start()
    assert ui_entered.wait(timeout=2.0) is True

    # 另一线程直接触发急停（热键/看门狗路径，与 UI 无关）
    trigger_result: list[bool] = []
    def fire_estop() -> None:
        trigger_result.append(estop.trigger("hotkey", "ui is wedged"))

    worker = threading.Thread(target=fire_estop, name="estop-hotkey", daemon=True)
    worker.start()
    worker.join(timeout=2.0)
    assert trigger_result == [True]
    assert estop.triggered is True
    assert broker.cancelled is True  # 真实输入已停止
    assert broker.key_ledger.pressed == frozenset()  # 已按键全部释放
    assert any(u.flags & KEYEVENTF_KEYUP for u in sender.units)
    assert all(
        r.reason == "broker_stopped"
        for r in broker.submit(_key_batch(clock, [("key_down", "b")]), _allow()).rejected
    )

    ui_released.set()  # 清理：解除模拟卡死
    ui_thread.join(timeout=2.0)


# ==================================================================
# SAFE-010 Runtime 父进程崩溃
# ==================================================================


def test_safe_010_parent_death_watchdog_releases_and_rejects(tmp_path: Path) -> None:
    """SAFE-010：Runtime 父进程崩溃 -> Watchdog 释放全部按键 + 拒绝旧会话后续意图。"""
    clock = FakeClock()
    sender = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sender, clock=clock, screen_size=SCREEN), clock=clock
    )
    estop = EstopController(
        broker,
        AuditLogger(JsonlTraceWriter(tmp_path / "trace.jsonl"), clock=clock),
        clock=clock,
    )
    watchdog = ParentWatchdog(
        parent_pid=424242,
        interval=0.01,
        on_dead=lambda: estop.trigger("watchdog", "parent gone"),
        probe=lambda pid: False,  # 假探活：父进程已死
    )
    assert broker.submit(_key_batch(clock, [("key_down", "ctrl")]), _allow()).accepted_count == 1

    assert watchdog.poll_once() is True
    assert watchdog.dead_fired is True
    assert estop.triggered is True
    assert broker.key_ledger.pressed == frozenset()  # 释放全部按键
    assert any(u.flags & KEYEVENTF_KEYUP for u in sender.units)
    # Broker 拒绝后续旧会话意图
    assert all(
        r.reason == "broker_stopped"
        for r in broker.submit(_key_batch(clock, [("key_up", "ctrl")]), _allow()).rejected
    )
    assert watchdog.poll_once() is False  # on_dead 至多触发一次


# ==================================================================
# SAFE-011 Broker 自身异常重启
# ==================================================================


def test_safe_011_broker_crash_restart_no_queue_replay() -> None:
    """SAFE-011：Broker 自身异常重启 -> 不重放旧队列；键状态进入安全复位。"""
    clock = FakeClock()
    sender1 = RecordingSender()
    source1 = FakeWindowSource()
    source1.make_window(hwnd=0x100, pid=1000, exe_name="game.exe", focus=True)
    broker1, _ = _bound_broker(clock, sender1, source1, session_id="sess-old")
    old_batch = _key_batch(clock, [("key_down", "ctrl")], session_id="sess-old")
    assert broker1.submit(old_batch, _allow()).accepted_count == 1

    # 模拟异常退出：不经过 stop/释放路径，旧实例连同其键状态一起消失
    crashed_batch = _key_batch(clock, [("key_down", "shift")], session_id="sess-old")

    # 重启：全新 Broker 实例（新账本、空绑定注册表）
    sender2 = RecordingSender()
    source2 = FakeWindowSource()
    source2.make_window(hwnd=0x100, pid=1000, exe_name="game.exe", focus=True)
    verifier2 = BatchForegroundVerifier({}, ForegroundVerifier(source2))  # 无任何旧绑定
    broker2 = InputBroker(
        SendInputAdapter(sender2, verifier2, clock=clock, screen_size=SCREEN),
        clock=clock,
        foreground_verifier=verifier2,
    )
    # 安全复位：新账本无任何卡键
    assert broker2.key_ledger.pressed == frozenset()

    # 旧队列重放尝试：旧会话在新实例中无绑定 -> 拒绝且零输入
    for replay in (old_batch, crashed_batch):
        result = broker2.submit(replay, _allow())
        assert result.accepted_intents == []
        assert all(
            r.reason == "foreground_mismatch:session_not_bound" for r in result.rejected
        )
    assert sender2.calls == 0  # 没有任何旧意图被执行（不重放）
    assert broker2.key_ledger.pressed == frozenset()


# ==================================================================
# SAFE-012 动作数预算耗尽
# ==================================================================


def test_safe_012_action_budget_exhausted_rejects_current_and_future() -> None:
    """SAFE-012：动作数预算耗尽 -> 当前及后续动作被拒绝，会话按策略停止输入。"""
    clock = FakeClock()
    tracker = BudgetTracker(
        clock=clock,
        policy=PolicyInput(max_total_actions=3, max_actions_per_minute=1000),
        max_total_actions=3,
        max_actions_per_minute=1000,
        max_runtime_minutes=60.0,
        start_monotonic=0.0,
    )
    assert tracker.consume(3, now=0.0) == (True, None)
    ok, reason = tracker.consume(1, now=1.0)
    assert ok is False and reason == "budget_exhausted:total_actions"
    assert tracker.state().total_actions == 3  # 拒绝不产生计数变化

    # 策略层：耗尽后同一会话的后续批次持续被拒（预算器从零开始）
    evaluator = PolicyEvaluator(PolicyInput(mode="real_input"), gate=_FakeGate("sess-1"))
    fg = FakeForegroundContext()
    fg.focus("target-1", pid=1000)
    fg.bind(target_id="target-1", session_id="sess-1")
    target = TargetRef("target-1")
    session_tracker = BudgetTracker(
        clock=clock,
        max_total_actions=3,
        max_actions_per_minute=1000,
        max_runtime_minutes=60.0,
        start_monotonic=0.0,
    )
    first = make_click_batch("sess-1", "target-1", 3, clock=clock)
    assert evaluator.evaluate(first, "real_input", target, fg, session_tracker, clock).allow
    for _ in range(2):  # 当前被拒 + 后续仍被拒
        nxt = make_click_batch("sess-1", "target-1", 1, clock=clock)
        decision = evaluator.evaluate(nxt, "real_input", target, fg, session_tracker, clock)
        assert not decision.allow
        assert "budget_exhausted:total_actions" in decision.reasons


# ==================================================================
# SAFE-013 连续按键时间超限
# ==================================================================


def test_safe_013_continuous_key_hold_over_limit_releases() -> None:
    """SAFE-013：连续按键时间超限 -> 自动释放对应键并停止。

    M1 边界说明：InputBroker 尚未实现"单键连续按住时长"预算的运行时
    接线（属 M2 FSM/调度）；此处断言 M1 已有的两个机制：
    1. BudgetTracker 对超限扣减返回 False 并给出机器可读原因；
    2. KeyLedger 能对仍按住的键生成补偿释放（停止路径可用）。
    """
    clock = FakeClock(start=0.0)
    ledger = KeyLedger()
    ledger.observe(make_intent("s", "t", "key_down", {"key": "ctrl"}, clock=clock))
    assert ledger.pressed == frozenset({"ctrl"})  # 键处于连续按住状态

    # 预算器：按住时长折算的运行预算超限 -> 拒绝扣减（False + 原因）
    tracker = BudgetTracker(
        clock=clock,
        max_runtime_minutes=0.5,  # 30 秒上限（模拟连续按住容忍窗口）
        max_actions_per_minute=10**9,
        max_total_actions=10**9,
        start_monotonic=0.0,
    )
    assert tracker.is_runtime_exceeded(now=29.9) is False
    assert tracker.is_runtime_exceeded(now=30.0) is True
    ok, reason = tracker.consume(1, now=30.0)
    assert ok is False and reason == "runtime_limit_reached"

    # 停止路径：超限后释放仍按住的键
    ups = ledger.release_all(30.0, cause="hold_over_limit")
    assert [i.payload["key"] for i in ups] == ["ctrl"]
    assert ledger.pressed == frozenset()
    assert ledger.release_all(30.1, cause="again") == []  # 幂等，无重复释放


# ==================================================================
# SAFE-014 运行时长达到硬上限
# ==================================================================


def test_safe_014_runtime_hard_limit_irrecoverable_stop() -> None:
    """SAFE-014：运行时长达到硬上限 -> 进入不可自动恢复停止态。"""
    clock = FakeClock(start=0.0)
    tracker = BudgetTracker(
        clock=clock,
        max_runtime_minutes=1.0,
        max_actions_per_minute=10**9,
        max_total_actions=10**9,
        start_monotonic=0.0,
    )
    clock.advance(59.9)
    assert tracker.is_runtime_exceeded() is False
    clock.advance(0.1)  # 恰好 60s（达到即超）
    assert tracker.is_runtime_exceeded() is True

    # 策略层：达到上限后批次被拒且原因机器可读
    evaluator = PolicyEvaluator(PolicyInput(mode="real_input"), gate=_FakeGate("sess-1"))
    fg = FakeForegroundContext()
    fg.focus("target-1", pid=1000)
    fg.bind(target_id="target-1", session_id="sess-1")
    batch = make_click_batch("sess-1", "target-1", 1, clock=clock)
    decision = evaluator.evaluate(batch, "real_input", TargetRef("target-1"), fg, tracker, clock)
    assert not decision.allow and "runtime_limit_reached" in decision.reasons

    # 不可自动恢复：时间继续流逝仍超限；BudgetTracker 不提供任何复位接口
    clock.advance(3600.0)
    assert tracker.is_runtime_exceeded() is True
    assert tracker.consume(1) == (False, "runtime_limit_reached")
    assert not any("reset" in name for name in dir(tracker))


# ==================================================================
# SAFE-015 不在允许运行时间窗
# ==================================================================


def test_safe_015_outside_time_window_cannot_enter_real_input(tmp_path: Path) -> None:
    """SAFE-015：不在允许运行时间窗 -> 会话不能进入 RealInput。

    M1 边界说明：PolicyInput 目前只有 unattended_schedule（enabled/disabled）
    开关字段，无细粒度时间窗字段；时段过滤的运行时接线属 M2。此处断言
    字段级防线：非法时间窗取值被 422 拒绝，且无人值守状态下 RealInput
    仍必须经过人工闸门（未确认 -> 409）。
    """
    assert PolicyInput().unattended_schedule == "disabled"  # 默认关闭
    assert not any(
        "window" in field or "hour" in field or "schedule_at" in field
        for field in PolicyInput().__dataclass_fields__
    )  # 当前策略模型无时间窗字段（字段级事实）

    ws = _workspace(tmp_path)
    # 非法时间窗取值 -> 字段级拒绝（422 invalid_enum）
    with pytest.raises(ControlPlaneError) as excinfo:
        ws.settings.update({"unattended_schedule": "daily-02:00-04:00"})
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "invalid_enum"

    # 即使（非受保护工作区）开启无人值守开关，RealInput 仍必须人工启动
    ws.settings.update({"unattended_schedule": "enabled"})
    session = ws.sessions.create("demo", "arena-lab", "real_input")
    with pytest.raises(ControlPlaneError) as excinfo:
        ws.sessions.start(session["session_id"])
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "manual_gate_required"


# ==================================================================
# SAFE-016 锁屏 / 用户切换 / RDP 状态变化
# ==================================================================


def test_safe_016_lock_screen_no_foreground_triggers_safe_stop() -> None:
    """SAFE-016：锁屏/前台消失/查询失败 -> 立即安全停止，不自动抢回焦点。

    ForegroundMonitor 以 (old, None) 回调表达"状态不确定，必须安全停止"；
    ForegroundVerifier / SessionLock / PolicyEvaluator 全部默认拒绝；
    全仓无任何"恢复/抢回焦点"接口（监视器只读，绝不操作窗口）。
    """
    source = FakeWindowSource()
    win = source.make_window(hwnd=0x100, pid=1000, exe_name="game.exe", focus=True)
    changes: list[tuple[Any, Any]] = []
    monitor = ForegroundMonitor(
        source, interval=0.05, on_change=lambda o, n: changes.append((o, n)),
        clock=FakeClock(), sleep=lambda s: None,
    )
    assert monitor.poll_once() is win  # 正常前台

    source.focus(None)  # 锁屏 / 用户切换 / RDP 断开 -> 无前台
    assert monitor.poll_once() is None
    assert changes[-1] == (win, None)  # 安全停止信号
    # win32 查询异常同样转换为安全停止信号（绝不吞异常继续输入）
    source.fail_with(RuntimeError("GetForegroundWindow failed"))
    assert monitor.poll_once() is None
    assert changes[-1] == (None, None)
    assert monitor.current() is None

    # 复核层：win32 查询异常 -> foreground_query_failed；无前台 -> no_foreground_window
    binding = make_session_binding("sess-1", "target-1", window=win)
    verifier = ForegroundVerifier(source)
    assert "foreground_query_failed" in verifier.verify(binding).reasons
    source.fail_with(None)
    source.focus(None)
    assert "no_foreground_window" in verifier.verify(binding).reasons

    # 会话锁：失效并闘存，需人工重新确认（不自动恢复）
    lock = SessionLock(process_alive=lambda pid: True, foreground_source=source)
    lock.bind(binding)
    assert "no_foreground_window" in lock.verify().reasons
    source.make_window(hwnd=0x100, pid=1000, exe_name="game.exe", focus=True)
    assert lock.verify().reasons[0] == NEEDS_MANUAL_RECONFIRM

    # 策略层：前台上下文缺失 -> 默认拒绝
    decision = PolicyEvaluator().evaluate(
        make_click_batch("s", "t", 1, clock=FakeClock()),
        "observe", TargetRef("t"), None, clock=FakeClock(),
    )
    assert "missing_foreground_context" in decision.reasons


# ==================================================================
# SAFE-017 目标权限高于工作台（UIPI）
# ==================================================================


def test_safe_017_uipi_elevated_target_reports_no_fake_success() -> None:
    """SAFE-017：目标权限高于工作台 -> 明确提示 UIPI/权限问题，不报告假成功。"""
    from test_kit import WindowInfo

    normal = WindowInfo(
        hwnd=0x100, pid=1000, exe_name="game.exe", title="Game",
        class_name="UnrealWindow", visible=True, minimized=False, monitor_index=0,
    )
    assert check_access(normal, probe=lambda pid: None) == []  # 正常 -> 空（且仅此时为空）

    # 疑似提权差异：受限句柄可开但映像读不到 -> 明确 UIPI 原因
    assert check_access(normal, probe=lambda pid: "target_elevated_uipi") == [
        "target_elevated_uipi"
    ]
    # 探针异常：状态不确定 -> 如实报告，绝不假装可输入
    def broken(pid: int) -> str | None:
        raise RuntimeError("probe crashed")

    assert check_access(normal, probe=broken) == ["process_probe_failed"]
    # 叠加问题全部列出（可见性 + 最小化 + 权限）
    hidden = WindowInfo(
        hwnd=0x200, pid=2000, exe_name="game.exe", title="Game",
        class_name="UnrealWindow", visible=False, minimized=True, monitor_index=0,
    )
    reasons = check_access(hidden, probe=lambda pid: "process_open_denied")
    assert reasons == ["window_not_visible", "window_minimized", "process_open_denied"]


# ==================================================================
# SAFE-018 Stop 被重复调用
# ==================================================================


def test_safe_018_stop_idempotent_keys_stay_released() -> None:
    """SAFE-018：Stop 被重复调用 -> 幂等、无异常、所有键保持释放。"""
    clock = FakeClock()
    sender = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sender, clock=clock, screen_size=SCREEN), clock=clock
    )
    assert broker.submit(_key_batch(clock, [("key_down", "ctrl")]), _allow()).accepted_count == 1

    first = broker.stop()
    assert [i.payload["key"] for i in first] == ["ctrl"]
    second = broker.stop()  # 重复调用：无异常、无额外效果
    assert second == []
    assert broker.stop() == []
    assert broker.key_ledger.pressed == frozenset()  # 键保持释放
    assert sender.calls == 2  # 仅原批次 + 一次补偿释放
    # 停止后提交始终被拒
    for _ in range(2):
        assert all(
            r.reason == "broker_stopped"
            for r in broker.submit(_key_batch(clock, [("key_down", "a")]), _allow()).rejected
        )

    # 急停触发同样幂等（闘存；重复触发无异常、无重复审计记录）
    estop = EstopController(broker, clock=clock)
    assert [estop.trigger("ui"), estop.trigger("ui"), estop.trigger("ui")] == [True, False, False]


def test_safe_018b_control_plane_stop_idempotent(tmp_path: Path) -> None:
    """SAFE-018（控制面侧）：stop 重复调用幂等返回终态记录，不报错。"""
    ws = _workspace(tmp_path)
    session = ws.sessions.create("demo", "arena-lab", "shadow")
    sid = session["session_id"]
    assert ws.sessions.start(sid)["state"] == "running"
    first = ws.sessions.stop(sid)
    second = ws.sessions.stop(sid)
    assert first["state"] == second["state"] == "stopped"
    assert first["session_id"] == second["session_id"]


# ==================================================================
# SAFE-019 两个 RealInput 会话并发启动
# ==================================================================


def test_safe_019_second_real_input_session_rejected(tmp_path: Path) -> None:
    """SAFE-019：两个 RealInput 会话并发启动 -> 第二个被拒绝（Broker + 控制面）。"""
    clock = FakeClock()
    sender = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sender, clock=clock, screen_size=SCREEN), clock=clock
    )
    # Broker 层：会话席位先到先得
    assert broker.claim_real_session("sess-a") is True
    assert broker.claim_real_session("sess-b") is False
    batch_b = _key_batch(clock, [("key_down", "a")], session_id="sess-b")
    result = broker.submit(batch_b, _allow())
    assert all(r.reason == "session_mismatch" for r in result.rejected)
    assert sender.calls == 0

    # 控制面层：第二个活跃 real_input 会话创建即 409
    ws = _workspace(tmp_path)
    ws.sessions.create("demo", "arena-lab", "real_input")
    with pytest.raises(ControlPlaneError) as excinfo:
        ws.sessions.create("demo", "arena-lab", "real_input")
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "real_input_session_exists"

    # 首个会话经人工闸门启动后停止（释放席位）即可再建
    sessions = ws.sessions.list_sessions()
    first_sid = sessions[0]["session_id"]
    ws.sessions.confirm(first_sid, "operator")
    ws.sessions.start(first_sid)
    ws.sessions.stop(first_sid)
    assert ws.sessions.create("demo", "arena-lab", "real_input")["state"] == "created"


# ==================================================================
# SAFE-020 protected_online 手改 real_input
# ==================================================================


def test_safe_020_protected_online_double_hard_lock() -> None:
    """SAFE-020：protected_online 手改 real_input=true -> Schema/Policy 双重拒绝。

    三层断言：domain_model 校验器（Schema/规则层，规则 ID
    protected_online_no_real_input）、ModeGate（人工确认层）、
    PolicyEvaluator（运行时硬锁，即使闸门被伪造也拒绝）。
    """
    # 1) 规则层：反例项目必须被拒绝并给出规则 ID
    result = validate_project_dir(ROOT / "examples" / "protected_online_demo")
    assert not result.ok
    rules = [issue.rule for issue in result.issues]
    assert "protected_online_no_real_input" in rules

    # 2) 人工闸门层：受保护目标确认直接拒绝，拒绝入闸门审计
    gate = ModeGate(clock=FakeClock())
    protected = TargetRef("example-online", protected_online=True)
    assert gate.confirm("sess-1", "operator", target=protected) is None
    assert "protected_online_no_real_input" in gate.refusal_reasons()
    assert not gate.is_confirmed("sess-1")

    # 3) 运行时层：即使闸门状态被伪造（硬编码 is_confirmed=True），硬锁仍拒绝
    evaluator = PolicyEvaluator(PolicyInput(mode="real_input"), gate=_FakeGate("sess-1"))
    clock = FakeClock()
    fg = FakeForegroundContext()
    fg.focus("example-online", pid=1000)
    fg.bind(target_id="example-online", session_id="sess-1")
    batch = make_click_batch("sess-1", "example-online", 1, clock=clock)
    decision = evaluator.evaluate(batch, "real_input", protected, fg, clock=clock)
    assert not decision.allow and PROTECTED_LOCK in decision.reasons


# ==================================================================
# SAFE-021 掉线样式弹窗出现
# ==================================================================


def test_safe_021_disconnect_dialog_defaults_to_stop_and_notify(tmp_path: Path) -> None:
    """SAFE-021：掉线样式弹窗出现 -> 默认停止并通知，不自动重连继续。

    M1 边界说明：完整"掉线弹窗检测 -> FSM 停止迁移 -> 通知"场景属
    M2（FSM/检测器未交付）；M1 断言默认安全姿态（observe/shadow 默认
    值 + 默认拒绝）与审计通知机制（事件入链可读）。
    """
    # 默认姿态：策略与工作区默认都是最低风险，任何真实输入默认被拒
    assert PolicyInput().mode == "observe"
    assert DEFAULT_SETTINGS["default_mode"] == "shadow"
    assert DEFAULT_SETTINGS["unattended_schedule"] == "disabled"
    clock = FakeClock()
    decision = PolicyEvaluator().evaluate(
        make_click_batch("s", "t", 1, clock=clock),
        "observe", TargetRef("t"),
        ForegroundContext(target_id="t", pid=1000), clock=clock,
    )
    assert not decision.allow and decision.reasons == ["mode_not_allowed"]

    # 通知机制：异常事件（如掉线弹窗）写入审计链并可读回
    trace = tmp_path / "trace.jsonl"
    audit = AuditLogger(JsonlTraceWriter(trace), clock=clock)
    audit.log_anomaly(
        "disconnect_dialog_detected",
        {"hint": "默认停止，等待人工处理"},
        session_id="sess-1",
    )
    events = JsonlTraceReader(trace).read()
    assert len(events) == 1 and events[0].type == "anomaly"
    assert events[0].payload["kind"] == "disconnect_dialog_detected"
    assert events[0].session_id == "sess-1"


# ==================================================================
# SAFE-022 受保护目标尝试定时自动启动
# ==================================================================


def test_safe_022_protected_target_scheduled_autostart_rejected(tmp_path: Path) -> None:
    """SAFE-022：受保护目标尝试定时自动启动 -> 策略拒绝并记录原因。

    M1 边界说明：定时调度执行器属 M2；M1 断言配置面与规则层的双重
    拒绝，且拒绝原因（错误码/规则 ID）机器可读地记录下来。
    """
    # 配置面：存在受保护目标时，无人值守调度与 real_input 默认模式都被拒
    ws = _workspace(tmp_path, protected=True)
    with pytest.raises(ControlPlaneError) as excinfo:
        ws.settings.update({"unattended_schedule": "enabled"})
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "protected_online_unattended_forbidden"
    with pytest.raises(ControlPlaneError) as excinfo:
        ws.settings.update({"default_mode": "real_input"})
    assert excinfo.value.code == "protected_online_no_real_input"
    # 拒绝后设置保持安全默认（未落盘任何危险值）
    current = ws.settings.get()
    assert current["unattended_schedule"] == "disabled"
    assert current["default_mode"] == "shadow"

    # 规则层：受保护目标 + unattended_schedule enabled -> 规则 ID 记录原因
    demo = ROOT / "examples" / "protected_online_demo"
    work = tmp_path / "unattended-protected"
    work.mkdir()
    for item in demo.rglob("*"):
        if item.is_file():
            rel = item.relative_to(demo)
            dest = work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                item.read_text(encoding="utf-8").replace(
                    "unattended_schedule: disabled", "unattended_schedule: enabled"
                ),
                encoding="utf-8",
            )
    result = validate_project_dir(work)
    assert not result.ok
    assert "protected_online_no_unattended" in [issue.rule for issue in result.issues]
