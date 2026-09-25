"""公共包冒烟测试：验证 harness 与 common 基础设施可用。"""

from __future__ import annotations

import logging

from common import (
    FakeClock,
    MonotonicClock,
    correlation_scope,
    get_correlation_id,
    get_logger,
    log_event,
    new_correlation_id,
    new_id,
    new_session_id,
)


def test_ids_unique_and_prefixed() -> None:
    a, b = new_id("x"), new_id("x")
    assert a != b and a.startswith("x-")
    assert new_session_id().startswith("sess-")
    assert new_correlation_id().startswith("cid-")


def test_fake_clock_advances_without_waiting() -> None:
    clock = FakeClock()
    base = clock.now()
    clock.advance(2.5)
    clock.advance(1.0)
    assert clock.now() == base + 3.5
    assert clock.advances == [2.5, 1.0]


def test_monotonic_clock_is_monotonic() -> None:
    clock = MonotonicClock()
    t1, t2 = clock.now(), clock.now()
    assert t2 >= t1


def test_correlation_scope_binds_and_restores() -> None:
    outer = new_correlation_id()
    with correlation_scope(outer):
        assert get_correlation_id() == outer
        with correlation_scope():
            inner = get_correlation_id()
            assert inner is not None and inner != outer
        assert get_correlation_id() == outer


def test_log_event_emits_json_with_fields(capsys: object) -> None:
    logger = get_logger("test.common")
    with correlation_scope("cid-test"):
        log_event(logger, "policy_denied", reason="foreground_mismatch")
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert '"event": "policy_denied"' in out
    assert '"cid": "cid-test"' in out
    assert '"reason": "foreground_mismatch"' in out


def test_get_logger_is_idempotent() -> None:
    a = get_logger("test.common.idem")
    b = get_logger("test.common.idem")
    assert a is b
    assert sum(isinstance(h.formatter, logging.Formatter) for h in a.handlers) >= 1
