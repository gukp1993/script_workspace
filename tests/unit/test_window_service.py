"""窗口服务单测（E04：TGT-001/002/003/004/009 + INP-005）。

覆盖：
- list_windows 真实环境冒烟（字段完整性）；
- matcher 正则/exe/类名命中与歧义信号（CAP-012：多命中不自动挑选）；
- ForegroundMonitor：注入假窗口源测试变化回调、异常安全停止、线程启停；
- ForegroundVerifier / SessionLock：错窗、PID 不符、进程消失即失效并闩存；
- diagnostics.check_access：注入探针的权限/UIPI 诊断（不报告假成功）；
- 静态守卫：window_service 内 win32 import 只允许在 enumerate_win.py。
"""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from common import FakeClock
from domain_model.models import TargetProfile

import window_service
from window_service import (
    BatchForegroundVerifier,
    ForegroundMonitor,
    ForegroundVerifier,
    SessionBinding,
    SessionLock,
    VerifyResult,
    WindowInfo,
    check_access,
    list_windows,
    make_foreground_source,
    match_targets,
    match_targets_detailed,
)


def _profile(**overrides: object) -> TargetProfile:
    """构造合法 TargetProfile（字段与 domain_model 校验对齐）。"""
    fields: dict[str, object] = {
        "target_id": "arena-lab",
        "executable": "ArenaLab.exe",
        "title_regex": "Arena.*Lab",
        "allowed_display_modes": ["windowed"],
    }
    fields.update(overrides)
    return TargetProfile(**fields)  # type: ignore[arg-type]


def _window(hwnd: int = 100, *, pid: int = 10, exe: str = "ArenaLab.exe",
            title: str = "ArenaLab", cls: str = "ArenaLabClass",
            visible: bool = True, minimized: bool = False, monitor: int = 0) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd, pid=pid, exe_name=exe, title=title, class_name=cls,
        visible=visible, minimized=minimized, monitor_index=monitor,
    )


