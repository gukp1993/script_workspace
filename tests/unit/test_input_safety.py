"""安全输入层单测（E08：INP-003/005/006/007/009/010 + SAFE 用例单测版）。

覆盖：
- SendInputAdapter：注入记录型 sender 断言 SendInput 载荷（扫描码/标志/次数）、
  前台不符零真实发送（SAFE-001）、TTL 过期跳过（SAFE-003）、取消屏障（INP-009）；
- InputBroker：可注入前台复核（INP-005）、取消竞态拦截、SAFE-019 集成复断；
- EstopController：急停释放全部键 + 新批次拒绝（SAFE-008）、幂等（SAFE-018）；
- ParentWatchdog：父进程死亡/心跳超时 -> 释放全部键（SAFE-010 风格）；
- EstopHotkey：假注册器触发 -> broker 停止；注册失败给明确错误；
- AuditLogger：拒绝/急停/释放事件入 JSONL 哈希链（INP-010）；
- 静态守卫镜像：input_broker 内 ctypes 只允许三个 win32 文件。
"""

from __future__ import annotations

import ast
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from common import FakeClock
from trace_format import JsonlTraceReader, JsonlTraceWriter

import input_broker
from input_broker import (
    AuditLogger,
    EstopController,
    EstopHotkey,
    InputBroker,
    KeyLedger,
    ParentWatchdog,
    ScanCodes,
    SendInputAdapter,
    SinkContext,
    make_batch,
    make_intent,
)
from input_broker.win32_adapter import (
    ABSOLUTE_MAX,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_WHEEL,
    SendInputUnit,
    send_units_real,
)
from input_broker.win32_hotkey import ERROR_REGISTER_FAILED

SCREEN = (1920, 1080)


# ---------------------------------------------------------------- 测试替身


class RecordingSender:
    """记录型低层发送器：只记录 SendInputUnit 载荷，绝不触碰系统。"""

    def __init__(self, *, inserted: int | None = None) -> None:
        self.units: list[SendInputUnit] = []
        self.calls = 0
        self.inserted = inserted  # None 表示按实际数量成功

    def __call__(self, units: list[SendInputUnit]) -> int:
        self.calls += 1
        self.units.extend(units)
        return len(units) if self.inserted is None else self.inserted


@dataclass
class FakeVerdict:
    """前台复核结果的 duck-typing 替身。"""

    ok: bool
    reasons: tuple[str, ...] = ()


class FakeVerifier:
    """可编程前台复核器：记录调用并可切换判定。"""

    def __init__(self, ok: bool = True, reasons: tuple[str, ...] = ()) -> None:
        self.ok = ok
        self.reasons = reasons
        self.calls: list[str] = []

    def verify_batch(self, batch: object) -> FakeVerdict:
        self.calls.append(str(getattr(batch, "batch_id", "")))
        return FakeVerdict(self.ok, self.reasons)


class FakeRegistrar:
    """假热键注册器：可控制注册成败并手动触发回调。"""

    def __init__(self, *, register_ok: bool = True) -> None:
        self.register_ok = register_ok
        self.registered = False
        self.unregistered = False
        self._fired = threading.Event()

    def register(self) -> bool:
        self.registered = self.register_ok
        return self.register_ok

    def pump_once(self, timeout_ms: int) -> bool:
        return self._fired.wait(timeout=max(0, timeout_ms) / 1000.0)

    def fire(self) -> None:
        self._fired.set()

    def unregister(self) -> None:
        self.unregistered = True


def _key_batch(clock: FakeClock, keys: list[tuple[str, str]], *, ttl_ms: float = 5000.0):
    intents = [
        make_intent("s", "t", kind, {"key": key}, clock=clock, ttl_ms=ttl_ms)
        for kind, key in keys
    ]
    return make_batch("s", "t", intents, clock=clock, ttl_ms=10_000.0)


def _allow(mode: str = "real_input") -> SimpleNamespace:
    return SimpleNamespace(allow=True, reasons=[], mode=mode)


# ---------------------------------------------------------------- INP-003 载荷


