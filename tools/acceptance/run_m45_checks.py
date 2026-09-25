"""M4/M5 验收门（对应任务文档 §3 里程碑表 M4/M5 退出条件）。

用法：python tools/acceptance/run_m45_checks.py

检查项：
  [1] 全量单测（含 extension_runner / target_templates）
  [2] 静态守卫（run_m0_checks.static_guard——扩展子进程也不得携带真实输入能力）
  [3] M3 门递归通过（publish/rollback/安全/构建链）
  [4] 目标模板：desktop-template 校验通过；shadow 模板校验通过；
      shadow 篡改 real_input 被拒绝（protected_online_no_real_input）
  [5] 扩展 Runner 子集：能力默认拒绝/超时 kill/崩溃隔离用例通过
  [6] 准入清单存在性：ADP-003 五项勾选表
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_m0_checks import SUBPROCESS_ENV, ROOT, run, static_guard  # noqa: E402


def main() -> int:
    python = sys.executable
    checks: list[tuple[str, bool, str]] = []

    ok, detail = run([python, "-m", "pytest", "tests", "-q", "--no-header"], "[1] 全量单测")
    last = [ln for ln in detail.splitlines() if "passed" in ln or "failed" in ln]
    checks.append(("全量 pytest", ok, last[-1] if last else ""))

    ok, detail = static_guard()
    checks.append(("静态守卫：无越权 import", ok, detail.splitlines()[0] if not ok else ""))

    from run_m3_checks import main as m3_main  # noqa: PLC0415

    rc = m3_main()
    checks.append(("M3 门递归（含 M0/M1/M2）", rc == 0, f"exit={rc}"))

    ok, detail = run(
        [python, "-m", "domain_model.validate", "examples/desktop-target-template"],
        "[4a] desktop 模板校验",
    )
    checks.append(("desktop-target-template VALID", ok, detail.splitlines()[-1]))

    ok, detail = run(
        [python, "-m", "domain_model.validate", "examples/protected-online-shadow-template"],
        "[4b] shadow 模板校验",
    )
    checks.append(("protected-online-shadow-template VALID", ok, detail.splitlines()[-1]))

    # shadow 模板篡改 real_input 必须被拒：在临时目录复制并篡改后校验
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "tampered"
        shutil.copytree(ROOT / "examples/protected-online-shadow-template", dst)
        policy_file = dst / "policies" / "default.yaml"
        text = policy_file.read_text(encoding="utf-8").replace("mode: shadow", "mode: real_input")
        policy_file.write_text(text, encoding="utf-8")
        ok, detail = run(
            [python, "-m", "domain_model.validate", str(dst)],
            "[4c] shadow 篡改 real_input 拒绝",
            expect_zero=False,
        )
        checks.append(
            (
                "shadow 篡改 real_input 被拒绝（protected_online_no_real_input）",
                ok and "protected_online_no_real_input" in detail,
                "",
            )
        )

    ok, detail = run(
        [python, "-m", "pytest", "tests/unit/test_extension_runner.py", "-q", "--no-header"],
        "[5] 扩展 Runner 子集",
    )
    checks.append(("extension_runner 用例通过", ok, detail.splitlines()[-1] if detail else ""))

    checklist = ROOT / "examples/desktop-target-template/README.md"
    text = checklist.read_text(encoding="utf-8") if checklist.exists() else ""
    items = ["授权依据", "风险等级", "可测试环境", "回滚", "数据处理"]
    ok = all(i in text for i in items)
    checks.append(("[6] ADP-003 准入清单五项齐全", ok, "" if ok else "缺少：" + str([i for i in items if i not in text])))

    print("\n" + "=" * 64)
    print("M4/M5 验收门")
    print("=" * 64)
    failed = 0
    for name, passed, note in checks:
        mark = "PASS" if passed else "FAIL"
        failed += 0 if passed else 1
        line = f"[{mark}] {name}"
        if note and not passed:
            line += f"\n       {note[:600]}"
        print(line)
    print("=" * 64)
    print(f"结果：{len(checks) - failed}/{len(checks)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
