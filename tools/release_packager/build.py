"""可重复构建（REL-001 基础版）：全量测试 -> 打包源码 -> SHA256SUMS -> 构建报告。

用法::

    python tools/release_packager/build.py --out dist/
        [--reproducible]    # 可重复构建模式（内部构建两次并自验 zip 字节一致）
        [--skip-tests]      # 跳过 pytest（快速打包；验收门已在 [1] 覆盖全量测试时用）
        [--source-root P]   # 源码根（默认仓库根）

产物（写入 --out 目录）：
  - ``visual-automation-workbench-<version>-<git短哈希>.zip``：源码 + schemas
    + examples + docs + tests + tools（排除 traces/node_modules/__pycache__/
    dist/.git/releases/backups 等）；
  - ``SHA256SUMS``：源码 zip 的 sha256（标准 ``<hash>  <name>`` 格式）；
  - ``build-report.json``：版本信息 + pytest 结果 + 产物哈希 + 可重复自检结果。

可重复构建口径（M3 验收门 [6] 注明）：
- zip **只含源码树**；``build-report.json``（含时间戳与 pytest 结果）与
  ``SHA256SUMS`` 落在 zip 外，不参与哈希；
- ``--reproducible`` 模式下：条目按 POSIX 路径排序、zip 时间戳固定为
  1980-01-01 00:00:00（或 STATIC_BUILD/SOURCE_DATE_EPOCH 注入值）、
  权限位固定 0644、创建系统固定——同一源码两次构建 zip 字节一致；
- 本脚本内部自验：连续构建两次并比较 sha256，不一致退出码 1。

退出码：0 成功（含自检通过）；1 测试失败或自检不一致。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: python -m 直跑同款搜索路径（ENG-002）
SUBPROCESS_ENV = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join(["packages", "services", "apps"]),
}

#: 进入 zip 的顶层目录（源码 + 资源 + 测试 + 工具 + 文档）
INCLUDE_DIRS: tuple[str, ...] = (
    "packages", "services", "apps", "schemas", "examples", "tests", "tools", "docs",
)
#: 进入 zip 的根文件
INCLUDE_FILES: tuple[str, ...] = ("pyproject.toml", "requirements.txt", "README.md")

#: 排除的目录名（任一层级命中即整枝跳过）
EXCLUDE_DIR_NAMES: frozenset[str] = frozenset(
    {"__pycache__", "node_modules", "dist", "traces", ".git", ".pytest_cache",
     ".github", "releases", "backups", "datasets", ".idea", ".vscode"}
)
#: 排除的文件扩展名
EXCLUDE_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo", ".log"})

#: 可重复模式的固定时间戳（zip 允许的最早时刻，避开 1970 时区负值）
REPRO_EPOCH = (1980, 1, 1, 0, 0, 0)

#: zip 成员统一属性：rw-r--r--，Unix 创建系统（固定值保证跨机器一致）
_MEMBER_ATTR = 0o644 << 16
_CREATE_SYSTEM = 3


# ---------------------------------------------------------------------------
# 版本信息
# ---------------------------------------------------------------------------


def git_short_hash(root: Path) -> str:
    """git commit 短哈希；git 不可用/非仓库时返回 ``unknown``（失败安全）。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.strip() or "unknown"


def fixed_epoch_from_env() -> tuple[int, ...] | None:
    """STATIC_BUILD / SOURCE_DATE_EPOCH 注入的固定时间戳（zip date_time 元组）。

    - ``STATIC_BUILD``：任意非空值即固定为可重复时间戳（``1`` 为推荐占位）；
    - ``SOURCE_DATE_EPOCH``：标准口径，Unix 秒。
    """
    static_build = os.environ.get("STATIC_BUILD", "").strip()
    if static_build:
        return REPRO_EPOCH
    sde = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if sde.isdigit():
        return time.gmtime(int(sde))[:6]
    return None


# ---------------------------------------------------------------------------
# 源码收集与 zip 构建
# ---------------------------------------------------------------------------