def test_scan_codes_table_covers_required_keys() -> None:
    """扫描码表覆盖：字母/数字/空格/回车/Esc/方向键/Shift/Ctrl/Alt。"""
    assert ScanCodes.lookup("a") == (0x1E, False)
    assert ScanCodes.lookup("ESC") == (0x01, False)
    assert ScanCodes.lookup("space") == (0x39, False)
    assert ScanCodes.lookup("enter") == (0x1C, False)
    assert ScanCodes.lookup("ctrl") == (0x1D, False)
    assert ScanCodes.lookup("shift") == (0x2A, False)
    assert ScanCodes.lookup("alt") == (0x38, False)
    for key, code in (("up", 0x48), ("left", 0x4B), ("right", 0x4D), ("down", 0x50)):
        result = ScanCodes.lookup(key)
        assert result is not None and result[0] == code and result[1] is True  # 扩展键
    for i in range(10):
        assert ScanCodes.lookup(str(i)) is not None
    for ch in "abcdefghijklmnopqrstuvwxyz":
        assert ScanCodes.lookup(ch) is not None
    assert ScanCodes.lookup("f13") is None
    assert ScanCodes.lookup(None) is None


def test_adapter_key_payload_uses_scancode_flags() -> None:
    """key_down/key_up 载荷：KEYEVENTF_SCANCODE、按下/释放标志、次数。"""
    clock = FakeClock()
    sender = RecordingSender()
    adapter = SendInputAdapter(sender, clock=clock, screen_size=SCREEN)
    batch = _key_batch(clock, [("key_down", "a"), ("key_up", "A")])
    result = adapter.execute(batch)
    assert result.accepted_count == 2 and result.rejected == []
    assert sender.calls == 1
    down, up = sender.units
    assert down.type == "key" and down.scancode == 0x1E  # "A" 归一化同 "a"
    assert down.flags & KEYEVENTF_SCANCODE
    assert not down.flags & KEYEVENTF_KEYUP
    assert up.flags & KEYEVENTF_SCANCODE and up.flags & KEYEVENTF_KEYUP
    assert adapter.sent_units == sender.units  # 观测口径一致


def test_adapter_click_and_move_absolute_normalization() -> None:
    """点击/移动：0~65535 绝对坐标归一化 + MOUSEEVENTF 标志。"""
    clock = FakeClock()
    sender = RecordingSender()
    adapter = SendInputAdapter(sender, clock=clock, screen_size=SCREEN)
    intents = [
        make_intent("s", "t", "click", {"button": "left", "x": 960, "y": 540}, clock=clock),
        make_intent("s", "t", "move", {"x": 0, "y": 1080}, clock=clock),
    ]
    result = adapter.execute(make_batch("s", "t", intents, clock=clock))
    assert result.accepted_count == 2
    click_down, click_up, move = sender.units
    expected_dx = round(960 * ABSOLUTE_MAX / SCREEN[0])
    assert click_down.flags == MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN
    assert click_up.flags == MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP
    assert click_down.dx == expected_dx and click_down.dy == round(
        540 * ABSOLUTE_MAX / SCREEN[1]
    )
    assert click_down.dx == click_up.dx and click_down.dy == click_up.dy
    assert move.flags == MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    assert (move.dx, move.dy) == (0, ABSOLUTE_MAX)
    assert click_down.is_release is False and click_up.is_release is True


def test_adapter_wheel_and_wait_intents() -> None:
    """滚轮：MOUSEEVENTF_WHEEL + 增量；wait 是调度语义、不产生真实事件。"""
    clock = FakeClock()
    sender = RecordingSender()
    adapter = SendInputAdapter(sender, clock=clock, screen_size=SCREEN)
    intents = [
        make_intent("s", "t", "wheel", {"delta": -120}, clock=clock),
        make_intent("s", "t", "wait", {"duration_ms": 10}, clock=clock),
    ]
    result = adapter.execute(make_batch("s", "t", intents, clock=clock))
    assert result.accepted_count == 2
    assert len(sender.units) == 1  # wait 不产生任何发送
    wheel = sender.units[0]
    assert wheel.type == "mouse" and wheel.flags == MOUSEEVENTF_WHEEL
    assert wheel.mouse_data == -120


