"""桌面壳单测（E11：UI-001）。

覆盖（全部用 fake server / fake webview，**绝不真开窗口**）：
- 启动顺序：server 线程先于 webview 窗口，关闭时 webview 先停、
  server 后停、线程被 join；
- 令牌注入：窗口 URL 携带 ``?token=<t>``；dev 模式令牌拼入 Vite URL；
- 优雅关闭：``should_exit`` 置位、线程在超时内退出（不留后台进程）；
- backend-only：绝不触碰 webview，后端线程退出后 run() 返回；
- 失败路径：后端线程启动即退 / 启动超时 → 返回 1 且完成清理；
  pywebview 不可用 → 明确错误、返回 1、后端仍被优雅关闭；
- 令牌策略：显式令牌被采用；缺省自动生成 40 位随机 hex；
- ``build_url``：兼容 base 已带查询串的情况。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

import desktop_shell.shell as shell_mod
from desktop_shell.shell import (
    BackendStartupError,
    DesktopShell,
    WebviewUnavailableError,
    build_url,
)

TOKEN = "shell-test-token"


# ---------------------------------------------------------------------------
# Fakes（绝不真开窗口 / 不起真实网络服务）
# ---------------------------------------------------------------------------


class FakeServer:
    """uvicorn.Server 替身：写入统一事件时间线，可控制启动成败与自动退出。"""

    started: bool
    should_exit: bool

    def __init__(
        self,
        *,
        startup_delay: float = 0.0,
        die_on_start: bool = False,
        never_starts: bool = False,
        auto_exit_after: float | None = None,
    ) -> None:
        self.started = False
        self.should_exit = False
        self._timeline: list[str] = []
        self._startup_delay = startup_delay
        self._die_on_start = die_on_start
        self._never_starts = never_starts
        self._auto_exit_after = auto_exit_after

    def run(self) -> None:
        self._timeline.append("server_run_entered")
        if self._die_on_start:
            return  # 模拟端口占用/装配失败：线程直接退出且未 started
        if not self._never_starts:
            time.sleep(self._startup_delay)
            self.started = True
        # never_starts：模拟后端卡死在启动阶段（started 永不置位）
        deadline = None if self._auto_exit_after is None else time.monotonic() + self._auto_exit_after
        while not self.should_exit:
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        self._timeline.append("server_run_exited")


class FakeWebview:
    """pywebview 模块替身：start() 立即返回，模拟窗口被关闭。"""

    def __init__(self) -> None:
        self._timeline: list[str] = []
        self.windows: list[dict[str, Any]] = []
        self.started_count = 0

    def create_window(self, title: str, url: str, *, width: int, height: int) -> None:
        self._timeline.append("window_created")
        self.windows.append({"title": title, "url": url, "width": width, "height": height})

    def start(self) -> None:  # 模拟用户关闭窗口后阻塞调用返回
        self.started_count += 1
        self._timeline.append("webview_start_returned")


class StrictWebview:
    """backend-only 用：一旦被触碰即让测试失败。"""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"backend-only 模式不得触碰 webview（调用了 {name}）")


def make_shell(
    server: FakeServer,
    webview: FakeWebview | None = None,
    **overrides: Any,
) -> DesktopShell:
    """构造注入 fake 的桌面壳：fake 与 shell 写入同一条事件时间线。"""
    defaults: dict[str, Any] = {
        "project_root": Path.cwd(),
        "token": TOKEN,
        "startup_timeout": 5.0,
        "webview_module": webview,
    }
    defaults.update(overrides)
    shell = DesktopShell(**defaults)
    shell._server_factory = lambda _config: server  # 注入 fake server
    server._timeline = shell.events  # 统一时间线：fake 事件写入 shell.events
    if webview is not None:
        webview._timeline = shell.events
    return shell


# ---------------------------------------------------------------------------
# 正常流程
# ---------------------------------------------------------------------------


def test_startup_order_and_graceful_shutdown() -> None:
    """启动：后端先就绪再开窗口；关闭：webview 先停 → should_exit → join。"""
    server = FakeServer(startup_delay=0.05)
    webview = FakeWebview()
    shell = make_shell(server, webview)

    assert shell.run() == 0

    assert shell.events == [
        "server_thread_start",
        "server_run_entered",
        "server_started",
        "webview_window_create",
        "window_created",
        "webview_start",
        "webview_start_returned",  # 窗口先关闭（webview 先停）
        "webview_stopped",
        "server_should_exit",      # 然后才请求后端停止
        "server_run_exited",
        "server_thread_joined",    # 最后 join 线程，不留后台进程
    ]


def test_token_injected_into_window_url() -> None:
    """窗口 URL 为 http://127.0.0.1:<port>/?token=<t>，标题/尺寸正确。"""
    server = FakeServer()
    webview = FakeWebview()
    shell = make_shell(server, webview, window_size=(800, 600))

    assert shell.run() == 0
    assert len(webview.windows) == 1
    window = webview.windows[0]
    expected = f"http://127.0.0.1:{shell.config.port}/?token={TOKEN}"
    assert window["url"] == expected
    assert window["title"] == "VAW 前台视觉自动化工作台"
    assert (window["width"], window["height"]) == (800, 600)
    # 显式令牌被原样采用
    assert shell.config.token == TOKEN


