"""INP-001/002/INP-004 雏形单测：意图/批次过期语义、按键账本、FakeInputSink 与 InputBroker。

对应 SAFE 用例的单测版：过期意图（SAFE-003）、cancel/stop 零输入、
并发 real_input 会话（SAFE-019 的 broker 层模拟）。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from types import SimpleNamespace

import pytest
from common import FakeClock

from input_broker import (
    FakeInputSink,
    InputBroker,
    KeyLedger,
    RealInputSink,
    SinkContext,
    make_batch,
    make_intent,
)

# ---------------------------------------------------------------- INP-001


def test_make_intent_generates_ids_timestamps_and_expiry() -> None:
    """工厂自动生成 id 与时间戳；TTL 到点即过期。"""
    clock = FakeClock(start=10.0)
    intent = make_intent(
        "sess-1", "tgt-1", "click", {"button": "left", "x": 1, "y": 2},
        clock=clock, ttl_ms=500.0, cause="test",
    )
    assert intent.intent_id.startswith("intent-")
    assert intent.session_id == "sess-1" and intent.target_id == "tgt-1"
    assert intent.created_monotonic == pytest.approx(10.0)
    assert intent.expires_monotonic == pytest.approx(10.5)
    assert not intent.expired(10.499)
    assert intent.expired(10.5)  # 达到过期时刻即过期（过期即拒绝、不补发）


def test_make_intent_requires_exactly_one_time_source() -> None:
    """clock / now 必须且只能提供一个（保证时间确定性）。"""
    with pytest.raises(ValueError):
        make_intent("s", "t", "click", {})
    clock = FakeClock()
    with pytest.raises(ValueError):
        make_intent("s", "t", "click", {}, clock=clock, now=1.0)


def test_make_intent_validates_kind_and_key_payload() -> None:
    """未知 kind 拒绝；key 类意图强制要求 payload['key']。"""
    with pytest.raises(ValueError):
        make_intent("s", "t", "teleport", {}, clock=FakeClock())
    with pytest.raises(ValueError):
        make_intent("s", "t", "key_down", {}, clock=FakeClock())
    for kind, payload in [
        ("key_down", {"key": "Ctrl"}),
        ("key_up", {"key": "ctrl"}),
        ("click", {"button": "left", "x": 0, "y": 0}),
        ("move", {"x": 1, "y": 1}),
        ("wheel", {"delta": -120}),
        ("wait", {"duration_ms": 10}),
    ]:
        intent = make_intent("s", "t", kind, payload, clock=FakeClock())
        assert intent.kind == kind


def test_batch_expiry_semantics() -> None:
    """整批统一过期判断；action_intents 不含 wait。"""
    clock = FakeClock(start=0.0)
    intents = [
        make_intent("s", "t", "click", {"button": "left", "x": 0, "y": 0},
                    clock=clock, ttl_ms=10_000.0),
        make_intent("s", "t", "wait", {"duration_ms": 5}, clock=clock, ttl_ms=10_000.0),
    ]
    batch = make_batch("s", "t", intents, clock=clock, ttl_ms=1000.0, cause="c")
    assert batch.batch_id.startswith("batch-")
    assert not batch.expired(0.999)
    assert batch.expired(1.0)
    assert len(batch.action_intents) == 1  # wait 不算动作


# ---------------------------------------------------------------- INP-004


def test_key_ledger_pairs_down_and_up() -> None:
    """key_down/key_up 配对；大小写归一；重复 key_up 无副作用。"""
    ledger = KeyLedger()
    down = make_intent("s", "t", "key_down", {"key": "Ctrl"}, clock=FakeClock())
    assert ledger.observe(down) is True
    assert ledger.is_pressed("ctrl") and ledger.pressed == frozenset({"ctrl"})
    up = make_intent("s", "t", "key_up", {"key": "ctrl"}, clock=FakeClock())
    assert ledger.observe(up) is True
    assert not ledger.is_pressed("ctrl") and ledger.pressed == frozenset()
    assert ledger.observe(up) is False  # 重复释放无额外效果
    click = make_intent("s", "t", "click", {"button": "left", "x": 0, "y": 0},
                        clock=FakeClock())
    assert ledger.observe(click) is False  # 非 key 意图被忽略


def test_key_ledger_release_all_is_idempotent() -> None:
    """release_all 生成必要 key_up 补偿（永不过期）；重复调用无额外效果。"""
    ledger = KeyLedger()
    for key in ("b", "a"):
        ledger.observe(
            make_intent("s", "t", "key_down", {"key": key}, clock=FakeClock())
        )
    ups = ledger.release_all(99.0, session_id="s", target_id="t", cause="stop")
    assert [u.payload["key"] for u in ups] == ["a", "b"]  # 确定性顺序
    assert all(u.kind == "key_up" and u.cause == "stop" for u in ups)
    assert all(u.expires_monotonic == float("inf") for u in ups)  # 补偿永不因 TTL 丢失
    assert all(not u.expired(10**9) for u in ups)
    assert ledger.pressed == frozenset()
    assert ledger.release_all(100.0) == []  # 幂等：第二次调用无额外效果


# ---------------------------------------------------------------- INP-002


def test_fake_sink_preserves_order_and_payload() -> None:
    """账本按执行顺序原样记录意图；结果区分接受/拒绝。"""
    clock = FakeClock(start=1.0)
    sink = FakeInputSink(clock=clock)
    intents = [
        make_intent("s", "t", "key_down", {"key": "a"}, clock=clock),
        make_intent("s", "t", "click", {"button": "left", "x": 3, "y": 4}, clock=clock),
        make_intent("s", "t", "wait", {"duration_ms": 10}, clock=clock),
    ]
    batch = make_batch("s", "t", intents, clock=clock)
    result = sink.execute(batch, SinkContext(correlation_id="cid-1"))
    assert [r.intent.intent_id for r in sink.records] == [i.intent_id for i in intents]
    assert result.batch_id == batch.batch_id
    assert result.accepted_intents == intents
    assert result.rejected == []
    assert sink.real_executed_count == 3
    assert sink.records[0].correlation_id == "cid-1"


def test_fake_sink_injected_fail_points() -> None:
    """失败点注入：指定全局序号的动作被拒绝，其余照常。"""
    sink = FakeInputSink(clock=FakeClock(), fail_points={1: "boom"})
    intents = [
        make_intent("s", "t", "click", {"button": "left", "x": 0, "y": 0},
                    clock=FakeClock())
        for _ in range(3)
    ]
    batch = make_batch("s", "t", intents, clock=FakeClock())
    result = sink.execute(batch)
    assert len(result.accepted_intents) == 2
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "boom"
    assert result.rejected[0].intent.intent_id == intents[1].intent_id
    assert sink.records[1].accepted is False


def test_fake_sink_failure_rate_is_deterministic() -> None:
    """失败率注入：同种子序列完全一致；0/1 边界行为正确。"""
    intents = [
        make_intent("s", "t", "click", {"button": "left", "x": 0, "y": 0},
                    clock=FakeClock())
        for _ in range(20)
    ]
    batch = make_batch("s", "t", intents, clock=FakeClock())
    a = FakeInputSink(clock=FakeClock(), failure_rate=0.5, rng_seed=7)
    b = FakeInputSink(clock=FakeClock(), failure_rate=0.5, rng_seed=7)
    pa = [r.intent.intent_id for r in a.execute(batch).rejected]
    pb = [r.intent.intent_id for r in b.execute(batch).rejected]
    assert pa == pb  # 同种子 -> 确定性
    clean = FakeInputSink(clock=FakeClock(), failure_rate=0.0).execute(batch)
    assert clean.rejected == []
    all_fail = FakeInputSink(clock=FakeClock(), failure_rate=1.0).execute(batch)
    assert len(all_fail.rejected) == len(intents)


def test_fake_sink_pressed_tracks_accepted_keys() -> None:
    """sink 内部 pressed 只随被接受的 key 意图变化，且与 KeyLedger 口径一致。"""
    clock = FakeClock()
    sink = FakeInputSink(clock=clock, fail_points={1: "fail-down"})
    ledger = KeyLedger()
    intents = [
        make_intent("s", "t", "key_down", {"key": "a"}, clock=clock),
        make_intent("s", "t", "key_down", {"key": "b"}, clock=clock),  # 将被注入失败
        make_intent("s", "t", "key_up", {"key": "A"}, clock=clock),
    ]
    batch = make_batch("s", "t", intents, clock=clock)
    result = sink.execute(batch)
    assert sink.pressed == frozenset()  # a 已被 key_up 释放；b 的 down 被注入失败未按下
    assert [r.intent.kind for r in sink.records if not r.accepted] == ["key_down"]
    for intent in result.accepted_intents:
        ledger.observe(intent)
    assert ledger.pressed == sink.pressed  # 账本一致（无真实 OS 调用的证据）


def test_input_broker_package_has_no_real_system_input_paths() -> None:
    """AST 级安全契约：输入包不 import 任何系统输入设施，无 exec/eval。"""
    import input_broker.broker
    import input_broker.fake_sink
    import input_broker.intents
    import input_broker.key_ledger

    forbidden_modules = {
        "ctypes", "pyautogui", "pydirectinput", "pynput", "keyboard", "mouse",
        "win32", "win32api", "win32con", "win32gui", "win32process", "user32",
        "kernel32", "sendkeys", "subprocess",
    }
    forbidden_calls = {"exec", "eval", "compile", "__import__"}
    for module in (
        input_broker.intents, input_broker.key_ledger,
        input_broker.fake_sink, input_broker.broker,
    ):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_modules, (
                        f"{module.__name__} 引入了被禁止的模块 {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_modules, (
                    f"{module.__name__} 引入了被禁止的模块 {node.module}"
                )
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, (
                    f"{module.__name__} 调用了被禁止的内置 {node.func.id}"
                )


def test_real_input_sink_is_placeholder_only() -> None:
    """RealInputSink 占位类：任何执行请求显式失败（M1 由 Win32 适配器实现）。"""
    clock = FakeClock()
    batch = make_batch(
        "s", "t",
        [make_intent("s", "t", "click", {"button": "left", "x": 0, "y": 0}, clock=clock)],
        clock=clock,
    )
    with pytest.raises(NotImplementedError):
        RealInputSink().execute(batch)


# ---------------------------------------------------------------- Broker


def _allow(mode: str = "real_input") -> SimpleNamespace:
    return SimpleNamespace(allow=True, reasons=[], mode=mode)


def test_broker_denied_decision_means_zero_input() -> None:
    """decision.deny -> 零输入，逐意图返回策略原因。"""
    clock = FakeClock()
    sink = FakeInputSink(clock=clock)
    broker = InputBroker(sink, clock=clock)
    batch = make_batch(
        "s", "t",
        [make_intent("s", "t", "click", {"button": "left", "x": 0, "y": 0}, clock=clock)],
        clock=clock,
    )
    decision = SimpleNamespace(allow=False, reasons=["mode_not_allowed"])
    result = broker.submit(batch, decision)
    assert result.accepted_intents == []
    assert [r.reason for r in result.rejected] == ["mode_not_allowed"]
    assert sink.real_executed_count == 0
    # 纵深防御：allow 但 Broker 模式非 real_input 时依然拒绝。
    result2 = broker.submit(batch, SimpleNamespace(allow=True, reasons=[], mode=None))
    assert result2.accepted_intents == []
    assert all(r.reason == "mode_not_allowed" for r in result2.rejected)


def test_broker_filters_expired_intents() -> None:
    """过期意图在执行前被过滤（expired_intent），新鲜意图照常执行。"""
    clock = FakeClock(start=0.0)
    sink = FakeInputSink(clock=clock)
    broker = InputBroker(sink, clock=clock)
    stale = make_intent("s", "t", "click", {"button": "left", "x": 0, "y": 0},
                        clock=clock, ttl_ms=100.0)
    fresh = make_intent("s", "t", "click", {"button": "left", "x": 1, "y": 1},
                        clock=clock, ttl_ms=5000.0)
    batch = make_batch("s", "t", [stale, fresh], clock=clock, ttl_ms=10_000.0)
    clock.advance(1.0)
    result = broker.submit(batch, _allow())
    assert [i.intent_id for i in result.accepted_intents] == [fresh.intent_id]
    assert [r.reason for r in result.rejected] == ["expired_intent"]
    assert sink.real_executed_count == 1


def test_broker_cancel_rejects_new_batches_and_stop_releases_idempotent() -> None:
    """cancel 后新批次拒绝；stop 幂等并 release_all（INP-004）。"""
    clock = FakeClock()
    sink = FakeInputSink(clock=clock)
    broker = InputBroker(sink, clock=clock)
    down_batch = make_batch(
        "s", "t",
        [make_intent("s", "t", "key_down", {"key": "ctrl"}, clock=clock)],
        clock=clock,
    )
    assert broker.submit(down_batch, _allow()).accepted_count == 1
    assert broker.key_ledger.pressed == frozenset({"ctrl"})

    released_on_cancel = broker.cancel("operator")
    assert [u.payload["key"] for u in released_on_cancel] == ["ctrl"]
    assert broker.key_ledger.pressed == frozenset()

    new_batch = make_batch(
        "s", "t",
        [make_intent("s", "t", "click", {"button": "left", "x": 0, "y": 0}, clock=clock)],
        clock=clock,
    )
    result = broker.submit(new_batch, _allow())
    assert result.accepted_intents == []
    assert all(r.reason == "broker_cancelled" for r in result.rejected)

    assert broker.stop() == []  # cancel 已释放，stop 无额外补偿（幂等）
    assert broker.stop() == []  # 重复 stop 无额外效果
    result2 = broker.submit(new_batch, _allow())
    assert all(r.reason == "broker_stopped" for r in result2.rejected)
    # 真实执行记录 = key_down + cancel 路径的补偿 key_up（均经 sink 顺序入账）。
    assert sink.real_executed_count == 2
    assert [r.intent.kind for r in sink.records if r.accepted] == ["key_down", "key_up"]


def test_broker_second_real_session_is_rejected() -> None:
    """SAFE-019（broker 层模拟）：real_input 会话独占，第二个会话拒绝。"""
    clock = FakeClock()
    sink = FakeInputSink(clock=clock)
    broker = InputBroker(sink, clock=clock)
    assert broker.claim_real_session("sess-a") is True
    assert broker.claim_real_session("sess-b") is False  # 并发第二个抢占失败
    batch_b = make_batch(
        "sess-b", "t",
        [make_intent("sess-b", "t", "click", {"button": "left", "x": 0, "y": 0},
                     clock=clock)],
        clock=clock,
    )
    result = broker.submit(batch_b, _allow())
    assert result.accepted_intents == []
    assert all(r.reason == "session_mismatch" for r in result.rejected)
    assert sink.real_executed_count == 0
    # 会话 a 不受影响（过期意图丢弃不影响后续合法会话的对称约定）。
    batch_a = make_batch(
        "sess-a", "t",
        [make_intent("sess-a", "t", "click", {"button": "left", "x": 1, "y": 1},
                     clock=clock)],
        clock=clock,
    )
    assert broker.submit(batch_a, _allow()).accepted_count == 1


def test_input_batch_replace_keeps_identity() -> None:
    """Broker 过期过滤用 dataclasses.replace 生成子批次：批次身份字段保留。"""
    clock = FakeClock()
    intents = [
        make_intent("s", "t", "click", {"button": "left", "x": 0, "y": 0}, clock=clock)
    ]
    batch = make_batch("s", "t", intents, clock=clock, cause="c")
    sub = dataclasses.replace(batch, intents=intents)
    assert sub.batch_id == batch.batch_id and sub.cause == batch.cause


def test_test_kit_input_fakes_convenience() -> None:
    """TST-001（M0）：test_kit 的 fake 便捷构造可用且与包行为一致。"""
    from test_kit import FakeClock, make_click_batch, make_fake_sink

    clock = FakeClock(start=2.0)
    sink = make_fake_sink(clock=clock)
    batch = make_click_batch("s", "t", 3, clock=clock, x=5, y=6)
    assert len(batch.intents) == 3
    result = sink.execute(batch)
    assert result.accepted_count == 3
    assert sink.real_executed_count == 3