def test_adapter_rejects_unknown_key_and_button() -> None:
    """未知按键/按钮给出机器可读错误码，且不产生任何发送。"""
    clock = FakeClock()
    sender = RecordingSender()
    adapter = SendInputAdapter(sender, clock=clock, screen_size=SCREEN)
    intents = [
        make_intent("s", "t", "key_down", {"key": "f13"}, clock=clock),
        make_intent("s", "t", "click", {"button": "side", "x": 1, "y": 1}, clock=clock),
    ]
    result = adapter.execute(make_batch("s", "t", intents, clock=clock))
    assert result.accepted_intents == []
    assert [r.reason for r in result.rejected] == ["unknown_key", "unknown_button"]
    assert sender.calls == 0 and adapter.sent_units == []


def test_adapter_negative_coordinates_rejected() -> None:
    """负坐标拒绝（invalid_coordinates），零发送。"""
    clock = FakeClock()
    sender = RecordingSender()
    adapter = SendInputAdapter(sender, clock=clock, screen_size=SCREEN)
    batch = make_batch(
        "s", "t",
        [make_intent("s", "t", "move", {"x": -5, "y": 0}, clock=clock)],
        clock=clock,
    )
    result = adapter.execute(batch)
    assert [r.reason for r in result.rejected] == ["invalid_coordinates"]
    assert sender.calls == 0


# ---------------------------------------------------------------- SAFE-001/003


def test_adapter_foreground_mismatch_zero_real_sends() -> None:
    """SAFE-001：整批前台复核不符 -> 零真实发送 + foreground_mismatch 原因。"""
    clock = FakeClock()
    sender = RecordingSender()
    verifier = FakeVerifier(ok=False, reasons=("pid_mismatch",))
    adapter = SendInputAdapter(sender, verifier, clock=clock, screen_size=SCREEN)
    batch = _key_batch(clock, [("key_down", "ctrl"), ("key_up", "ctrl")])
    result = adapter.execute(batch)
    assert result.accepted_intents == []
    assert all(r.reason == "foreground_mismatch:pid_mismatch" for r in result.rejected)
    assert sender.calls == 0 and len(sender.units) == 0
    assert verifier.calls == [batch.batch_id]  # 每批都复核


def test_adapter_ttl_expired_intents_skipped_not_sent() -> None:
    """SAFE-003：过期意图逐条跳过（expired_intent），新鲜意图照常执行。"""
    clock = FakeClock(start=0.0)
    sender = RecordingSender()
    adapter = SendInputAdapter(sender, clock=clock, screen_size=SCREEN)
    stale = make_intent("s", "t", "key_down", {"key": "a"}, clock=clock, ttl_ms=100.0)
    fresh = make_intent("s", "t", "key_up", {"key": "a"}, clock=clock, ttl_ms=5000.0)
    batch = make_batch("s", "t", [stale, fresh], clock=clock, ttl_ms=10_000.0)
    clock.advance(1.0)
    result = adapter.execute(batch)
    assert [i.intent_id for i in result.accepted_intents] == [fresh.intent_id]
    assert [r.reason for r in result.rejected] == ["expired_intent"]
    assert [u.scancode for u in sender.units] == [0x1E]  # 只有新鲜的 key_up 被发送
    assert sender.units[0].flags & KEYEVENTF_KEYUP


def test_adapter_count_mismatch_fails_whole_batch() -> None:
    """low_level 插入数量不符 -> 整批判失败（保守，绝不部分成功）。"""
    clock = FakeClock()
    sender = RecordingSender(inserted=0)
    adapter = SendInputAdapter(sender, clock=clock, screen_size=SCREEN)
    batch = _key_batch(clock, [("key_down", "a")])
    result = adapter.execute(batch)
    assert result.accepted_intents == []
    assert all(r.reason == "sendinput_failed:count_mismatch" for r in result.rejected)
    assert adapter.sent_units == []  # 未确认成功的事件不进入观测账本