def test_dev_url_also_gets_token() -> None:
    """dev 模式：窗口指向 Vite dev server，令牌自动拼入其 query。"""
    server = FakeServer()
    webview = FakeWebview()
    shell = make_shell(
        server,
        webview,
        dev_url="http://localhost:5173/?foo=1",  # 已带查询串 -> 应拼 &token=
    )

    assert shell.run() == 0
    url = webview.windows[0]["url"]
    assert url.startswith("http://localhost:5173/?foo=1&token=")
    assert TOKEN in url


def test_token_autogenerated_when_absent() -> None:
    """缺省令牌由 config 自动生成（40 位随机 hex，CTL-001）。"""
    shell = DesktopShell(project_root=Path.cwd())
    token = shell.config.token
    assert token is not None and len(token) == 40
    assert all(ch in "0123456789abcdef" for ch in token)
    # 两次生成不同（随机性）
    other = DesktopShell(project_root=Path.cwd())
    assert other.config.token != token


def test_build_url_handles_existing_query() -> None:
    """build_url：无查询串拼 ?，已有查询串拼 &。"""
    assert build_url("http://x:1/", "t") == "http://x:1/?token=t"
    assert build_url("http://x:1/?a=b", "t") == "http://x:1/?a=b&token=t"


# ---------------------------------------------------------------------------
# backend-only
# ---------------------------------------------------------------------------


def test_backend_only_never_touches_webview() -> None:
    """backend-only：webview 完全不被触碰；后端线程退出后 run() 返回 0。"""
    server = FakeServer(auto_exit_after=0.1)  # 模拟自行退出（如 Ctrl+C 清理后）
    shell = make_shell(server, None, backend_only=True, webview_module=StrictWebview())

    assert shell.run() == 0
    assert "server_run_entered" in shell.events
    assert "window_created" not in shell.events
    # 退出前仍完成优雅关闭
    assert "server_should_exit" in shell.events
    assert "server_thread_joined" in shell.events


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


def test_backend_thread_dies_on_start_returns_1() -> None:
    """后端线程启动即退（如端口被占）：返回 1，不触碰 webview。"""
    server = FakeServer(die_on_start=True)
    webview = FakeWebview()
    shell = make_shell(server, webview)

    assert shell.run() == 1
    # 仍执行了清理：should_exit -> join
    assert shell.events[-2:] == ["server_should_exit", "server_thread_joined"]
    assert "window_created" not in shell.events
    # start_backend 对同一失败注入再次抛出结构化错误
    with pytest.raises(BackendStartupError):
        shell.start_backend()


def test_backend_startup_timeout_returns_1_and_cleans_up() -> None:
    """启动超时：返回 1，should_exit 置位并 join 线程。"""
    server = FakeServer(never_starts=True)  # started 永远不会被置位
    webview = FakeWebview()
    shell = make_shell(server, webview, startup_timeout=0.15)

    assert shell.run() == 1
    assert server.should_exit is True
    assert "window_created" not in shell.events
    # 等待 fake run 循环退出，确认无残留后台线程
    for _ in range(100):
        if not any(t.name == "vaw-control-plane" and t.is_alive() for t in threading.enumerate()):
            break
        time.sleep(0.02)
    assert not any(t.name == "vaw-control-plane" and t.is_alive() for t in threading.enumerate())


def test_webview_unavailable_returns_1_and_stops_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """pywebview 不可用：明确错误、返回 1、后端仍被优雅关闭。"""

    def _raise() -> Any:
        raise WebviewUnavailableError("pywebview 不可用（测试注入）")

    monkeypatch.setattr(shell_mod, "_import_webview", _raise)
    server = FakeServer()
    shell = make_shell(server, None, webview_module=None)  # 走真实 _import_webview 路径

    assert shell.run() == 1
    assert server.should_exit is True
    assert "server_should_exit" in shell.events
    assert "window_created" not in shell.events
