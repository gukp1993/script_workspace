"""M3 验收门（对应《完整开发任务与测试验收方案》§12.4 M3 验收清单）。

用法：python tools/acceptance/run_m3_checks.py
失败检查项导致退出码 1。结构复用 run_m2_checks.py（run/gate_check 单一来源在
run_m0_checks.py 与 run_m2_checks.py）。

检查项映射：
  [1] 全量后端 pytest —— tests/unit + contract + replay + security
                      （ENG-002/TST-002/004/006/010）；失败重跑一次；
  [2] 静态守卫        —— 复用 run_m0_checks.static_guard（SEC-001/GOV-001）；
  [3] 前置门递归      —— M0（§12.1）+ M1（§12.2）+ M2（§12.3）main() 返回码 0
                      （M2 门内部含 e2e_windows 的 SKIP 语义）；
  [4] 发布链路冒烟    —— 内联脚本（AC-P0-12 最小复现）：tmp 复制
                      examples/arena_lab_demo -> publish（HMAC 签名信封校验，
                      错钥必败）-> 篡改一个受管文件 -> verify_directory 报告
                      非空（篡改被检出）-> rollback_to(v1) -> verify 通过
                      且文件内容还原（VER-002/006/008，TST-006 语义）；
  [5] 恶意包子集      —— pytest tests/security -q（TST-010，AC-P0-11）；
  [6] 可重复构建      —— tools/release_packager/build.py --reproducible
                      （--skip-tests：全量测试由 [1] 覆盖；可重复口径：
                      zip 只含源码树，pytest 报告/SHA256SUMS 在 zip 外不参与
                      哈希；构建内部两次构建自验 zip 字节一致，REL-001）；
  [7] 诊断包冒烟      —— tools/release_packager/diagnostics.py 生成 -> 包内
                      断言无图片扩展名成员（REL-004/SEC-007）；
  [8] 首启检查        —— tools/release_packager/first_run_check.py 通过
                      （REL-005）。

末尾对照 §12.4 清单逐行勾稽并打印；长稳（8h Shadow / 2h RealInput）与
签字页属人工/长时证据，由 TST-009 长稳报告与 regression_pack 产物另行出具。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# python -m 直跑需要与 pytest pythonpath 一致的搜索路径（ENG-002）
SUBPROCESS_ENV = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join(["packages", "services", "apps"]),
}

# 支持 in-process 调用 M0/M1/M2 门与静态守卫（复用既有实现，规则单一来源）
for _entry in ("packages", "services", "apps"):
    _path = str(ROOT / _entry)
    if _path not in sys.path:
        sys.path.insert(0, _path)
sys.path.insert(0, str(ROOT / "tools" / "acceptance"))

import run_m0_checks  # noqa: E402
import run_m1_checks  # noqa: E402
import run_m2_checks  # noqa: E402

run = run_m0_checks.run
PASS, FAIL, SKIP = run_m2_checks.PASS, run_m2_checks.FAIL, run_m2_checks.SKIP
pytest_summary_line = run_m2_checks.pytest_summary_line
pytest_with_retry = run_m2_checks.pytest_with_retry
gate_check = run_m2_checks.gate_check

#: 全量后端 pytest 的目标目录（e2e_windows 由 M2 门内 SKIP 语义处理）
BACKEND_TARGETS = ("tests/unit", "tests/contract", "tests/replay", "tests/security")

#: 诊断包内禁止出现的图片扩展名（与 security_kit.sanitize.IMAGE_EXTENSIONS 对齐）
IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".npy"}
)

#: [4] 发布链路冒烟的内联脚本（AC-P0-12 最小复现；argv: project releases_dir）
PUBLISH_SMOKE_SCRIPT = r'''
"""发布链路冒烟（M3 门 [4]）：publish(HMAC) -> 篡改 -> verify 报告 -> rollback -> verify 通过。"""
import json
import sys
from pathlib import Path

from common.clock import MonotonicClock
from release_kit.hashing import verify_directory
from release_kit.publisher import ReleasePublisher
from release_kit.rollback import ReleaseRollback
from release_kit.signing import HMACSigner, verify_envelope

project = Path(sys.argv[1])
releases = Path(sys.argv[2])
key = b"m3-gate-release-key"

# 1) 发布 v1（含 HMAC 签名信封；VER-005/006）
publisher = ReleasePublisher(project, releases, MonotonicClock())
record = publisher.publish(signer=HMACSigner(key))
manifest = publisher.load_release(record.release_id)

# 2) 签名信封校验：正确密钥通过、错误密钥必败
envelope = json.loads((record.directory / "manifest.sig").read_text(encoding="utf-8"))
manifest_bytes = (record.directory / "manifest.json").read_bytes()
assert envelope["algorithm"] == "hmac-sha256", envelope
assert verify_envelope(manifest_bytes, envelope, HMACSigner(key)), "正确密钥签名校验失败"
assert not verify_envelope(manifest_bytes, envelope, HMACSigner(b"wrong-key")), \
    "错误密钥不应通过签名校验"
print(f"publish/signature: OK (release={record.release_id}, files={len(manifest.files)})")

# 3) 篡改一个受管文件 -> 完整性校验必须报告问题（SEC-004）
target = project / "policies" / "default.yaml"
original = target.read_bytes()
target.write_bytes(original + b"\n# tampered-by-m3-gate\n")
problems = verify_directory(project, manifest.files, ignore_extra=True)
assert problems, "篡改未被完整性校验发现"
print(f"tamper/verify: OK (检出 {len(problems)} 处不一致)")

# 4) 整体回滚 v1 -> 回滚后 verify 必须通过（AC-P0-12；rollback_to 内含
#    发布目录完整性校验 + 自动备份 + 回滚后校验）
rollback = ReleaseRollback(project, releases, backups_dir=releases.parent / "backups")
result = rollback.rollback_to(record.release_id)
assert result.release_id == record.release_id
assert target.read_bytes() == original, "回滚后文件内容未还原"
remaining = verify_directory(project, manifest.files, ignore_extra=True)
assert not remaining, f"回滚后完整性校验未通过：{remaining}"
print(f"rollback/verify: OK (restored={result.files_restored} files, backup={result.backup_dir.name})")
print("M3 发布链路冒烟：全部断言通过")
'''


def publish_chain_smoke() -> tuple[str, str]:
    """[4] tmp 复制示例项目 -> 内联脚本跑发布/签名/篡改/回滚/校验链路。"""
    import shutil

    with tempfile.TemporaryDirectory(prefix="m3_publish_smoke_") as tmp:
        tmp_path = Path(tmp)
        project = tmp_path / "project"
        shutil.copytree(ROOT / "examples" / "arena_lab_demo", project)
        script_path = tmp_path / "publish_smoke.py"
        script_path.write_text(PUBLISH_SMOKE_SCRIPT, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(script_path), str(project), str(tmp_path / "releases")],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=SUBPROCESS_ENV, timeout=300,
        )
        tail = (proc.stdout + proc.stderr).strip().splitlines()
        note = "；".join(tail[-2:]) if tail else ""
        if proc.returncode == 0 and "全部断言通过" in proc.stdout:
            return PASS, note
        return FAIL, note or f"退出码 {proc.returncode}：{(proc.stdout + proc.stderr)[-400:]}"


def reproducible_build_check() -> tuple[str, str]:
    """[6] build.py --reproducible：内部两次构建 zip 字节一致（--skip-tests 口径）。"""
    with tempfile.TemporaryDirectory(prefix="m3_build_smoke_") as tmp:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "release_packager" / "build.py"),
             "--out", tmp, "--reproducible", "--skip-tests"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=SUBPROCESS_ENV, timeout=300,
        )
        if proc.returncode != 0:
            return FAIL, (proc.stdout + proc.stderr).strip()[-500:]
        report = json.loads((Path(tmp) / "build-report.json").read_text(encoding="utf-8"))
        repro = report.get("reproducible", {})
        if repro.get("self_check") != "PASS" or repro.get("pass1_sha256") != repro.get("pass2_sha256"):
            return FAIL, f"自检结果异常：{repro}"
        return PASS, (
            f"zip={report['artifacts']['files_in_zip']} 文件，"
            f"sha256={report['artifacts']['zip_sha256'][:16]}…；"
            "口径：pytest 报告/SHA256SUMS 在 zip 外（全量测试由 [1] 覆盖）"
        )


def diagnostics_smoke() -> tuple[str, str]:
    """[7] 诊断包生成 -> 包内断言无图片成员；原图（模板 png）被排除计数。"""
    with tempfile.TemporaryDirectory(prefix="m3_diag_smoke_") as tmp:
        out_zip = Path(tmp) / "diag.zip"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "release_packager" / "diagnostics.py"),
             "--project", str(ROOT / "examples" / "arena_lab_demo"), "--out", str(out_zip)],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=SUBPROCESS_ENV, timeout=120,
        )
        if proc.returncode != 0:
            return FAIL, (proc.stdout + proc.stderr).strip()[-400:]
        with zipfile.ZipFile(out_zip) as zf:
            names = zf.namelist()
            images = [n for n in names
                      if Path(n).suffix.lower() in IMAGE_SUFFIXES]
        if images:
            return FAIL, f"诊断包含图片成员（违反 REL-004）：{images}"
        excluded = "排除原图" in proc.stdout
        return PASS, f"成员 {len(names)} 个，无图片成员" + (
            "（原图已排除）" if excluded else "")


def first_run_check() -> tuple[str, str]:
    """[8] first_run_check.py 通过（REL-005）。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "release_packager" / "first_run_check.py")],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=SUBPROCESS_ENV, timeout=120,
    )
    tail = [l for l in (proc.stdout + proc.stderr).splitlines() if l.strip().startswith("结果")]
    if proc.returncode == 0:
        return PASS, tail[-1] if tail else "全部通过"
    return FAIL, (proc.stdout + proc.stderr).strip()[-500:]