def test_adapter_cancelled_check_blocks_sending() -> None:
    """取消屏障（INP-003 侧）：发送前取消检查为真 -> 零发送。"""
    clock = FakeClock()
    sender = RecordingSender()
    checks = iter([False, True])  # 入口检查通过，构建后复核发现已取消
    adapter = SendInputAdapter(
        sender, clock=clock, screen_size=SCREEN, cancelled=lambda: next(checks)
    )
    batch = _key_batch(clock, [("key_down", "a")])
    result = adapter.execute(batch)
    assert result.accepted_intents == []
    assert all(r.reason == "broker_cancelled" for r in result.rejected)
    assert sender.calls == 0


def test_adapter_compensation_batch_bypasses_cancel_and_verify() -> None:
    """补偿释放批次（cause=broker_*）跳过前台复核与取消检查：
    急停/失焦时 key_up 必须无条件发出。"""
    clock = FakeClock()
    sender = RecordingSender()
    verifier = FakeVerifier(ok=False, reasons=("no_foreground_window",))
    adapter = SendInputAdapter(
        sender, verifier, clock=clock, screen_size=SCREEN, cancelled=lambda: True
    )
    intents = [
        make_intent("s", "t", "key_up", {"key": "ctrl"}, clock=clock, ttl_ms=None,
                    cause="broker_cancel")
    ]
    batch = make_batch("s", "t", intents, clock=clock, ttl_ms=None, cause="broker_cancel")
    result = adapter.execute(batch)
    assert result.accepted_count == 1
    assert sender.calls == 1 and verifier.calls == []


def test_adapter_ctx_mode_defense_in_depth() -> None:
    """纵深防御：ctx.mode 非 real_input 时适配器自身也拒绝。"""
    clock = FakeClock()
    sender = RecordingSender()
    adapter = SendInputAdapter(sender, clock=clock, screen_size=SCREEN)
    batch = _key_batch(clock, [("key_down", "a")])
    result = adapter.execute(batch, SinkContext(mode="shadow"))
    assert all(r.reason == "mode_not_allowed" for r in result.rejected)
    assert sender.calls == 0


