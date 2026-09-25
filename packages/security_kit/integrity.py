"""资产来源、哈希与篡改检测（SEC-004，TM-09）。

- :class:`AssetIntegrity` / 模块级同型函数：对项目 ``assets/`` 目录建立
  ``hashes.json`` 基线（路径 -> sha256 + size + mtime；其中 mtime 仅作
  诊断信息，**不参与篡改判定**——touch 不改变内容即不算被改）；
- :func:`verify_assets`：对照基线逐文件复核，报告缺失/被改/多余三类篡改；
- :func:`ensure_untouched`：发布前置检查，不通过抛 :class:`IntegrityError`
  ——是"模板被篡改的项目不能进入 RealInput"的资产侧依据（VER-002）；
- :func:`inspect_release_pack`：发布包（zip）预检：manifest 存在性/合法性、
  版本号重复、包内文件哈希与 manifest 声明一致（伪造 manifest / 重复版本
  号 / 篡改哈希三类恶意包的规则落点，TST-010）。

规则 ID：

========================  ==============================================
``no_baseline``           尚未建立 hashes.json 基线
``asset_missing``         基线中的资产在磁盘上缺失
``asset_modified``        资产内容（sha256/size）与基线不符
``asset_extra``           磁盘上出现基线之外的新资产
``manifest_missing``      发布包缺少 manifest.json
``manifest_invalid``      manifest.json 缺失/损坏/字段非法
``duplicate_version``     目标版本号已存在于 releases 目录
``hash_mismatch``         包内文件内容哈希与 manifest 声明不符
========================  ==============================================
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from security_kit.errors import IntegrityError
from security_kit.path_guard import (
    SEVERITY_ERROR,
    PathIssue,
    SizeLimits,
    check_entry_path,
)

__all__ = [
    "HASHES_FILENAME",
    "ASSETS_DIRNAME",
    "MANIFEST_FILENAME",
    "TamperReport",
    "AssetIntegrity",
    "build_hashes",
    "verify_assets",
    "ensure_untouched",
    "inspect_release_pack",
]

#: 基线文件名（位于项目目录根部）。
HASHES_FILENAME: str = "hashes.json"

#: 受保护资产子目录（与 control_plane/domain_model 布局一致）。
ASSETS_DIRNAME: str = "assets"

#: 发布包内 manifest 的约定文件名。
MANIFEST_FILENAME: str = "manifest.json"

#: 基线格式版本。
_BASELINE_SCHEMA_VERSION: int = 1

#: 合法 sha256 十六进制（64 位小写）。
_SHA256_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class TamperReport:
    """一条篡改/异常报告。"""

    #: 项目内相对路径（posix 风格）。
    path: str
    #: missing / modified / extra / no_baseline。
    status: str
    message: str
    #: 基线声明的 sha256（有则填）。
    expected_sha256: str | None = None
    #: 实际计算的 sha256（有则填）。
    actual_sha256: str | None = None


def _sha256_file(path: Path) -> str:
    """流式计算文件 sha256。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_asset_files(project_dir: Path) -> list[Path]:
    """列出 assets/ 下全部普通文件（相对路径 posix 排序，跳过符号链接）。"""
    assets_root = Path(project_dir) / ASSETS_DIRNAME
    if not assets_root.is_dir():
        return []
    out = [
        p
        for p in assets_root.rglob("*")
        if p.is_file() and not p.is_symlink()
    ]
    out.sort()
    return out


def _rel_posix(project_dir: Path, path: Path) -> str:
    """项目内相对路径，统一 posix 分隔符（基线键格式）。"""
    return path.relative_to(Path(project_dir)).as_posix()


# ----------------------------------------------------------------------
# 基线建立与校验
# ----------------------------------------------------------------------