def collect_source_files(source_root: Path) -> dict[str, bytes]:
    """按固定顺序收集源码树为 {posix相对路径: 字节}（排序保证遍历确定）。"""
    files: dict[str, bytes] = {}

    def _walk(base: Path) -> None:
        for path in sorted(base.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(source_root)
            if EXCLUDE_DIR_NAMES & set(rel.parts):
                continue
            if path.suffix.lower() in EXCLUDE_SUFFIXES:
                continue
            files[rel.as_posix()] = path.read_bytes()

    for name in INCLUDE_DIRS:
        base = source_root / name
        if base.is_dir():
            _walk(base)
    for name in INCLUDE_FILES:
        path = source_root / name
        if path.is_file():
            files[name] = path.read_bytes()
    return dict(sorted(files.items()))


def build_zip_bytes(files: dict[str, bytes], date_time: tuple[int, ...]) -> bytes:
    """把源码树构建为 zip 字节（条目顺序、时间戳、权限位全部确定）。"""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(filename=name, date_time=date_time)
            info.external_attr = _MEMBER_ATTR
            info.create_system = _CREATE_SYSTEM
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[name])
    return buffer.getvalue()


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pytest_summary_line(detail: str) -> str:
    for line in reversed(detail.splitlines()):
        text = line.strip()
        if any(word in text for word in ("passed", "failed", "error", "no tests ran")):
            return text
    return detail.splitlines()[-1] if detail else ""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build.py", description="可重复构建（REL-001）：测试+打包+SHA256SUMS+报告",
    )
    parser.add_argument("--out", required=True, help="产物输出目录（如 dist/）")
    parser.add_argument("--reproducible", action="store_true",
                        help="可重复构建模式：固定时间戳/排序，内部构建两次自验一致")
    parser.add_argument("--skip-tests", action="store_true",
                        help="跳过全量 pytest（快速打包；验收门另行覆盖时使用）")
    parser.add_argument("--source-root", type=Path, default=ROOT, help="源码根（默认仓库根）")
    args = parser.parse_args(argv)

    source_root: Path = args.source_root.resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) 版本信息（git 短哈希若可用；时间戳可被 STATIC_BUILD 固定）
    commit = git_short_hash(source_root)
    injected = fixed_epoch_from_env()
    reproducible = args.reproducible
    zip_date_time = REPRO_EPOCH if (reproducible or injected) else time.gmtime()[:6]
    build_time = (
        datetime(*zip_date_time, tzinfo=timezone.utc).isoformat()
        if (reproducible or injected)
        else datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    version = {
        "name": "visual-automation-workbench",
        "version": "0.1.0",
        "git_short": commit,
        "build_time": build_time,
        "timestamp_fixed": bool(reproducible or injected),
        "python": sys.version.split()[0],
    }
    print(f"[build] 版本：{version['version']}  git:{commit}  时间:{build_time}")

    # 2) 全量 pytest（结果进 build-report.json；报告不进 zip）
    tests: dict[str, object]
    if args.skip_tests:
        tests = {"ran": False, "note": "--skip-tests：全量测试由调用方（如 M3 验收门 [1]）覆盖"}
        print("[build] pytest：跳过（--skip-tests）")
    else:
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "--no-header"],
            cwd=str(source_root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=SUBPROCESS_ENV, timeout=900,
        )
        duration = round(time.monotonic() - started, 1)
        tests = {
            "ran": True,
            "returncode": proc.returncode,
            "summary": _pytest_summary_line(proc.stdout + proc.stderr),
            "duration_s": duration,
        }
        print(f"[build] pytest：{tests['summary']}（{duration}s，rc={proc.returncode}）")
        if proc.returncode != 0:
            report = {"version": version, "tests": tests,
                      "artifacts": None, "reproducible": {"mode": reproducible}}
            (out_dir / "build-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("[build] FAIL：全量测试未通过，构建中止（详见 build-report.json）")
            return 1

    # 3) 打包源码 zip（可重复模式：内部构建两次自验字节一致）
    files = collect_source_files(source_root)
    pass1 = build_zip_bytes(files, zip_date_time)
    repro = {"mode": reproducible, "source_files": len(files)}
    if reproducible:
        pass2 = build_zip_bytes(files, zip_date_time)
        sha1, sha2 = sha256_of(pass1), sha256_of(pass2)
        repro.update({"self_check": "PASS" if sha1 == sha2 else "FAIL",
                      "pass1_sha256": sha1, "pass2_sha256": sha2})
        print(f"[build] 可重复自检：{repro['self_check']}  {sha1[:16]}…")
        if sha1 != sha2:
            (out_dir / "build-report.json").write_text(
                json.dumps({"version": version, "tests": tests,
                            "artifacts": None, "reproducible": repro},
                           ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("[build] FAIL：两次构建 zip 字节不一致", file=sys.stderr)
            return 1

    zip_name = f"visual-automation-workbench-{version['version']}-{commit}.zip"
    zip_path = out_dir / zip_name
    zip_path.write_bytes(pass1)
    zip_sha = sha256_of(pass1)
    (out_dir / "SHA256SUMS").write_text(f"{zip_sha}  {zip_name}\n", encoding="utf-8")
    print(f"[build] zip：{zip_path.name}（{len(pass1)} 字节，{len(files)} 文件）")
    print(f"[build] sha256：{zip_sha}")

    # 4) 构建报告（与 SHA256SUMS 一样在 zip 外，不参与可重复哈希）
    report = {
        "version": version,
        "tests": tests,
        "artifacts": {
            "zip": zip_name,
            "zip_sha256": zip_sha,
            "files_in_zip": len(files),
            "zip_bytes": len(pass1),
            "sha256sums": "SHA256SUMS",
        },
        "reproducible": repro,
        "repro_note": "zip 仅含源码树；build-report.json 与 SHA256SUMS 在 zip 外不参与哈希",
    }
    (out_dir / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[build] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