def test_send_units_real_guarded_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实发送函数在非 Windows 平台显式失败（不触碰系统）。"""
    import sys

    from input_broker import win32_adapter

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="sendinput_requires_windows"):
        send_units_real([SendInputUnit(type="key", flags=KEYEVENTF_SCANCODE, scancode=0x1E)])


# ---------------------------------------------------------------- INP-005 Broker


def test_broker_foreground_verifier_rejects_with_zero_input() -> None:
    """INP-005/SAFE-001（broker 层）：前台复核不符 -> 零输入。"""
    clock = FakeClock()
    sink = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sink, clock=clock, screen_size=SCREEN),
        clock=clock,
        foreground_verifier=FakeVerifier(ok=False, reasons=("hwnd_mismatch",)),
    )
    batch = _key_batch(clock, [("key_down", "a")])
    result = broker.submit(batch, _allow())
    assert result.accepted_intents == []
    assert all(r.reason == "foreground_mismatch:hwnd_mismatch" for r in result.rejected)
    assert sink.calls == 0
    assert broker.key_ledger.pressed == frozenset()


def test_broker_foreground_verifier_ok_executes_and_observes_ledger() -> None:
    """复核通过 -> 正常执行且按键账本记录（与 FakeSink 口径交叉验证）。"""
    clock = FakeClock()
    sink = RecordingSender()
    verifier = FakeVerifier(ok=True)
    broker = InputBroker(
        SendInputAdapter(sink, clock=clock, screen_size=SCREEN),
        clock=clock,
        foreground_verifier=verifier,
    )
    batch = _key_batch(clock, [("key_down", "ctrl")])
    assert broker.submit(batch, _allow()).accepted_count == 1
    assert broker.key_ledger.pressed == frozenset({"ctrl"})
    assert len(verifier.calls) == 1  # real_input 模式逐批二次核验


def test_broker_cancel_barrier_blocks_batch_approved_mid_flight() -> None:
    """INP-009：复核期间发生 cancel（竞态）-> 执行前被 epoch 屏障拦下。"""
    clock = FakeClock()
    sink = RecordingSender()
    broker = InputBroker(SendInputAdapter(sink, clock=clock, screen_size=SCREEN), clock=clock)

    class CancelDuringVerify:
        """恶意/竞态复核器：在复核调用中触发 cancel（模拟并发取消）。"""

        def verify_batch(self, batch: object) -> FakeVerdict:
            broker.cancel("race_during_verify")
            return FakeVerdict(True)

    broker.foreground_verifier = CancelDuringVerify()
    batch = _key_batch(clock, [("key_down", "a")])
    result = broker.submit(batch, _allow())
    assert result.accepted_intents == []
    assert all(r.reason == "broker_cancelled" for r in result.rejected)
    assert sink.calls == 0
    assert broker.epoch == 1  # 取消屏障计数已推进


def test_broker_cancelled_property_wires_into_adapter() -> None:
    """broker.cancelled 注入 adapter.cancelled：取消后适配器双层拒绝。"""
    clock = FakeClock()
    sink = RecordingSender()
    adapter = SendInputAdapter(sink, clock=clock, screen_size=SCREEN)
    broker = InputBroker(adapter, clock=clock)
    adapter.cancelled = lambda: broker.cancelled
    batch = _key_batch(clock, [("key_down", "a")])
    assert broker.submit(batch, _allow()).accepted_count == 1
    broker.cancel("operator")
    result = broker.submit(_key_batch(clock, [("key_down", "b")]), _allow())
    assert all(r.reason == "broker_cancelled" for r in result.rejected)
    # 直接调用适配器（绕过 broker 的入口检查）也被取消屏障拦截。
    direct = adapter.execute(_key_batch(clock, [("key_down", "c")]))
    assert all(r.reason == "broker_cancelled" for r in direct.rejected)
    # 真实发送只有两批：第一批 key_down + cancel 路径的补偿 key_up；
    # 取消后的新批次与直连适配器的批次都未产生发送。
    assert sink.calls == 2
    assert [u.flags & KEYEVENTF_KEYUP for u in sink.units[-1:]] == [KEYEVENTF_KEYUP]


# ---------------------------------------------------------------- SAFE-008/018


def test_estop_releases_all_keys_and_rejects_new_batches(tmp_path) -> None:
    """SAFE-008：急停 -> 已按下键全释放 + 新批次拒绝 + 审计入链。"""
    clock = FakeClock(start=7.0)
    sender = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sender, clock=clock, screen_size=SCREEN), clock=clock
    )
    trace_path = tmp_path / "trace.jsonl"
    audit = AuditLogger(JsonlTraceWriter(trace_path), clock=clock)
    estop = EstopController(broker, audit, clock=clock)

    assert estop.triggered is False
    batch = _key_batch(clock, [("key_down", "ctrl"), ("key_down", "shift")])
    assert broker.submit(batch, _allow()).accepted_count == 2
    assert broker.key_ledger.pressed == frozenset({"ctrl", "shift"})

    assert estop.trigger("hotkey", "operator pressed Ctrl+Alt+F12") is True
    assert estop.triggered is True
    # 无卡键：账本清空，且补偿 key_up 经真实适配器发出（2 条 up 载荷）。
    assert broker.key_ledger.pressed == frozenset()
    ups = [u for u in sender.units if u.flags & KEYEVENTF_KEYUP]
    assert len(ups) == 2
    # 新批次一律拒绝。
    result = broker.submit(_key_batch(clock, [("key_down", "a")]), _allow())
    assert all(r.reason == "broker_stopped" for r in result.rejected)
    # 审计：estop + keys_released（无卡键证据）。
    events = JsonlTraceReader(trace_path).read()
    types = [e.type for e in events]
    assert "estop" in types
    estop_event = next(e for e in events if e.type == "estop")
    assert estop_event.payload["source"] == "hotkey"
    released = [e for e in events if e.type == "anomaly"
                and e.payload.get("kind") == "keys_released"]
    assert len(released) == 1
    assert released[0].payload["keys"] == ["ctrl", "shift"]


def test_estop_is_idempotent_safe_018(tmp_path) -> None:
    """SAFE-018：急停幂等——重复触发只执行一次、审计只有一条 estop。"""
    clock = FakeClock()
    sender = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sender, clock=clock, screen_size=SCREEN), clock=clock
    )
    trace_path = tmp_path / "trace.jsonl"
    audit = AuditLogger(JsonlTraceWriter(trace_path), clock=clock)
    estop = EstopController(broker, audit, clock=clock)

    broker.submit(_key_batch(clock, [("key_down", "a")]), _allow())
    assert estop.trigger("hotkey", "first") is True
    assert estop.trigger("watchdog", "second") is False
    assert estop.trigger("ui", "third") is False
    assert len(estop.records) == 1
    estop_events = [
        e for e in JsonlTraceReader(trace_path).read() if e.type == "estop"
    ]
    assert len(estop_events) == 1
    assert estop_events[0].payload["source"] == "hotkey"


def test_estop_records_source_reason_and_monotonic_time() -> None:
    """触发记录包含来源、原因与单调时间戳（内存视图 + 审计一致）。"""
    clock = FakeClock(start=42.5)
    estop = EstopController(clock=clock)  # 仅控制器（无 broker/audit 也能记录）
    assert estop.trigger("focus_lost", "foreground changed") is True
    record = estop.records[0]
    assert record.source == "focus_lost"
    assert record.reason == "foreground changed"
    assert record.ts_monotonic == pytest.approx(42.5)
    assert record.released_keys == ()


def test_estop_reset_requires_manual_recovery_flow() -> None:
    """reset 仅人工恢复流程使用：复位后可再次触发（新会话重新绑定）。"""
    estop = EstopController(clock=FakeClock())
    assert estop.trigger("ui", "manual") is True
    assert estop.trigger("ui", "again") is False
    estop.reset()
    assert estop.trigger("ui", "after manual recovery") is True
    assert len(estop.records) == 2


# ---------------------------------------------------------------- SAFE-010 / INP-007


def test_watchdog_parent_death_triggers_estop_and_key_release(tmp_path) -> None:
    """SAFE-010：父进程死亡 -> on_dead -> 急停释放全部键 + 新批次拒绝。"""
    clock = FakeClock()
    sender = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sender, clock=clock, screen_size=SCREEN), clock=clock
    )
    trace_path = tmp_path / "trace.jsonl"
    estop = EstopController(broker, AuditLogger(JsonlTraceWriter(trace_path), clock=clock), clock=clock)
    watchdog = ParentWatchdog(
        parent_pid=424242, interval=0.01, on_dead=lambda: estop.trigger("watchdog", "parent gone"),
        probe=lambda pid: False,  # 注入假探活：父进程已死
    )
    broker.submit(_key_batch(clock, [("key_down", "ctrl")]), _allow())
    assert broker.key_ledger.pressed == frozenset({"ctrl"})

    assert watchdog.poll_once() is True
    assert watchdog.dead_fired is True
    assert estop.triggered is True
    assert broker.key_ledger.pressed == frozenset()  # 全部释放
    assert any(u.flags & KEYEVENTF_KEYUP for u in sender.units)
    assert watchdog.poll_once() is False  # on_dead 至多一次
    assert len([r for r in estop.records]) == 1


def test_watchdog_heartbeat_tolerance_before_trigger() -> None:
    """心跳容忍：max_misses=3 时，两次失败不触发，第三次触发。"""
    fired: list[int] = []
    watchdog = ParentWatchdog(
        1, 0.01, lambda: fired.append(1), probe=lambda pid: False, max_misses=3
    )
    assert watchdog.poll_once() is False and watchdog.misses == 1
    assert watchdog.poll_once() is False and watchdog.misses == 2
    assert watchdog.poll_once() is True and watchdog.misses == 3
    assert len(fired) == 1
    # 恢复存活会清零计数（未触发前）。
    watchdog2 = ParentWatchdog(1, 0.01, lambda: None, probe=lambda pid: True, max_misses=3)
    watchdog2.poll_once()
    assert watchdog2.misses == 0


def test_watchdog_probe_exception_counts_as_miss() -> None:
    """默认拒绝：探针异常按"不存活"计入心跳失败。"""
    def broken(pid: int) -> bool:
        raise RuntimeError("probe failed")

    watchdog = ParentWatchdog(1, 0.01, lambda: None, probe=broken, max_misses=1)
    assert watchdog.poll_once() is True


def test_watchdog_thread_mode_stops_after_death() -> None:
    """线程模式：探活失败自动触发并退出线程；stop 幂等。"""
    fired = threading.Event()
    watchdog = ParentWatchdog(
        99, 0.01, fired.set, probe=lambda pid: False, sleep=lambda s: None
    )
    watchdog.start()
    assert fired.wait(timeout=2.0) is True
    deadline = time.monotonic() + 2.0
    while watchdog.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert watchdog.running is False
    watchdog.stop()


def test_watchdog_alive_parent_never_fires_then_stop() -> None:
    """父进程存活 -> 不触发；stop 后线程退出且不触发 on_dead。"""
    fired: list[int] = []
    watchdog = ParentWatchdog(
        1, 0.01, lambda: fired.append(1), probe=lambda pid: True, sleep=lambda s: None
    )
    watchdog.start()
    watchdog.stop()
    assert fired == [] and watchdog.dead_fired is False


def test_watchdog_rejects_invalid_config() -> None:
    """interval/max_misses 配置校验。"""
    with pytest.raises(ValueError):
        ParentWatchdog(1, 0, lambda: None)
    with pytest.raises(ValueError):
        ParentWatchdog(1, 0.1, lambda: None, max_misses=0)


# ---------------------------------------------------------------- INP-006 热键


def test_hotkey_trigger_stops_broker_via_estop() -> None:
    """假热键触发 -> 急停 -> broker 停止、按键释放（UI 无关）。"""
    clock = FakeClock()
    sender = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sender, clock=clock, screen_size=SCREEN), clock=clock
    )
    estop = EstopController(broker, clock=clock)
    hotkey = EstopHotkey(
        lambda source: estop.trigger(source, "hotkey"), registrar=FakeRegistrar()
    )
    hotkey.start()
    registrar = hotkey._registrar
    assert isinstance(registrar, FakeRegistrar)
    deadline = time.monotonic() + 2.0
    while not registrar.registered and time.monotonic() < deadline:
        time.sleep(0.01)
    assert registrar.registered is True
    broker.submit(_key_batch(clock, [("key_down", "a")]), _allow())
    registrar.fire()  # 模拟用户按下 Ctrl+Alt+F12
    hotkey.stop()
    assert estop.triggered is True
    assert broker.cancelled is True
    assert registrar.unregistered is True
    assert estop.records[0].source == "hotkey"


def test_hotkey_registration_failure_reports_clear_error() -> None:
    """注册失败（热键被占用）-> 明确错误码 + on_error 回调，绝不静默。"""
    errors: list[str] = []
    hotkey = EstopHotkey(
        lambda source: None,
        registrar=FakeRegistrar(register_ok=False),
        on_error=errors.append,
    )
    hotkey.start()
    assert hotkey._thread is not None
    hotkey._thread.join(timeout=2.0)
    assert hotkey.failed is True
    assert hotkey.error == ERROR_REGISTER_FAILED
    assert errors == [ERROR_REGISTER_FAILED]
    assert hotkey.running is False


def test_hotkey_stop_before_start_is_safe() -> None:
    """未 start 直接 stop / 重复 stop 均安全（幂等）。"""
    registrar = FakeRegistrar()
    hotkey = EstopHotkey(lambda source: None, registrar=registrar)
    hotkey.stop()
    hotkey.stop()
    assert hotkey.running is False and registrar.unregistered is False


# ---------------------------------------------------------------- SAFE-019 / INP-010


def test_second_real_session_rejected_with_real_adapter() -> None:
    """SAFE-019（真实适配器集成）：real_input 会话独占。"""
    clock = FakeClock()
    sender = RecordingSender()
    broker = InputBroker(
        SendInputAdapter(sender, clock=clock, screen_size=SCREEN), clock=clock
    )
    assert broker.claim_real_session("sess-a") is True
    assert broker.claim_real_session("sess-b") is False
    batch_b = make_batch(
        "sess-b", "t",
        [make_intent("sess-b", "t", "key_down", {"key": "a"}, clock=clock)],
        clock=clock,
    )
    result = broker.submit(batch_b, _allow())
    assert all(r.reason == "session_mismatch" for r in result.rejected)
    assert sender.calls == 0


def test_audit_logs_policy_denied_and_budget_and_focus_events(tmp_path) -> None:
    """INP-010：策略拒绝/预算耗尽/失焦事件全部入链且类型正确。"""
    clock = FakeClock()
    trace_path = tmp_path / "trace.jsonl"
    audit = AuditLogger(JsonlTraceWriter(trace_path), clock=clock)
    audit.log_policy_denied("batch-1", "sess-1", ["mode_not_allowed"], target_id="t")
    audit.log_budget_exhausted(session_id="sess-1", detail={"actions": 120})
    audit.log_focus_lost(
        session_id="sess-1",
        expected={"pid": 10, "hwnd": 100},
        actual={"pid": 99, "hwnd": 999},
    )
    events = JsonlTraceReader(trace_path).read()
    assert [e.type for e in events] == ["policy_decision", "anomaly", "anomaly"]
    assert events[0].payload["outcome"] == "denied"
    assert events[0].payload["reasons"] == ["mode_not_allowed"]
    kinds = [e.payload.get("kind") for e in events[1:]]
    assert kinds == ["budget_exhausted", "focus_lost"]


def test_audit_release_and_estop_chain_is_verifiable(tmp_path) -> None:
    """急停与释放事件写入同一哈希链，读取端可完整校验（不可篡改证据）。"""
    clock = FakeClock()
    ledger = KeyLedger()
    ledger.observe(make_intent("s", "t", "key_down", {"key": "shift"}, clock=clock))
    released = ledger.release_all(1.0, cause="estop:hotkey")
    trace_path = tmp_path / "trace.jsonl"
    audit = AuditLogger(JsonlTraceWriter(trace_path), clock=clock)
    audit.log_estop("hotkey", "user", released_count=1, session_id="s")
    audit.log_release(released, cause="estop:hotkey", session_id="s")
    reader = JsonlTraceReader(trace_path)
    events = reader.read()
    assert reader.truncated_tail is False  # 哈希链完整
    assert [e.type for e in events] == ["estop", "anomaly"]
    assert events[0].payload["released_count"] == 1
    assert events[1].payload["keys"] == ["shift"]
    assert events[1].payload["cause"] == "estop:hotkey"


# ---------------------------------------------------------------- 静态守卫


def test_input_broker_ctypes_confinement_static_guard() -> None:
    """静态守卫镜像：input_broker 内 ctypes 只允许三个 win32 文件；
    pyautogui/pydirectinput/pynput/keyboard/mouse/win32* 全仓禁止。"""
    pkg_dir = Path(input_broker.__file__).parent
    ctypes_allowed = {"win32_adapter.py", "win32_hotkey.py", "win32_watchdog.py"}
    forbidden_everywhere = {
        "pyautogui", "pydirectinput", "pynput", "keyboard", "mouse",
        "win32gui", "win32con", "win32api", "win32process", "pywintypes",
    }
    for path in sorted(pkg_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                assert name not in forbidden_everywhere, (
                    f"{path.name}: input_broker 禁止 import {name}"
                )
                if name == "ctypes":
                    assert path.name in ctypes_allowed, (
                        f"{path.name}: ctypes 只允许出现在 {sorted(ctypes_allowed)}"
                    )
