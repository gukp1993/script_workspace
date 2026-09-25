"""桌面壳启动入口：``python -m desktop_shell [options]``。

用法::

    python -m desktop_shell                          # 生产模式：壳内置后端 + 窗口
    python -m desktop_shell --dev http://localhost:5173   # 前端开发：窗口指向 Vite
    python -m desktop_shell --backend-only           # 无 GUI：只起后端（前端开发用）

可选：``--project-root <dir>``（默认当前目录）、``--port N``（缺省取空闲
临时端口）、``--token <hex>``（缺省自动生成）。

直跑需注入包路径（见根 README）::

    export PYTHONPATH="packages;services;apps"
"""

from __future__ import annotations

import argparse
from pathlib import Path

from desktop_shell.shell import DesktopShell


def main(argv: list[str] | None = None) -> int:
    """解析命令行并运行桌面壳（阻塞直至窗口关闭/后端退出）。"""
    parser = argparse.ArgumentParser(
        prog="desktop_shell",
        description="VAW 桌面壳：进程内控制面 + pywebview 窗口（仅回环绑定）",
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd(), help="工作区根目录（默认当前目录）")
    parser.add_argument("--port", type=int, default=None, help="后端端口（默认自动取空闲端口）")
    parser.add_argument("--token", default=None, help="访问令牌（默认自动生成随机 hex）")
    parser.add_argument("--dev", "--url", dest="dev_url", default=None, metavar="URL",
                        help="dev 模式：窗口加载该 URL（如 Vite dev server），令牌自动拼入 query")
    parser.add_argument("--backend-only", action="store_true", help="只启动后端，不开窗口（无头开发）")
    args = parser.parse_args(argv)

    shell = DesktopShell(
        project_root=args.project_root,
        token=args.token,
        port=args.port,
        dev_url=args.dev_url,
        backend_only=args.backend_only,
    )
    return shell.run()


if __name__ == "__main__":
    raise SystemExit(main())