def build_hashes(project_dir: str | Path) -> dict[str, Any]:
    """扫描项目 ``assets/`` 目录并写入 ``hashes.json`` 基线；返回基线内容。

    每个条目记录 ``sha256`` / ``size`` / ``mtime``；mtime 仅作诊断展示，
    **不参与**篡改判定（verify 只比较 sha256 与 size）。
    """
    project = Path(project_dir)
    entries: dict[str, dict[str, Any]] = {}
    for path in _iter_asset_files(project):
        stat = path.stat()
        entries[_rel_posix(project, path)] = {
            "sha256": _sha256_file(path),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }
    baseline = {
        "schema_version": _BASELINE_SCHEMA_VERSION,
        "algorithm": "sha256",
        "assets_root": ASSETS_DIRNAME,
        "entries": entries,
    }
    out = project / HASHES_FILENAME
    out.write_text(
        json.dumps(baseline, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return baseline


def _load_baseline(project_dir: Path) -> dict[str, Any] | None:
    """读取基线；缺失/损坏返回 None（由调用方决定报告口径）。"""
    path = Path(project_dir) / HASHES_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return None
    return data


def verify_assets(project_dir: str | Path) -> list[TamperReport]:
    """对照 ``hashes.json`` 基线复核资产，返回全部异常（空列表 = 未被篡改）。

    - 基线缺失 -> 单条 ``no_baseline`` 报告（调用方应先 build_hashes）；
    - 基线中存在但磁盘缺失 -> ``asset_missing``；
    - sha256 或 size 不一致 -> ``asset_modified``；
    - 磁盘上存在但基线没有 -> ``asset_extra``。
    """
    project = Path(project_dir)
    baseline = _load_baseline(project)
    if baseline is None:
        return [
            TamperReport(
                path=HASHES_FILENAME,
                status="no_baseline",
                message="缺少 hashes.json 基线（请先执行 build_hashes）",
            )
        ]
    entries: dict[str, Any] = baseline["entries"]
    reports: list[TamperReport] = []

    seen_on_disk: set[str] = set()
    for path in _iter_asset_files(project):
        rel = _rel_posix(project, path)
        seen_on_disk.add(rel)
        record = entries.get(rel)
        if record is None:
            reports.append(
                TamperReport(rel, "extra", "磁盘上存在基线之外的资产文件")
            )
            continue
        actual = _sha256_file(path)
        actual_size = path.stat().st_size
        if actual != record.get("sha256") or actual_size != record.get("size"):
            reports.append(
                TamperReport(
                    rel,
                    "modified",
                    "资产内容与基线不一致（sha256/size 不符）",
                    expected_sha256=str(record.get("sha256")),
                    actual_sha256=actual,
                )
            )

    for rel in sorted(entries):
        if rel in seen_on_disk:
            continue
        reports.append(
            TamperReport(
                rel, "missing", "基线中的资产文件缺失",
                expected_sha256=str(entries[rel].get("sha256")),
            )
        )
    reports.sort(key=lambda r: (r.status, r.path))
    return reports


def ensure_untouched(project_dir: str | Path) -> None:
    """发布/进入 RealInput 前的资产侧前置检查；任何异常即抛 :class:`IntegrityError`。"""
    reports = verify_assets(project_dir)
    if reports:
        summary = "; ".join(f"[{r.status}] {r.path}" for r in reports)
        raise IntegrityError(
            f"资产完整性校验不通过（{len(reports)} 项）：{summary}", reports
        )


class AssetIntegrity:
    """面向单个项目的完整性门面（build / verify / ensure_untouched）。"""

    def __init__(self, project_dir: str | Path) -> None:
        #: 项目目录。
        self.project_dir = Path(project_dir)

    def build(self) -> dict[str, Any]:
        """建立（或重建）基线。"""
        return build_hashes(self.project_dir)

    def verify(self) -> list[TamperReport]:
        """对照基线复核。"""
        return verify_assets(self.project_dir)

    def ensure_untouched(self) -> None:
        """前置检查：不通过抛 :class:`IntegrityError`。"""
        ensure_untouched(self.project_dir)


# ----------------------------------------------------------------------
# 发布包预检（伪造 manifest / 重复版本号 / 篡改哈希，TST-010）
# ----------------------------------------------------------------------


def inspect_release_pack(
    zip_path: str | Path,
    *,
    releases_dir: str | Path | None = None,
    limits: SizeLimits | None = None,
) -> list[PathIssue]:
    """对发布包 zip 做导入前安全预检，返回全部问题（空列表 = 可导入）。

    检查层次：
    1. 复用 SEC-002 全量预检（路径穿越/设备路径/超大/高压缩比/可执行载荷）；
    2. ``manifest.json`` 必须存在且为合法 JSON 对象（含字符串 version 与
       files 映射）——否则 ``manifest_missing`` / ``manifest_invalid``；
    3. ``files`` 声明的每个路径自身也要通过路径规则，且包内对应条目内容
       的 sha256 必须与声明一致——否则 ``hash_mismatch``；
    4. 提供 releases_dir 时，目标版本目录已存在 -> ``duplicate_version``。
    """
    limits = limits or SizeLimits()
    issues: list[PathIssue] = []

    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        return [PathIssue("corrupt_zip", str(zip_path), SEVERITY_ERROR, f"无法读取 Zip：{exc}")]

    with zf:
        infos = zf.infolist()

        # 1) SEC-002 全量路径/大小预检（内联复用条目规则，避免解包）
        from security_kit.path_guard import _aggregate_issues

        precheck_issues, _count = _aggregate_issues(infos, limits)
        issues.extend(precheck_issues)
        if any(i.severity == SEVERITY_ERROR for i in precheck_issues):
            return issues

        names = {info.filename: info for info in infos}
        manifest_info = names.get(MANIFEST_FILENAME)
        if manifest_info is None:
            issues.append(
                PathIssue("manifest_missing", MANIFEST_FILENAME, SEVERITY_ERROR,
                          "发布包缺少 manifest.json")
            )
            return issues

        # 2) manifest 合法性
        try:
            manifest = json.loads(zf.read(manifest_info).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            issues.append(
                PathIssue("manifest_invalid", MANIFEST_FILENAME, SEVERITY_ERROR,
                          f"manifest.json 不是合法 JSON：{exc}")
            )
            return issues
        if not isinstance(manifest, dict) or not isinstance(manifest.get("version"), str) \
                or not isinstance(manifest.get("files"), dict):
            issues.append(
                PathIssue("manifest_invalid", MANIFEST_FILENAME, SEVERITY_ERROR,
                          "manifest.json 必须是含 version(str) 与 files(对象) 的 JSON 对象")
            )
            return issues

        # 3) files 声明逐条复核（路径规则 + 内容哈希）
        files: dict[str, Any] = manifest["files"]
        for rel, declared in sorted(files.items()):
            path_issue = check_entry_path(rel)
            if path_issue is not None:
                issues.append(path_issue)
                continue
            if not isinstance(declared, str) or not _SHA256_RE.fullmatch(declared):
                issues.append(
                    PathIssue("manifest_invalid", rel, SEVERITY_ERROR,
                              "files 值必须是 64 位小写 sha256 十六进制")
                )
                continue
            member = names.get(rel)
            if member is None:
                issues.append(
                    PathIssue("hash_mismatch", rel, SEVERITY_ERROR,
                              "manifest 声明的文件不在包内")
                )
                continue
            if member.file_size > limits.max_file_bytes:
                continue  # 超限已由第 1 层报告，不再读入内存
            digest = hashlib.sha256(zf.read(member)).hexdigest()
            if digest != declared:
                issues.append(
                    PathIssue(
                        "hash_mismatch", rel, SEVERITY_ERROR,
                        f"包内内容 sha256={digest} 与 manifest 声明 {declared} 不符（疑似篡改）",
                    )
                )

        # 4) 版本号重复
        if releases_dir is not None:
            version_dir = Path(releases_dir) / str(manifest["version"])
            if version_dir.exists():
                issues.append(
                    PathIssue(
                        "duplicate_version", str(manifest["version"]), SEVERITY_ERROR,
                        f"版本 {manifest['version']} 已存在于 releases 目录（重复版本号/降级包）",
                    )
                )
    return issues
