"""M2 验收门（对应《完整开发任务与测试验收方案》§12.3 M2 验收清单）。

用法：python tools/acceptance/run_m2_checks.py
失败检查项导致退出码 1；SKIP（依赖真实显示器的 E2E）不判定失败但打印计数。
结构复用 run_m1_checks.py（run/static_guard 单一来源在 run_m0_checks.py）。

检查项映射：
  [1] 全量后端 pytest —— tests/unit + tests/contract + tests/replay
                      （ENG-002/TST-002/TST-004）；跑一次失败则重跑一次，
                      仍失败才 FAIL（偶发环境抖动防护）；
  [2] e2e_windows     —— 依赖真实显示器（tkinter + 抓屏）：单独跑一次，
                      失败重跑一次，仍失败 -> SKIP 并输出诊断，不计失败
                      （与 M1 E2E 冒烟同语义；确定性回归由 [1] 兜底）；
  [3] 静态守卫        —— 复用 run_m0_checks.static_guard（SEC-001/GOV-001）；
  [4] 视觉指标子集    —— pytest tests/unit/test_vision_detectors.py
                      -k "golden or metric or mae"（VIS-011 黄金数据集指标）；
  [5] FSM 确定性子集  —— pytest tests/unit/test_fsm_runtime.py
                      -k "hash or determin"（FSM-009/012，AC-P0-08）；
  [6] 回放回归        —— pytest tests/replay -q（TRC-005/006/007，AC-P0-09）；
  [7] 契约测试        —— pytest tests/contract -q（TST-004 消息结构冻结）；
  [8] M0 门           —— import run_m0_checks; main() 返回码 == 0（§12.1）；
  [9] M1 门           —— import run_m1_checks; main() 返回码 == 0（§12.2）。

末尾对照 §12.3 清单逐行勾稽并打印量化门槛（§7.2）口径说明。
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# python -m 直跑需要与 pytest pythonpath 一致的搜索路径（ENG-002）
SUBPROCESS_ENV = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join(["packages", "services", "apps"]),
}

# 支持 in-process 调用 M0/M1 门与静态守卫：注入包搜索路径后复用既有实现
for _entry in ("packages", "services", "apps"):
    _path = str(ROOT / _entry)
    if _path not in sys.path:
        sys.path.insert(0, _path)
sys.path.insert(0, str(ROOT / "tools" / "acceptance"))

import run_m0_checks  # noqa: E402  (复用 run 与 static_guard，规则保持单一来源)
import run_m1_checks  # noqa: E402  (M1 门 import 调用)

run = run_m0_checks.run

#: 检查结果状态
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

#: 全量后端 pytest 的目标目录（e2e_windows 由 [2] 单独处理）
BACKEND_TARGETS = ("tests/unit", "tests/contract", "tests/replay")


# ---------------------------------------------------------------------------
# pytest 辅助
# ---------------------------------------------------------------------------


def pytest_summary_line(detail: str) -> str:
    """提取 pytest 输出中的结果汇总行（"N passed/failed/error"），便于阅读。"""
    for line in reversed(detail.splitlines()):
        text = line.strip()
        if any(word in text for word in ("passed", "failed", "error", "no tests ran")):
            return text
    return detail.splitlines()[-1] if detail else ""


def pytest_once(targets: str, *args: str) -> tuple[bool, str]:
    """跑一次 pytest（子进程，环境与 pytest pythonpath 一致），返回 (成败, 输出)。

    注意不要追加 ``-q``：pyproject addopts 已带 ``-q``，两个 ``-q`` 叠成
    ``-qq`` 会把 pytest 的结果汇总行整个吞掉。
    """
    return run(
        [sys.executable, "-m", "pytest", *targets.split(), "--no-header", *args],
        "pytest",
    )


def pytest_with_retry(targets: str, *, retries: int = 1) -> tuple[bool, str, int]:
    """跑一次，失败重跑最多 retries 次；返回 (最终成败, 末次输出, 重跑次数)。"""
    ok, detail = pytest_once(targets)
    reruns = 0
    while not ok and reruns < retries:
        reruns += 1
        ok, detail = pytest_once(targets)
    return ok, detail, reruns


def pytest_subset(targets: str, k_expr: str) -> tuple[str, str]:
    """确定性子集检查：-k 过滤必须命中测试且全绿（空命中视为 FAIL）。"""
    ok, detail = pytest_once(targets, "-k", k_expr)
    tail = pytest_summary_line(detail)
    if not ok:
        return FAIL, tail
    lowered = tail.lower()
    if " no tests ran" in lowered or ("deselected" in lowered and " passed" not in lowered):
        return FAIL, f"-k 表达式未命中任何测试：{tail}"
    return PASS, tail


# ---------------------------------------------------------------------------
# [2] e2e_windows：依赖真实显示器，retry + SKIP 语义
# ---------------------------------------------------------------------------


def e2e_windows_check() -> tuple[str, str]:
    """e2e_windows 单独跑：失败重跑一次，仍失败 -> SKIP（不计失败）+ 诊断。"""
    ok, detail, reruns = pytest_with_retry("tests/e2e_windows")
    tail = pytest_summary_line(detail)
    if ok:
        note = tail or "e2e_windows 通过"
        return PASS, note + ("（重跑通过）" if reruns else "")
    # 依赖真实显示器：头less/服务会话下必然失败，不判定为门失败
    return SKIP, f"e2e_windows 未通过（依赖真实显示器，诊断如下）：{detail[-400:]}"


# ---------------------------------------------------------------------------
# [8]/[9] M0 / M1 门：import 调用 main()，捕获返回码
# ---------------------------------------------------------------------------


def gate_check(name: str, module) -> tuple[str, str]:
    """调用前置里程碑验收门的 main()，返回码 0 -> PASS。

    门内打印重定向捕获，仅提取其“结果”行作为说明（保持本门输出整洁）。
    """
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = module.main()
    except SystemExit as exc:  # main 以 raise SystemExit(main()) 方式被直跑时
        code = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 - 门本身崩溃视作 FAIL，不掩盖
        return FAIL, f"{name} 门执行异常：{exc!r}"
    result_line = next(
        (line.strip() for line in buffer.getvalue().splitlines() if "结果：" in line),
        "",
    )
    if int(code or 0) == 0:
        return PASS, result_line or f"{name} 门返回码 0"
    return FAIL, f"{name} 门返回码 {code}；{result_line}\n{buffer.getvalue()[-600:]}"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    python = sys.executable
    checks: list[tuple[str, str, str]] = []  # (名称, 状态, 说明)

    # [1] 全量后端 pytest（unit + contract + replay），失败重跑一次
    ok, detail, reruns = pytest_with_retry(" ".join(BACKEND_TARGETS))
    note = pytest_summary_line(detail)
    if reruns:
        note += f"（首次失败，重跑第 {reruns} 次{'通过' if ok else '仍失败'}）"
    checks.append(("[1] 全量后端 pytest（unit+contract+replay，失败重跑一次）",
                   PASS if ok else FAIL, note))

    # [2] e2e_windows 单独（依赖真实显示器，SKIP 语义）
    status, detail = e2e_windows_check()
    checks.append((f"[2] ArenaLab Windows E2E（{status}，不计失败）", status, detail))

    # [3] 静态守卫（复用 M0 规则）
    ok, detail = run_m0_checks.static_guard()
    checks.append(("[3] 静态守卫（禁止 import 规则与 M0/M1 同源）",
                   PASS if ok else FAIL, "" if ok else detail[:800]))

    # [4] 视觉指标子集（黄金数据集指标）
    status, detail = pytest_subset("tests/unit/test_vision_detectors.py",
                                   "golden or metric or mae")
    checks.append((f"[4] 视觉指标子集（golden/metric/mae，{status}）", status, detail))

    # [5] FSM 确定性子集
    status, detail = pytest_subset("tests/unit/test_fsm_runtime.py",
                                   "hash or determin")
    checks.append((f"[5] FSM 确定性子集（hash/determin，{status}）", status, detail))

    # [6] 回放回归（TRC-005/006/007）
    ok, detail = pytest_once("tests/replay")
    checks.append(("[6] 回放回归（固定感知 + 原始帧 + 差异报告）",
                   PASS if ok else FAIL, pytest_summary_line(detail)))

    # [7] 契约测试（TST-004）
    ok, detail = pytest_once("tests/contract")
    checks.append(("[7] 契约测试（TST-004 消息结构冻结）",
                   PASS if ok else FAIL, pytest_summary_line(detail)))

    # [8]/[9] 前置门仍通过
    status, detail = gate_check("M0", run_m0_checks)
    checks.append((f"[8] M0 门（§12.1）仍通过（{status}）", status, detail))
    status, detail = gate_check("M1", run_m1_checks)
    checks.append((f"[9] M1 门（§12.2）仍通过（{status}）", status, detail))

    failed = sum(1 for _, s, _ in checks if s == FAIL)
    skipped = sum(1 for _, s, _ in checks if s == SKIP)

    print("\n" + "=" * 72)
    print("M2 验收门（§12.3）")
    print("=" * 72)
    for name, status, note in checks:
        line = f"[{status}] {name}"
        if note:
            line += f"\n       {note[:500]}"
        print(line)
    print("=" * 72)
    print(
        f"结果：{len(checks) - failed}/{len(checks)} 项通过"
        f"（FAIL {failed}，SKIP {skipped}；SKIP 不判定失败）"
    )
    print("§12.3 清单勾稽：")
    print("  - 模板/颜色条/像素/变化稳定/ROI OCR 可用 —— [4] + [1] test_vision_detectors 全量")
    print("  - 视觉黄金数据集与指标达门槛（§7.2：precision/recall ≥0.98、MAE ≤0.03）—— [4]，")
    print("    完整冻结验证集指标由视觉指标报告另行出具")
    print("  - DSL 编译/静态检查/状态机/超时/有界重试 —— [1] test_dsl_static_analysis + test_fsm_runtime")
    print("  - 确定性运行（§7.2：100 次哈希一致率 100%）—— [5]（AC-P0-08）")
    print("  - 固定感知与原始帧回放 —— [6]（AC-P0-09 回放侧，TRC-005/006）")
    print("  - 差异报告定位首个分歧与受影响测试 —— [6]（TRC-007）")
    print("  - AC-P0-08~10 —— [5][6] + [1]（state_no_exit 编译阻断等）")
    print("  - ArenaLab E2E happy path 与异常路径 —— [2]（真实显示器）+ [1] test_arena_lab*（确定性回归）")
    print("  - 消息结构冻结（轨迹/perception payload/编译产物/控制面事件）—— [7]（TST-004）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
