"""M0 验收门（对应《完整开发任务与测试验收方案》§12.1 M0 验收清单）。

用法：python tools/acceptance/run_m0_checks.py
任一检查失败则退出码 1。CI 与本地验收共用。

检查项映射：
  [1] 工程底座     —— pytest 全量单测通过（ENG-002/004/TST-002）
  [2] Schema 正例  —— examples/arena_lab_demo 通过全部 Schema 校验（DOM-002/003/009）
  [3] Schema 反例  —— protected_online 反例被拒绝且给出规则 ID（POL-003/SAFE-020）
  [4] ArenaLab     —— 固定种子确定性帧序列自检（LAB-001/004）
  [5] 策略安全     —— Shadow/错窗/过期/预算/硬锁场景演示通过（POL/INP/SAFE-004/020）
  [6] 静态守卫     —— 真实输入库全仓禁止；系统级绑定按 M1 allowlist 精确放行
                      （GOV-001/SEC-001；M1 起与 run_m1_checks 共用同一规则）
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# python -m 直跑需要与 pytest pythonpath 一致的搜索路径（ENG-002）
SUBPROCESS_ENV = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join(["packages", "services", "apps"]),
}

# 静态守卫（SEC-001、架构不变量 1/2/8）。M1 起按 allowlist 精确放行：
# - ctypes 允许且仅允许出现在 input_broker 的三个 Win32 绑定文件
#   （SendInput / 热键 / 看门狗——全平台唯一的系统调用集中地）；
# - win32api/win32gui/win32con/win32process/pywintypes 允许且仅允许出现在
#   services/window_service/ 目录内（当前集中于 enumerate_win.py）；
# - pyautogui/pydirectinput/pynput/keyboard/mouse 仍然全仓禁止（含上述文件）。
FORBIDDEN_EVERYWHERE = ("pyautogui", "pydirectinput", "pynput", "keyboard", "mouse")
FORBIDDEN_WIN32 = ("win32api", "win32gui", "win32con", "win32process", "pywintypes")

#: ctypes 的精确白名单（相对 POSIX 路径）。
CTYPES_ALLOWLIST = frozenset(
    {
        "services/input_broker/win32_adapter.py",
        "services/input_broker/win32_hotkey.py",
        "services/input_broker/win32_watchdog.py",
    }
)
#: win32* 系模块的精确白名单目录（目录内文件放行）。
WIN32_ALLOWLIST_DIR = "services/window_service/"

SCAN_DIRS = ("packages", "services", "apps")
SCAN_EXCLUDE = {"__pycache__"}  # 排除的目录名


def run(cmd: list[str], name: str, expect_zero: bool = True) -> tuple[bool, str]:
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=SUBPROCESS_ENV,
    )
    ok = (proc.returncode == 0) == expect_zero
    detail = (proc.stdout + proc.stderr).strip()
    return ok, detail


def static_guard() -> tuple[bool, str]:
    """扫描源码 import，禁止真实输入与越权模块；允许注释中提及。

    规则（M1 allowlist）：
    - pyautogui/pydirectinput/pynput/keyboard/mouse：全仓禁止；
    - ctypes：仅允许 CTYPES_ALLOWLIST 中列出的三个 Win32 绑定文件；
    - win32api/win32gui/win32con/win32process/pywintypes：仅允许
      services/window_service/ 目录内的文件；
    - 白名单之外任何文件出现这些 import 一律 FAIL。
    """
    violations: list[str] = []
    pattern = re.compile(
        r"^\s*(?:import|from)\s+("
        + "|".join(FORBIDDEN_EVERYWHERE + FORBIDDEN_WIN32 + ("ctypes",))
        + r")\b",
        re.MULTILINE,
    )
    for scan_dir in SCAN_DIRS:
        for path in (ROOT / scan_dir).rglob("*.py"):
            if SCAN_EXCLUDE & set(path.parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            for match in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")):
                module = match.group(1)
                if module == "ctypes":
                    if rel not in CTYPES_ALLOWLIST:
                        violations.append(
                            f"{rel}: import ctypes（仅允许 {sorted(CTYPES_ALLOWLIST)}）"
                        )
                elif module in FORBIDDEN_WIN32:
                    if not rel.startswith(WIN32_ALLOWLIST_DIR):
                        violations.append(
                            f"{rel}: import {module}（仅允许 {WIN32_ALLOWLIST_DIR} 目录内）"
                        )
                else:
                    violations.append(f"{rel}: import {module}")
    if violations:
        return False, "发现禁止的 import：\n  " + "\n  ".join(violations)
    return True, (
        "未发现越权 import；ctypes 仅限三处 Win32 绑定文件，"
        "win32* 仅限 window_service，真实输入库全仓禁止"
    )


def main() -> int:
    python = sys.executable
    checks: list[tuple[str, bool, str]] = []

    ok, detail = run([python, "-m", "pytest", "tests", "-q", "--no-header"], "[1] pytest 全量单测")
    checks.append(("pytest 全量单测", ok, detail.splitlines()[-1] if detail else ""))

    ok, detail = run(
        [python, "-m", "domain_model.validate", "examples/arena_lab_demo"], "[2] Schema 正例"
    )
    checks.append(("示例项目 Schema 校验通过", ok, detail.splitlines()[-1] if detail else ""))

    ok, detail = run(
        [python, "-m", "domain_model.validate", "examples/protected_online_demo"],
        "[3] Schema 反例（受保护目标拒绝）",
        expect_zero=False,
    )
    rejected = ok and "protected_online_no_real_input" in detail
    checks.append(("protected_online 反例被拒绝且给出规则 ID", rejected, ""))

    ok, detail = run([python, "-m", "arena_lab.selfcheck"], "[4] ArenaLab 确定性自检")
    checks.append(("ArenaLab 固定种子帧序列确定", ok, detail.splitlines()[-1] if detail else ""))

    ok, detail = run([python, "-m", "policy_engine.demo"], "[5] 策略安全场景演示")
    checks.append(("策略/输入安全场景通过", ok, detail.splitlines()[-1] if detail else ""))

    ok, detail = static_guard()
    checks.append(("静态守卫：无禁止 import", ok, detail))

    print("\n" + "=" * 64)
    print("M0 验收门（§12.1）")
    print("=" * 64)
    failed = 0
    for name, passed, note in checks:
        mark = "PASS" if passed else "FAIL"
        failed += 0 if passed else 1
        line = f"[{mark}] {name}"
        if note and not passed:
            line += f"\n       {note[:800]}"
        print(line)
    print("=" * 64)
    print(f"结果：{len(checks) - failed}/{len(checks)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
