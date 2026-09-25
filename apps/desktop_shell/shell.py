"""pywebview 桌面壳（UI-001）。

职责（M1 基础版）：
- 启动时生成/读取访问令牌（CTL-001）；
- 在**后台线程**用进程内 ``uvicorn.Server`` 启动 control_plane（硬编码
  绑定 127.0.0.1，端口缺省取空闲临时端口）；
- pywebview 创建窗口并加载 ``http://127.0.0.1:<port>/?token=<t>``；
  dev 模式可用 ``--dev <url>`` 指向 Vite dev server（令牌仍拼入 query）；
- 关闭顺序：webview ``start()`` 返回（窗口已关）→ ``server.should_exit
  = True`` → join 后台线程，确保退出不留后台进程；
- ``--backend-only`` 只起后端（无 GUI 环境的前端开发用），绝不触碰 webview。

可测性设计：server 与 webview 均可注入 fake（:class:`BackendServer` /
webview 模块对象），单测不真开窗口；pywebview 的 import 延迟到需要时，
缺失时抛出带明确指引的 :class:`WebviewUnavailableError`。
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import uvicorn

from control_plane.app import create_app
from control_plane.config import ControlPlaneConfig

#: 等待后端启动完成的超时（秒）
DEFAULT_STARTUP_TIMEOUT: float = 20.0

#: 关闭后等待后端线程退出的超时（秒）
DEFAULT_SHUTDOWN_TIMEOUT: float = 15.0


class BackendStartupError(RuntimeError):
    """后端在超时内未完成启动。"""


class WebviewUnavailableError(RuntimeError):
    """当前环境无法使用 pywebview（未安装或缺 GUI 运行时）。"""


class BackendServer(Protocol):
    """桌面壳依赖的最小 server 协议（``uvicorn.Server`` 天然满足）。"""

    started: bool
    should_exit: bool

    def run(self) -> None:
        """阻塞运行直至 ``should_exit`` 置位（在后台线程调用）。"""


def pick_free_loopback_port() -> int:
    """向系统申请一个 127.0.0.1 的空闲 TCP 端口（存在极小竞态，可接受）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def build_url(base_url: str, token: str) -> str:
    """把令牌拼入 URL 查询参数（兼容 base 已带 ``?`` 的情况）。"""
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}token={token}"


def _default_server_factory(config: ControlPlaneConfig) -> BackendServer:
    """构造进程内 uvicorn.Server（仅回环绑定，CTL-001）。"""
    uv_config = uvicorn.Config(
        create_app(config),
        host="127.0.0.1",
        port=config.port,
        log_level="info",
    )
    return uvicorn.Server(uv_config)


def _import_webview() -> Any:
    """延迟导入 pywebview；缺失/不可用时抛出明确错误。"""
    try:
        import webview  # type: ignore[import-untyped]
    except Exception as exc:  # ImportError 或 GUI 后端初始化问题
        raise WebviewUnavailableError(
            "pywebview 不可用：请先 `pip install pywebview`，且本机需有 GUI 桌面会话。"
            "无 GUI 环境请改用 `--backend-only` 只启动后端。"
            f"（原始错误：{exc!r}）"
        ) from exc
    return webview


