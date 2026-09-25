"""控制面启动入口：``python -m control_plane --project-root <dir> [--port N]``。

安全约定（CTL-001）：
- uvicorn **硬编码绑定 127.0.0.1**，任何情况下不对外网/局域网暴露；
- 访问令牌每次启动自动生成（随机 hex），仅打印到本地控制台；桌面壳
  （desktop_shell）作为本地子进程读取后，经 ``X-VAW-Token`` 头调用 API。

直跑需注入包路径（见根 README）::

    export PYTHONPATH="packages;services"
    python -m control_plane --project-root ./my-workspace
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from control_plane.app import create_app
from control_plane.config import DEFAULT_PORT, ControlPlaneConfig


def main(argv: list[str] | None = None) -> int:
    """解析命令行并启动控制面（阻塞直至服务退出）。"""
    parser = argparse.ArgumentParser(prog="control_plane", description="VAW 本地控制面（仅回环绑定）")
    parser.add_argument("--project-root", required=True, type=Path, help="工作区根目录")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    args = parser.parse_args(argv)

    config = ControlPlaneConfig(project_root=args.project_root, port=args.port)
    assert config.token is not None  # 构造时自动生成

    # 令牌只打印到本地控制台（CTL-001），不落盘、不进日志文件
    print("=" * 64)
    print("VAW 控制面启动（仅绑定 127.0.0.1；请勿泄露访问令牌）")
    print(f"  地址: http://127.0.0.1:{config.port}/api/v1")
    print(f"  令牌: {config.token}")
    print(f"  工作区: {config.project_root.resolve()}")
    print("=" * 64)

    app = create_app(config)
    uvicorn.run(app, host="127.0.0.1", port=config.port, log_level="info")  # 硬编码回环绑定
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
