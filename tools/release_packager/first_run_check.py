"""首次启动检查（REL-005）：安装/升级后首启前的环境自检。

用法::

    python tools/release_packager/first_run_check.py [--workspace <目录>]

检查项（PASS/FAIL/SKIP 逐项打印，任一 FAIL 退出码 1）：
  [1] 工作区目录可写   —— 默认当前目录，可 --workspace 指定；
  [2] Python 版本      —— 要求 >= 3.12（ENG-002）；
  [3] 依赖可导入       —— 第三方锁定依赖 + 本仓包（按 pytest pythonpath 注入）；
  [4] 显示器可用       —— tkinter 试开主窗口；无 GUI 库 -> SKIP（backend-only 可用）；
  [5] 旧版本迁移状态   —— 占位实现：检查迁移失败/进行中标记文件（VER-009 完整版随迁移矩阵交付）。
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 第三方锁定依赖（requirements.txt 的可导入名；dxcam 等真实采集适配器
#: 依赖具体驱动环境，属于运行期冒烟范畴，不在首启静态检查内）。
THIRD_PARTY_IMPORTS: tuple[str, ...] = (
    "numpy", "yaml", "jsonschema", "PIL", "pydantic", "cv2",
    "pytest", "fastapi", "uvicorn", "httpx", "mss",
)

#: 本仓包（导入约定见 README；需先把包路径注入 sys.path）。
LOCAL_IMPORTS: tuple[str, ...] = (
    "common", "domain_model", "policy_engine", "trace_format", "vision_core",
    "state_machine", "capture_api", "input_broker", "runtime_engine",
    "control_plane", "arena_lab", "desktop_shell", "window_service",
    "release_kit", "security_kit", "test_kit",
)

#: 迁移状态标记文件名（占位约定；VER-009 完整实现时替换为迁移矩阵查询）。
MIGRATION_FAILED_MARKER = ".migration_failed"
MIGRATION_IN_PROGRESS_MARKER = ".migration_in_progress"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def _check(name: str, ok: bool, note: str = "", status: str | None = None) -> tuple[str, str, str]:
    """规整一项检查结果：(状态, 名称, 说明)。"""
    return (status or (PASS if ok else FAIL), name, note)


def check_workspace_writable(workspace: Path) -> tuple[str, str, str]:
    """[1] 工作区目录可写：临时文件创建+删除。"""
    if not workspace.is_dir():
        return _check("工作区目录存在且可写", False, f"目录不存在：{workspace}")
    try:
        with tempfile.NamedTemporaryFile(dir=workspace, prefix=".first_run_", delete=True):
            pass
        return _check("工作区目录存在且可写", True, str(workspace))
    except OSError as exc:
        return _check("工作区目录存在且可写", False, f"{workspace}：{exc}")


def check_python_version() -> tuple[str, str, str]:
    """[2] Python 版本 >= 3.12。"""
    version = sys.version_info
    ok = (version.major, version.minor) >= (3, 12)
    return _check(
        "Python 版本 >= 3.12",
        ok,
        f"{version.major}.{version.minor}.{version.micro}" + ("" if ok else "（需 3.12+，见 requirements.txt/README）"),
    )


def check_dependencies() -> tuple[str, str, str]:
    """[3] 第三方依赖与本仓包可导入。"""
    missing: list[str] = []
    for name in THIRD_PARTY_IMPORTS:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - 任何导入失败都算缺失
            missing.append(name)
    for entry in ("packages", "services", "apps"):
        path = str(ROOT / entry)
        if path not in sys.path:
            sys.path.insert(0, path)
    for name in LOCAL_IMPORTS:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{name}（{type(exc).__name__}: {exc}）")
    if missing:
        return _check("依赖可导入", False, "缺失/失败：" + ", ".join(missing))
    return _check(
        "依赖可导入",
        True,
        f"第三方 {len(THIRD_PARTY_IMPORTS)} 项 + 本仓 {len(LOCAL_IMPORTS)} 项全部可导入",
    )


def check_display() -> tuple[str, str, str]:
    """[4] 显示器可用：tkinter 试开主窗口（真实显示器/GUI 会话）。"""
    try:
        import tkinter
    except ImportError:
        return _check(
            "显示器可用", True, "无 tkinter/GUI 环境：SKIP（backend-only 模式可用；E2E 与真实输入需真实显示器）",
            status=SKIP,
        )
    try:
        root = tkinter.Tk()
        screens = root.winfo_screenwidth(), root.winfo_screenheight()
        root.destroy()
        return _check("显示器可用", True, f"{screens[0]}x{screens[1]}")
    except Exception as exc:  # noqa: BLE001 - 无法开窗（服务会话/权限等）
        return _check("显示器可用", False, f"tkinter 打开失败：{exc}")


def check_migration_state(workspace: Path) -> tuple[str, str, str]:
    """[5] 旧版本迁移状态（占位）：检查迁移失败/进行中标记。"""
    failed = workspace / MIGRATION_FAILED_MARKER
    in_progress = workspace / MIGRATION_IN_PROGRESS_MARKER
    if failed.is_file():
        return _check(
            "旧版本迁移状态（占位）", False,
            "发现迁移失败标记：" + str(failed) + "；先按 docs/operations/install-upgrade.md §3.3 恢复",
        )
    if in_progress.is_file():
        return _check(
            "旧版本迁移状态（占位）", False,
            "迁移进行中标记未清除：" + str(in_progress) + "；重跑升级或按 §3.2 恢复",
        )
    return _check(
        "旧版本迁移状态（占位）", True,
        "未发现迁移失败/进行中标记（占位检查；VER-009 完整迁移矩阵交付后替换）",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="first_run_check.py",
        description="首次启动检查（REL-005）：目录可写/Python 版本/依赖/显示器/迁移状态",
    )
    parser.add_argument(
        "--workspace", type=Path, default=Path.cwd(),
        help="工作区目录（默认当前目录）",
    )
    args = parser.parse_args(argv)

    checks = [
        check_workspace_writable(args.workspace),
        check_python_version(),
        check_dependencies(),
        check_display(),
        check_migration_state(args.workspace),
    ]

    failed = sum(1 for status, _, _ in checks if status == FAIL)
    skipped = sum(1 for status, _, _ in checks if status == SKIP)

    print("=" * 64)
    print("首次启动检查（REL-005）")
    print("=" * 64)
    for status, name, note in checks:
        line = f"[{status}] {name}"
        if note:
            line += f"\n       {note}"
        print(line)
    print("=" * 64)
    print(
        f"结果：{len(checks) - failed}/{len(checks)} 项通过"
        f"（FAIL {failed}，SKIP {skipped}；SKIP 不判定失败）"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