def _binding(**overrides: object) -> SessionBinding:
    fields: dict[str, object] = {
        "session_id": "sess-1", "target_id": "arena-lab",
        "pid": 10, "hwnd": 100, "instance_token": "tok-1",
        "exe_name": "ArenaLab.exe",
    }
    fields.update(overrides)
    return SessionBinding(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------- TGT-001


def test_window_service_available_and_real_enumeration() -> None:
    """真实环境：服务可用且能枚举到 >=1 个窗口，字段完整且类型正确。"""
    from window_service import WINDOW_SERVICE_AVAILABLE

    assert WINDOW_SERVICE_AVAILABLE is True
    windows = list_windows()
    assert len(windows) >= 1
    for w in windows:
        assert isinstance(w.hwnd, int) and w.hwnd != 0
        assert isinstance(w.pid, int) and w.pid > 0
        assert isinstance(w.exe_name, str)  # 取不到时允许为空串
        assert isinstance(w.title, str)
        assert isinstance(w.class_name, str)
        assert isinstance(w.visible, bool)
        assert isinstance(w.minimized, bool)
        assert isinstance(w.monitor_index, int)


def test_real_environment_has_visible_window_and_describe() -> None:
    """真实桌面必有可见窗口；describe() 输出包含关键标识。"""
    visible = [w for w in list_windows() if w.visible]
    assert len(visible) >= 1
    text = visible[0].describe()
    for token in ("hwnd=", "pid=", "title="):
        assert token in text


# ---------------------------------------------------------------- TGT-002


def test_matcher_hits_by_title_regex_and_exe() -> None:
    """标题正则 + exe 名（大小写不敏感）共同命中。"""
    windows = [
        _window(1, exe="ArenaLab.EXE", title="ArenaLab - 主界面"),
        _window(2, exe="other.exe", title="ArenaLab - 假窗口"),
        _window(3, exe="ArenaLab.exe", title="无关标题"),
    ]
    result = match_targets_detailed(_profile(), windows)
    assert [w.hwnd for w in result.matches] == [1]
    assert result.ambiguous is False
    assert result.unambiguous_match is not None
    assert result.unambiguous_match.hwnd == 1


def test_matcher_window_class_is_optional_filter() -> None:
    """指定窗口类时必须精确一致；不指定时不约束。"""
    windows = [_window(1, cls="ArenaLabClass"), _window(2, cls="OtherClass")]
    with_class = match_targets(_profile(window_class="ArenaLabClass"), windows)
    assert [w.hwnd for w in with_class] == [1]
    without_class = match_targets(_profile(window_class=None), windows)
    assert [w.hwnd for w in without_class] == [1, 2]


def test_matcher_returns_all_matches_without_auto_pick() -> None:
    """CAP-012：同名多窗口全部返回，绝不自动挑选单个实例。"""
    windows = [
        _window(1, title="ArenaLab - 实例1"),
        _window(2, title="ArenaLab - 实例2"),
        _window(3, title="ArenaLab - 实例3"),
    ]
    matched = match_targets(_profile(), windows)
    assert sorted(w.hwnd for w in matched) == [1, 2, 3]


def test_matcher_detailed_ambiguous_signal_blocks_auto_pick() -> None:
    """多命中 -> ambiguous=True 且 unambiguous_match 为 None（要求人工确认）。"""
    windows = [_window(1), _window(2, pid=11)]
    result = match_targets_detailed(_profile(), windows)
    assert result.ambiguous is True
    assert len(result.matches) == 2
    assert result.unambiguous_match is None


def test_matcher_no_match_returns_empty() -> None:
    """无命中返回空列表（不抛异常、不猜）。"""
    assert match_targets(_profile(), [_window(1, title="别的程序")]) == []
    assert match_targets_detailed(_profile(), []).matches == []


# ---------------------------------------------------------------- TGT-003


def test_monitor_reports_change_via_callback() -> None:
    """注入假窗口源：前台变化时 on_change(old, new) 按序触发。"""
    a = _window(1)
    b = _window(2)
    seq = iter([a, b, b])
    changes: list[tuple[WindowInfo | None, WindowInfo | None]] = []
    monitor = ForegroundMonitor(
        lambda: next(seq), interval=0.01, on_change=lambda old, new: changes.append((old, new)),
        clock=FakeClock(),
    )
    assert monitor.poll_once() is a
    assert changes == [(None, a)]
    assert monitor.poll_once() is b
    assert monitor.poll_once() is b  # 无变化不重复回调
    assert changes == [(None, a), (a, b)]
    assert monitor.current() is b


def test_monitor_poll_exception_signals_safe_stop() -> None:
    """TGT-003：轮询异常 -> on_change(old, None)，调用方必须安全停止。"""
    a = _window(1)
    state = {"calls": 0}

    def flaky_source() -> WindowInfo | None:
        state["calls"] += 1
        if state["calls"] >= 2:
            raise RuntimeError("win32 query failed")
        return a

    changes: list[tuple[WindowInfo | None, WindowInfo | None]] = []
    monitor = ForegroundMonitor(
        flaky_source, interval=0.01, on_change=lambda old, new: changes.append((old, new)),
        clock=FakeClock(),
    )
    monitor.poll_once()
    assert monitor.poll_once() is None
    assert changes[-1] == (a, None)
    assert monitor.current() is None  # 状态不确定时不保留旧快照


def test_monitor_thread_lifecycle_and_stop() -> None:
    """线程模式：注入 sleep 控制轮数，start/stop 生命周期可预期。"""
    a = _window(1)
    b = _window(2)
    seq = iter([a, b])
    changes: list[tuple[WindowInfo | None, WindowInfo | None]] = []
    monitor = ForegroundMonitor(
        lambda: next(seq, b), interval=0.01,
        on_change=lambda old, new: changes.append((old, new)),
        clock=FakeClock(),
    )
    polls = {"n": 0}
    original_poll = monitor.poll_once

    def counting_poll() -> WindowInfo | None:
        polls["n"] += 1
        if polls["n"] >= 4:
            monitor.stop(join_timeout=0)  # 从线程内安全停止（不自我 join）
        return original_poll()

    monitor.poll_once = counting_poll  # type: ignore[method-assign]
    monitor.start()
    assert monitor.running is True
    monitor._thread.join(timeout=2.0)  # type: ignore[union-attr]
    assert monitor.running is False
    assert polls["n"] == 4
    assert (None, a) in changes and (a, b) in changes
    monitor.stop()  # 幂等
    monitor.stop()


def test_monitor_rejects_nonpositive_interval() -> None:
    """interval 必须 > 0（防御性配置校验）。"""
    with pytest.raises(ValueError):
        ForegroundMonitor(lambda: None, interval=0)


# ---------------------------------------------------------------- INP-005


def test_verifier_accepts_matching_foreground() -> None:
    """前台 hwnd/pid/exe 与绑定一致 -> 通过。"""
    source = make_foreground_source
    del source  # 真实源在专门的真实环境用例中使用
    window = _window(100, pid=10, exe="ArenaLab.exe")
    verifier = ForegroundVerifier(lambda: window)
    assert verifier.verify(_binding()) == VerifyResult.pass_ok()


def test_verifier_rejects_wrong_window() -> None:
    """错窗（hwnd 不符）-> 拒绝并给出 hwnd_mismatch。"""
    verifier = ForegroundVerifier(lambda: _window(999))
    result = verifier.verify(_binding())
    assert result.ok is False
    assert "hwnd_mismatch" in result.reasons


def test_verifier_rejects_pid_mismatch() -> None:
    """同 hwnd 但 PID 不符（实例重启/HWND 复用）-> pid_mismatch。"""
    verifier = ForegroundVerifier(lambda: _window(100, pid=77))
    result = verifier.verify(_binding())
    assert result.ok is False
    assert "pid_mismatch" in result.reasons


def test_verifier_rejects_exe_mismatch() -> None:
    """exe 名不符 -> exe_mismatch。"""
    verifier = ForegroundVerifier(lambda: _window(100, pid=10, exe="Other.exe"))
    result = verifier.verify(_binding())
    assert result.ok is False
    assert "exe_mismatch" in result.reasons


def test_verifier_rejects_missing_or_failed_foreground() -> None:
    """无前台（锁屏等）与查询失败都必须拒绝，绝不假成功。"""
    verifier = ForegroundVerifier(lambda: None)
    assert verifier.verify(_binding()).reasons == ("no_foreground_window",)

    def broken() -> WindowInfo | None:
        raise RuntimeError("query failed")

    result = ForegroundVerifier(broken).verify(_binding())
    assert result.ok is False
    assert result.reasons == ("foreground_query_failed",)


def test_batch_verifier_maps_session_to_binding() -> None:
    """BatchForegroundVerifier：按 session_id 查绑定并复核整批。"""
    window = _window(100, pid=10)
    bindings = {"sess-1": _binding()}
    verifier = BatchForegroundVerifier(bindings, ForegroundVerifier(lambda: window))

    @dataclass
    class BatchLike:
        session_id: str

    assert verifier.verify_batch(BatchLike("sess-1")).ok is True
    result = verifier.verify_batch(BatchLike("sess-unknown"))
    assert result.ok is False
    assert result.reasons == ("session_not_bound",)


def test_real_foreground_verifier_roundtrip() -> None:
    """真实环境冒烟：以真实前台建立绑定 -> ForegroundVerifier 复核通过。"""
    source = make_foreground_source()
    window = source()
    assert window is not None  # 桌面会话内必有前台窗口
    binding = _binding(pid=window.pid, hwnd=window.hwnd, exe_name=window.exe_name)
    assert ForegroundVerifier(source).verify(binding).ok is True


# ---------------------------------------------------------------- TGT-009


def test_session_lock_verify_ok_with_injected_probes() -> None:
    """进程存活 + 前台一致 -> 锁有效。"""
    lock = SessionLock(
        process_alive=lambda pid: pid == 10,
        foreground_source=lambda: _window(100, pid=10),
    )
    lock.bind(_binding())
    assert lock.verify().ok is True
    assert lock.invalidated is False


def test_session_lock_pid_gone_invalidates_and_latches() -> None:
    """PID 消失 -> 失效；即使探针恢复也保持失效，需重新 bind（人工确认）。"""
    alive = {"value": True}
    lock = SessionLock(process_alive=lambda pid: alive["value"])
    lock.bind(_binding())
    alive["value"] = False
    result = lock.verify()
    assert result.ok is False and result.reasons == ("pid_gone",)
    assert lock.invalidated is True
    alive["value"] = True  # 表面恢复也必须人工重确认
    result2 = lock.verify()
    assert result2.ok is False
    assert result2.reasons[0] == "needs_manual_reconfirm"
    # 重新 bind（新 instance_token）后解除闩存。
    lock.bind(_binding(instance_token="tok-2"))
    assert lock.verify().ok is True


def test_session_lock_hwnd_changed_invalidates() -> None:
    """前台 HWND 变化 -> hwnd_changed 且闩存。"""
    current = {"hwnd": 100}
    lock = SessionLock(
        process_alive=lambda pid: True,
        foreground_source=lambda: _window(current["hwnd"], pid=10),
    )
    lock.bind(_binding())
    assert lock.verify().ok is True
    current["hwnd"] = 200
    result = lock.verify()
    assert result.ok is False and result.reasons == ("hwnd_changed",)
    assert lock.invalidated is True


def test_session_lock_unbound_and_foreground_missing() -> None:
    """未绑定即拒绝；前台消失（None/异常）按不存活处理。"""
    lock = SessionLock(process_alive=lambda pid: True, foreground_source=lambda: None)
    assert lock.verify().reasons == ("not_bound",)
    lock.bind(_binding())
    assert lock.verify().reasons == ("no_foreground_window",)

    def broken() -> WindowInfo | None:
        raise RuntimeError("boom")

    lock2 = SessionLock(process_alive=lambda pid: True, foreground_source=broken)
    lock2.bind(_binding())
    assert lock2.verify().reasons == ("no_foreground_window",)


def test_session_lock_default_probe_real_process() -> None:
    """默认探针（enumerate_win 权限探针）对自身进程判定存活。"""
    import os

    from window_service import WINDOW_SERVICE_AVAILABLE

    if not WINDOW_SERVICE_AVAILABLE:  # pragma: no cover
        pytest.skip("window_service 不可用")
    lock = SessionLock(process_alive=None)  # type: ignore[arg-type]
    lock.bind(_binding(pid=os.getpid()))
    assert lock.verify().ok is True


def test_session_lock_unbind_is_idempotent() -> None:
    """unbind 清空绑定与闩存；重复调用无额外效果。"""
    lock = SessionLock(process_alive=lambda pid: False)
    lock.bind(_binding())
    assert lock.verify().ok is False
    lock.unbind()
    lock.unbind()
    assert lock.binding is None and lock.invalidated is False
    assert lock.verify().reasons == ("not_bound",)


# ---------------------------------------------------------------- TGT-004


def test_diagnostics_reports_uipi_and_window_issues() -> None:
    """注入探针：UIPI 受限 + 不可见/最小化全部如实列出。"""
    reasons = check_access(
        _window(visible=False, minimized=True),
        probe=lambda pid: "target_elevated_uipi",
    )
    assert reasons == [
        "window_not_visible",
        "window_minimized",
        "target_elevated_uipi",
    ]


def test_diagnostics_ok_when_probe_passes() -> None:
    """探针通过且窗口可见 -> 空问题清单（此时才允许报告成功）。"""
    assert check_access(_window(), probe=lambda pid: None) == []
    assert check_access(_window(), probe=lambda pid: "process_open_denied") == [
        "process_open_denied"
    ]


def test_diagnostics_probe_exception_never_reports_false_success() -> None:
    """探针异常 -> 记为 process_probe_failed，绝不返回空清单假成功。"""

    def broken(pid: int) -> str | None:
        raise RuntimeError("probe crashed")

    reasons = check_access(_window(), probe=broken)
    assert reasons == ["process_probe_failed"]


def test_diagnostics_real_probe_on_live_process() -> None:
    """真实探针：对自身进程（可读）不产生权限原因。"""
    import os

    reasons = check_access(
        _window(pid=os.getpid()), probe=None
    )
    assert all(isinstance(r, str) for r in reasons)
    assert "process_open_denied" not in reasons


# ---------------------------------------------------------------- 静态守卫


def test_window_service_win32_imports_confined_to_enumerate_win() -> None:
    """静态守卫镜像：window_service 内 win32* 只允许 enumerate_win.py；
    ctypes / pyautogui 等真实输入设施一律禁止。"""
    pkg_dir = Path(window_service.__file__).parent
    win32_modules = {"win32gui", "win32con", "win32process", "win32api", "pywintypes"}
    forbidden_anywhere = {
        "ctypes", "pyautogui", "pydirectinput", "pynput", "keyboard", "mouse",
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
                assert name not in forbidden_anywhere, f"{path.name}: 禁止 import {name}"
                if name in win32_modules:
                    assert path.name == "enumerate_win.py", (
                        f"{path.name}: win32 import 只允许出现在 enumerate_win.py"
                    )


def test_verify_result_never_passes_without_reasons() -> None:
    """VerifyResult.fail 无原因时强制补默认原因（杜绝假成功）。"""
    assert VerifyResult.fail().reasons == ("unspecified_mismatch",)
    assert VerifyResult.fail("", "").reasons == ("unspecified_mismatch",)
    assert VerifyResult.fail("pid_gone", "").reasons == ("pid_gone",)