def main() -> int:
    checks: list[tuple[str, str, str]] = []  # (名称, 状态, 说明)

    # [1] 全量后端 pytest（失败重跑一次）
    ok, detail, reruns = pytest_with_retry(" ".join(BACKEND_TARGETS))
    note = pytest_summary_line(detail)
    if reruns:
        note += f"（首次失败，重跑第 {reruns} 次{'通过' if ok else '仍失败'}）"
    checks.append(("[1] 全量后端 pytest（unit+contract+replay+security，失败重跑一次）",
                   PASS if ok else FAIL, note))

    # [2] 静态守卫（与 M0/M1/M2 同源规则）
    ok, detail = run_m0_checks.static_guard()
    checks.append(("[2] 静态守卫（禁止 import 规则与 M0~M2 同源）",
                   PASS if ok else FAIL, "" if ok else detail[:800]))

    # [3] 前置门递归（M0 -> M1 -> M2；聚合为一项，任一失败即 FAIL）
    gate_results: list[tuple[str, str, str]] = []
    for name, module in (("M0（§12.1）", run_m0_checks), ("M1（§12.2）", run_m1_checks),
                         ("M2（§12.3）", run_m2_checks)):
        status, detail = gate_check(name, module)
        gate_results.append((name, status, detail))
    gates_ok = all(status == PASS for _, status, _ in gate_results)
    gates_note = "；".join(f"{name} {status}" for name, status, _ in gate_results)
    gates_bad = "\n       ".join(
        f"{name} 门 FAIL：{detail}" for name, status, detail in gate_results if status != PASS
    )
    checks.append((
        "[3] M0/M1/M2 门递归通过（" + ("PASS" if gates_ok else "FAIL") + "）",
        PASS if gates_ok else FAIL,
        gates_note + (("；\n       " + gates_bad) if gates_bad else ""),
    ))

    # [4] 发布链路冒烟（AC-P0-12 最小复现）
    status, detail = publish_chain_smoke()
    checks.append((f"[4] 发布链路冒烟：publish(HMAC)->篡改->verify->rollback->verify（{status}）",
                   status, detail))

    # [5] 恶意包子集（TST-010 / AC-P0-11）
    ok, detail = run([sys.executable, "-m", "pytest", "tests/security", "-q", "--no-header"],
                     "恶意包子集")
    checks.append(("[5] 安全恶意包测试（tests/security）", PASS if ok else FAIL,
                   pytest_summary_line(detail)))

    # [6] 可重复构建（REL-001）
    status, detail = reproducible_build_check()
    checks.append((f"[6] 可重复构建：--reproducible 两次 zip 哈希一致（{status}）", status, detail))

    # [7] 诊断包冒烟（REL-004 / SEC-007）
    status, detail = diagnostics_smoke()
    checks.append((f"[7] 诊断包冒烟：生成并断言无 png（{status}）", status, detail))

    # [8] 首启检查（REL-005）
    status, detail = first_run_check()
    checks.append((f"[8] 首次启动检查（{status}）", status, detail))

    failed = sum(1 for _, s, _ in checks if s == FAIL)
    skipped = sum(1 for _, s, _ in checks if s == SKIP)

    print("\n" + "=" * 76)
    print("M3 验收门（§12.4）")
    print("=" * 76)
    for name, status, note in checks:
        line = f"[{status}] {name}"
        if note:
            line += f"\n       {note[:500]}"
        print(line)
    print("=" * 76)
    print(
        f"结果：{len(checks) - failed}/{len(checks)} 项通过"
        f"（FAIL {failed}，SKIP {skipped}；SKIP 不判定失败）"
    )
    print("§12.4 清单勾稽：")
    print("  - 资产库/标定/检测器/状态机/测试中心/时间轴工作流完整 —— [1]（test_domain_model/")
    print("    test_vision_detectors/test_fsm_runtime/test_trace_format）+ [3] M2 门；")
    print("    作者与操作文档随本里程碑交付（docs/authoring/*.md、docs/operations/*.md）")
    print("  - 发布包冻结依赖、签名、导入预检、版本比较和整体回滚 —— [4]（publish+HMAC 信封、")
    print("    篡改检出、rollback+verify）+ [1] test_release_kit（export/import 预检、快照 diff）")
    print("  - 隐私遮罩、留存、脱敏、诊断包可用 —— [7] + [1] test_security_kit（SEC-005/006/007）")
    print("  - 干净安装、升级、迁移失败回滚和卸载 —— [8] + [1]（upgrade_with_backup/AC-P0-14）；")
    print("    安装器（installer.iss）在发布机编译验收，本门覆盖文档面与环境面")
    print("  - 安全恶意包测试通过 —— [5]（AC-P0-11）")
    print("  - AC-P0-12 —— [4]（版本整体回滚最小复现）；AC-P0-13/14 —— [1]（test_capture 黑帧、")
    print("    test_release_kit 迁移恢复）")
    print("  - 8 小时 Shadow / 2 小时 ArenaLab RealInput 长稳、UAT 与签字页 —— 长时/人工证据，")
    print("    由 TST-009 长稳报告与 regression_pack 产物另行出具（自动化门不重复长稳）")
    print("  - 全量 P0/P1 用例达到发布闸门 —— [1] + [3] 递归门（M0~M2 全部仍通过）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
