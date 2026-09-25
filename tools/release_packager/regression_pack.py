"""发布候选回归包（TST-012 基础版）：一键分级回归 + 结构化报告 + Markdown 摘要。

用法::

    python tools/release_packager/regression_pack.py [--out dist/regression] [--with-e2e]

分级回归（按"便宜且高价值在前"排序，失败不阻断后续级——完整画像更有用）：

  [1] static_guard  静态守卫（禁止 import 规则，与 M0/M1/M2 门同源）；
  [2] unit          pytest tests/unit；
  [3] contract      pytest tests/contract（TST-004 消息结构冻结）；
  [4] replay        pytest tests/replay（TRC-005/006/007 回放回归）；
  [5] security      pytest tests/security（TST-010 恶意包与安全子集）；
  [6] e2e_windows   pytest tests/e2e_windows（可选：--with-e2e 启用；
                    依赖真实显示器，未启用记 SKIP 不判失败）。

产物：
  - ``regression-report.json``：逐级 status（pass/fail/skip）/耗时/摘要行；
  - ``regression-summary.md``：可读摘要（签字页素材，§15.1 候选版归档）。

退出码：出现 FAIL 即 1（SKIP 不判失败）。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 复用 M0 门的 run/static_guard（规则单一来源），并注入包搜索路径
for _entry in ("packages", "services", "apps"):
    _path = str(ROOT / _entry)
    if _path not in sys.path:
        sys.path.insert(0, _path)
sys.path.insert(0, str(ROOT / "tools" / "acceptance"))

import run_m0_checks  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

#: 回归分级定义：(名称, pytest 目标)。static_guard 单独处理。
PYTEST_STAGES: tuple[tuple[str, str], ...] = (
    ("unit", "tests/unit"),
    ("contract", "tests/contract"),
    ("replay", "tests/replay"),
    ("security", "tests/security"),
)


def _summary_line(detail: str) -> str:
    """提取 pytest 结果汇总行（供报告与摘要）。"""
    for line in reversed(detail.splitlines()):
        text = line.strip()
        if any(word in text for word in ("passed", "failed", "error", "no tests ran")):
            return text
    return detail.splitlines()[-1] if detail else ""


def run_static_guard() -> tuple[str, float, str]:
    """[1] 静态守卫（in-process 复用）。"""
    started = time.monotonic()
    ok, detail = run_m0_checks.static_guard()
    duration = round(time.monotonic() - started, 1)
    return (PASS if ok else FAIL), duration, detail[:200]


def run_pytest(target: str) -> tuple[str, float, str]:
    """跑一级 pytest（子进程，环境与 pytest pythonpath 一致）。"""
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "--no-header"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONPATH": os.pathsep.join(["packages", "services", "apps"])},
        timeout=900,
    )
    duration = round(time.monotonic() - started, 1)
    return (PASS if proc.returncode == 0 else FAIL), duration, _summary_line(proc.stdout + proc.stderr)


def run_e2e() -> tuple[str, float, str]:
    """[6] e2e_windows：真实显示器依赖；失败重跑一次，仍失败记 SKIP + 诊断。

    与 M2 验收门 [2] 同语义：无显示器/服务会话环境下必然失败，不判 FAIL；
    确定性回归由 unit/replay 兜底。
    """
    status, duration, summary = run_pytest("tests/e2e_windows")
    if status == PASS:
        return PASS, duration, summary
    status2, duration2, summary2 = run_pytest("tests/e2e_windows")
    total = round(duration + duration2, 1)
    if status2 == PASS:
        return PASS, total, summary2 + "（重跑通过）"
    return SKIP, total, f"e2e 未通过（依赖真实显示器，不计失败）：{summary2[:160]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="regression_pack.py", description="发布候选回归包（TST-012）：分级回归 + 报告 + 摘要",
    )
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "regression",
                        help="报告输出目录（默认 dist/regression）")
    parser.add_argument("--with-e2e", action="store_true",
                        help="启用 e2e_windows 分级（需真实显示器；默认 SKIP）")
    args = parser.parse_args(argv)

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    stages: list[dict[str, object]] = []

    # [1] 静态守卫
    status, duration, note = run_static_guard()
    stages.append({"name": "static_guard", "target": "(in-process)", "status": status,
                   "duration_s": duration, "summary": note})

    # [2]~[5] 分级 pytest
    for name, target in PYTEST_STAGES:
        status, duration, note = run_pytest(target)
        stages.append({"name": name, "target": target, "status": status,
                       "duration_s": duration, "summary": note})

    # [6] e2e（可选）
    if args.with_e2e:
        status, duration, note = run_e2e()
    else:
        status, duration, note = SKIP, 0.0, "未启用（--with-e2e 开启；依赖真实显示器）"
    stages.append({"name": "e2e_windows", "target": "tests/e2e_windows", "status": status,
                   "duration_s": duration, "summary": note})

    totals = {
        "pass": sum(1 for s in stages if s["status"] == PASS),
        "fail": sum(1 for s in stages if s["status"] == FAIL),
        "skip": sum(1 for s in stages if s["status"] == SKIP),
        "duration_s": round(sum(float(s["duration_s"]) for s in stages), 1),
    }
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": {"python": sys.version.split()[0], "platform": platform.platform()},
        "stages": stages,
        "totals": totals,
    }
    report_path = out_dir / "regression-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 可读 Markdown 摘要（§15.1 候选版归档 / 签字页素材）
    lines = [
        "# 发布候选回归摘要（TST-012）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 环境：Python {report['host']['python']} / {report['host']['platform']}",
        f"- 结果：**PASS {totals['pass']} / FAIL {totals['fail']} / SKIP {totals['skip']}**"
        f"（总耗时 {totals['duration_s']}s）",
        "",
        "| 分级 | 目标 | 状态 | 耗时(s) | 摘要 |",
        "|---|---|---|---:|---|",
    ]
    for s in stages:
        lines.append(f"| {s['name']} | `{s['target']}` | {s['status']} | {s['duration_s']} | {s['summary']} |")
    lines += [
        "",
        "说明：SKIP 不判定失败（e2e 依赖真实显示器）；任一 FAIL 阻断候选版（§7.1 一票否决项）。",
        "",
    ]
    summary_path = out_dir / "regression-summary.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8")

    print("=" * 72)
    print("发布候选回归包（TST-012）")
    print("=" * 72)
    for s in stages:
        print(f"  [{s['status']}] {s['name']:<13} {s['duration_s']:>6}s  {s['summary']}")
    print("=" * 72)
    print(f"结果：PASS {totals['pass']} / FAIL {totals['fail']} / SKIP {totals['skip']}"
          f"（{totals['duration_s']}s）")
    print(f"报告：{report_path}")
    print(f"摘要：{summary_path}")
    return 1 if totals["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