class DesktopShell:
    """桌面壳：进程内后端 + pywebview 窗口（均可注入替身用于测试）。

    Attributes:
        config: 控制面配置（含自动生成或显式传入的令牌与端口）。
    """

    def __init__(
        self,
        *,
        project_root: Path | str,
        token: str | None = None,
        port: int | None = None,
        dev_url: str | None = None,
        backend_only: bool = False,
        window_title: str = "VAW 前台视觉自动化工作台",
        window_size: tuple[int, int] = (1280, 800),
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        server_factory: Callable[[ControlPlaneConfig], BackendServer] | None = None,
        webview_module: Any | None = None,
    ) -> None:
        self.config = ControlPlaneConfig(
            project_root=Path(project_root),
            token=token,  # None 时 config 自动生成随机令牌（CTL-001）
            port=port if port is not None else pick_free_loopback_port(),
        )
        self.dev_url = dev_url
        self.backend_only = backend_only
        self.window_title = window_title
        self.window_size = window_size
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self._server_factory = server_factory or _default_server_factory
        self._webview_module = webview_module
        self._server: BackendServer | None = None
        self._thread: threading.Thread | None = None
        self.events: list[str] = []  # 启动/关闭关键事件（供测试断言顺序）

    # -- 生命周期 -----------------------------------------------------------

    @property
    def base_url(self) -> str:
        """后端回环基地址（带尾斜杠，便于拼接路径/query）。"""
        return f"http://127.0.0.1:{self.config.port}/"

    def start_backend(self) -> str:
        """在后台线程启动 control_plane，阻塞等待就绪；返回基地址。

        启动超时抛 :class:`BackendStartupError`（调用方负责 ``shutdown``）。
        """
        if self._thread is not None:
            raise RuntimeError("后端已启动")
        self._server = self._server_factory(self.config)
        self._thread = threading.Thread(
            target=self._server.run,
            name="vaw-control-plane",
            daemon=True,  # 兜底：异常退出时不悬挂解释器；正常路径仍显式 join
        )
        self.events.append("server_thread_start")
        self._thread.start()

        deadline = time.monotonic() + self.startup_timeout
        while not self._server.started:
            if not self._thread.is_alive():
                raise BackendStartupError("后端线程在启动阶段退出（端口占用或应用装配失败）")
            if time.monotonic() >= deadline:
                raise BackendStartupError(f"后端 {self.startup_timeout}s 内未完成启动")
            time.sleep(0.02)
        self.events.append("server_started")
        return self.base_url

    def run(self) -> int:
        """完整生命周期：起后端 →（开窗口 | 仅后端）→ 优雅关闭。

        Returns:
            进程退出码：0 正常；1 启动失败。
        """
        try:
            self.start_backend()
        except BackendStartupError as exc:
            print(f"[desktop_shell] 后端启动失败：{exc}", flush=True)
            self.shutdown()
            return 1

        if self.backend_only:
            try:
                print(
                    "=" * 64,
                    f"[backend-only] API: {self.base_url}api/v1",
                    f"[backend-only] 令牌: {self.config.token}（请勿泄露）",
                    "按 Ctrl+C 退出",
                    "=" * 64,
                    sep="\n",
                    flush=True,
                )
                # 阻塞等待后端线程退出；Ctrl+C 时走 finally 优雅关闭
                assert self._thread is not None
                while self._thread.is_alive():
                    self._thread.join(timeout=0.2)
                self.events.append("backend_only_exit")
                return 0
            finally:
                self.shutdown()

        try:
            # 延迟解析 webview：缺失时给出明确指引（--backend-only 不受影响）
            try:
                webview = self._webview_module if self._webview_module is not None else _import_webview()
            except WebviewUnavailableError as exc:
                print(f"[desktop_shell] {exc}", flush=True)
                return 1

            url = self.dev_url if self.dev_url else self.base_url
            url = build_url(url, self.config.token or "")
            self.events.append("webview_window_create")
            webview.create_window(self.window_title, url, width=self.window_size[0], height=self.window_size[1])
            self.events.append("webview_start")
            webview.start()  # 阻塞至窗口关闭（webview 由此先行停止）
            self.events.append("webview_stopped")
            return 0
        finally:
            # 优雅关闭：webview 已停 → 置 should_exit → join，不留后台进程
            self.shutdown()

    def shutdown(self) -> bool:
        """请求后端停止并等待线程退出；返回是否在超时内干净退出。"""
        server, thread = self._server, self._thread
        if server is not None:
            self.events.append("server_should_exit")
            server.should_exit = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.shutdown_timeout)
            self.events.append("server_thread_joined")
        self._server, self._thread = None, None
        return thread is None or not thread.is_alive()
