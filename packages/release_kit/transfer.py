"""发布包导出/导入与安全预检（VER-007，SEC-002 基础，AC-P0-11）。

导出：把发布目录（manifest + assets + 对象）打包为 zip，默认不含
traces 与原始帧（冻结规则本就排除运行产物）。

导入前安全预检（两遍扫描，全部通过才解压到隔离目录）：
- ``path_escape``：绝对路径 / ``..`` 穿越 / 盘符 / 符号链接逃逸 -> 拒绝；
- ``file_too_large``：单文件超限（默认 100 MB）；
- ``total_size_exceeded``：解压总量超限（默认 500 MB，按压缩前大小累计）；
- ``compression_ratio_abuse``：压缩比 > 200:1（zip bomb 特征）；
- ``executable_payload``：可执行载荷扩展名黑名单（exe/dll/bat/ps1/...）；
- ``manifest_invalid``：清单缺失 / Schema 不符 / 登记哈希不符 / 未登记文件；
- ``signature_invalid`` / ``unsigned_package``：签名无效或未知签名
  （``allow_unsigned=False`` 时拒绝；默认允许导入但标注"禁止启用 real_input"，
  完整签名校验见 :mod:`release_kit.signing`，VER-006）。

安全约定：预检阶段只读 zip 中央目录与成员字节，**不落盘**；
解压阶段再次校验目标路径包含关系（防御纵深）。安全事件通过
:attr:`ImportReport.rejected` 结构化返回并写入日志。
"""

from __future__ import annotations

import json
import logging
import re
import stat as stat_module
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from release_kit.errors import ReleaseError
from release_kit.hashing import content_hash, verify_directory
from release_kit.manifest import ReleaseManifest
from release_kit.signing import Signer, verify_envelope

LOGGER = logging.getLogger("release_kit.transfer")

#: 可执行载荷黑名单扩展名（AC-P0-11：导入包不得携带可执行代码）
EXECUTABLE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".exe", ".dll", ".com", ".scr", ".msi", ".msp", ".mst",
        ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse",
        ".wsf", ".wsh", ".hta", ".jar", ".sh", ".bash", ".py", ".pyw",
        ".pl", ".php", ".rb", ".lua",
    }
)

#: 单文件大小上限（默认 100 MB）
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
#: 解压总量上限（默认 500 MB，按压缩前大小累计）
DEFAULT_MAX_TOTAL_BYTES = 500 * 1024 * 1024
#: 压缩比上限（zip bomb 特征）
DEFAULT_MAX_RATIO = 200
#: 压缩比检查的文件大小下限（过小文件的比值无统计意义）
_RATIO_MIN_FILE_BYTES = 4096
#: 发布包根目录允许存在的"清单外"文件
_ALLOWED_ROOT_EXTRAS = frozenset({"manifest.json", "manifest.sig"})

#: 导入隔离目录名（位于 projects_root 下）
IMPORTS_DIRNAME = "imports"


@dataclass(frozen=True)
class Rejection:
    """一条导入拒绝记录：规则 ID + 涉及条目 + 说明。"""

    rule_id: str
    entry: str
    detail: str = ""

    def __str__(self) -> str:  # 便于日志与 UI 展示
        return f"[{self.rule_id}] {self.entry}: {self.detail}"


@dataclass
class ImportReport:
    """导入结果报告。

    Attributes:
        ok:          是否成功导入（存在任何拒绝即 False）。
        release_id:  成功导入时的发布 ID。
        import_dir:  成功导入时的隔离目录。
        rejected:    全部拒绝记录（预检收集所有问题而非首个即停）。
        warnings:    非阻断警告（如 unsigned_package）。
    """

    ok: bool = True
    release_id: str | None = None
    import_dir: Path | None = None
    rejected: list[Rejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rule_ids(self) -> set[str]:
        """全部命中的规则 ID 集合（断言与安全审计用）。"""
        return {item.rule_id for item in self.rejected}


def export_release(release_id: str, out_zip: str | Path, releases_dir: str | Path) -> Path:
    """把发布目录打包为 zip（manifest + assets + 对象；不含 traces/原始帧）。

    Args:
        release_id:   发布 ID（须已存在于 releases_dir）。
        out_zip:      输出 zip 路径。
        releases_dir: 发布根目录。
    """
    release_dir = Path(releases_dir) / release_id
    manifest_path = release_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ReleaseError(f"发布不存在或缺少清单：{release_id}")
    manifest = ReleaseManifest.load(manifest_path)
    entries = sorted(set(manifest.files) | {"manifest.json", "manifest.sig"})
    output = Path(out_zip)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel in entries:
            source = release_dir / rel
            if not source.is_file():
                raise ReleaseError(f"发布目录缺少文件，导出中止：{release_id}/{rel}")
            archive.write(source, arcname=rel)
    return output


def _entry_path_issue(name: str, info: zipfile.ZipInfo) -> str | None:
    """条目路径安全检查（AC-P0-11）：返回拒绝原因，None 表示通过。

    覆盖：绝对路径、``..`` 穿越、Windows 盘符、符号链接逃逸。
    """
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return "绝对路径条目"
    if re.match(r"^[A-Za-z]:", normalized):
        return "包含盘符的绝对路径条目"
    segments = [seg for seg in normalized.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in segments):
        return "包含 .. 的路径穿越条目"
    # 符号链接逃逸：Unix 模式位的文件类型为 S_IFLNK
    if stat_module.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
        return "符号链接条目"
    return None


def _extract_safely(archive: zipfile.ZipFile, dest_root: Path) -> None:
    """手动解压：逐条目再做一次路径包含校验（防御纵深），不使用 extractall。"""
    for info in archive.infolist():
        if info.is_dir():
            continue
        segments = [seg for seg in info.filename.replace("\\", "/").split("/") if seg not in ("", ".")]
        if not segments or any(seg == ".." for seg in segments) or re.match(r"^[A-Za-z]:", info.filename):
            continue  # 预检已拒绝；此处兜底跳过
        target = dest_root.joinpath(*segments)
        resolved_root = dest_root.resolve()
        if not str(target.resolve()).startswith(str(resolved_root)):
            continue  # 包含关系兜底校验失败则跳过
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(info))


def import_release(
    zip_path: str | Path,
    projects_root: str | Path,
    *,
    allow_unsigned: bool = True,
    signer: Signer | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_ratio: int = DEFAULT_MAX_RATIO,
) -> ImportReport:
    """导入发布包：安全预检全部通过才解压到隔离目录（AC-P0-11）。

    Args:
        zip_path:        发布包 zip 路径。
        projects_root:   项目库根目录；解压目标为
                         ``<projects_root>/imports/<release_id>-<短哈希>/``。
        allow_unsigned:  是否允许未签名/未知签名包（默认 True，导入后
                         标注警告"禁止启用 real_input"；False 时拒绝）。
        signer:          签名验证器（VER-006）；提供时校验 manifest.sig。
        max_file_bytes:  单文件大小上限。
        max_total_bytes: 解压总量上限（压缩前大小累计）。
        max_ratio:       压缩比上限。

    Returns:
        :class:`ImportReport`——任何拒绝都会给出规则 ID 与条目名，
        且**不产生任何文件写入**。
    """
    projects_root = Path(projects_root)
    report = ImportReport()

    # 0) 损坏包安全失败（不抛裸异常，不污染现有项目）
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        rejection = Rejection("corrupt_archive", str(zip_path), f"无法作为 zip 打开：{exc}")
        report.ok = False
        report.rejected.append(rejection)
        LOGGER.warning("导入被拒绝：%s", rejection)
        return report

    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = {info.filename for info in infos}

        # 1) 第一遍：条目级安全检查（路径 / 载荷 / 大小 / 压缩比）
        total_bytes = 0
        for info in infos:
            entry = info.filename
            path_issue = _entry_path_issue(entry, info)
            if path_issue is not None:
                report.rejected.append(Rejection("path_escape", entry, path_issue))
                continue
            suffix = PurePosixPath(entry.replace("\\", "/")).suffix.lower()
            if suffix in EXECUTABLE_SUFFIXES:
                report.rejected.append(Rejection("executable_payload", entry, f"禁止的载荷扩展名 {suffix}"))
                continue
            if info.file_size > max_file_bytes:
                report.rejected.append(
                    Rejection("file_too_large", entry, f"解压前 {info.file_size} 字节超过上限 {max_file_bytes}")
                )
                continue
            if info.file_size >= _RATIO_MIN_FILE_BYTES:
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > max_ratio:
                    report.rejected.append(
                        Rejection(
                            "compression_ratio_abuse",
                            entry,
                            f"压缩比 {ratio:.0f}:1 超过上限 {max_ratio}:1（zip bomb 特征）",
                        )
                    )
                    continue
            total_bytes += info.file_size
        if total_bytes > max_total_bytes:
            report.rejected.append(
                Rejection(
                    "total_size_exceeded",
                    "(archive)",
                    f"解压总量 {total_bytes} 字节超过上限 {max_total_bytes}",
                )
            )

        # 2) 第二遍：manifest 校验（缺失 / Schema 不符 / 哈希不符 / 未登记文件）
        manifest: ReleaseManifest | None = None
        if "manifest.json" not in names:
            report.rejected.append(Rejection("manifest_invalid", "manifest.json", "发布包缺少 manifest.json"))
        else:
            try:
                manifest = _manifest_from_bytes(archive.read("manifest.json"))
            except ReleaseError as exc:
                report.rejected.append(Rejection("manifest_invalid", "manifest.json", str(exc)))
                manifest = None
        if manifest is not None:
            for rel, expected_sha in sorted(manifest.files.items()):
                if rel not in names:
                    report.rejected.append(Rejection("manifest_invalid", rel, "清单登记的文件在包中缺失"))
                    continue
                if content_hash(archive.read(rel)) != expected_sha:
                    report.rejected.append(Rejection("manifest_invalid", rel, "文件哈希与清单登记值不一致"))
            extras = names - set(manifest.files) - _ALLOWED_ROOT_EXTRAS
            for extra in sorted(extras):
                report.rejected.append(Rejection("manifest_invalid", extra, "包中存在清单未登记的文件"))

            # 3) 签名校验（VER-006）：无效签名硬拒绝；未知/缺失签名按策略处理
            _check_signature(archive, names, manifest, report, signer=signer, allow_unsigned=allow_unsigned)

        # 4) 存在拒绝 -> 不产生任何文件写入（AC-P0-11）
        if report.rejected:
            report.ok = False
            for rejection in report.rejected:
                LOGGER.warning("导入被拒绝：%s", rejection)
            return report

        # 5) 全部通过 -> 解压到隔离目录
        assert manifest is not None
        import_dir = projects_root / IMPORTS_DIRNAME / f"{manifest.release_id}-{manifest.content_sha[:8]}"
        import_dir.mkdir(parents=True, exist_ok=True)
        _extract_safely(archive, import_dir)
        problems = verify_directory(import_dir, manifest.files, ignore_extra=True)
        if problems:  # 防御纵深：解压后复核，失败则视为导入失败
            report.ok = False
            report.rejected.append(Rejection("manifest_invalid", "(extracted)", "; ".join(problems)))
            return report
        report.ok = True
        report.release_id = manifest.release_id
        report.import_dir = import_dir
        return report


def _manifest_from_bytes(payload: bytes) -> ReleaseManifest:
    """从字节解析清单（供导入预检使用，解析失败抛 ReleaseError）。"""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"manifest.json 无法解析：{exc}") from exc
    return ReleaseManifest.from_dict(data)


def _check_signature(
    archive: zipfile.ZipFile,
    names: set[str],
    manifest: ReleaseManifest,
    report: ImportReport,
    *,
    signer: Signer | None,
    allow_unsigned: bool,
) -> None:
    """签名校验（VER-006）：

    - 提供验证器且算法匹配：签名无效 -> ``signature_invalid`` 硬拒绝；
    - 缺失/算法未知/Null 签名：视为未签名——``allow_unsigned=True`` 时
      导入并记警告（导入后禁止启用 real_input）；False 时
      ``unsigned_package`` 拒绝。
    """
    manifest_bytes = archive.read("manifest.json")
    envelope: dict[str, str] | None = None
    if "manifest.sig" in names:
        try:
            parsed = json.loads(archive.read("manifest.sig").decode("utf-8"))
            envelope = dict(parsed) if isinstance(parsed, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            envelope = None

    if signer is not None and envelope is not None and envelope.get("algorithm") == signer.algorithm:
        if not verify_envelope(manifest_bytes, envelope, signer):
            report.rejected.append(
                Rejection("signature_invalid", "manifest.sig", "签名与内容不符，包可能被篡改")
            )
        return
    # 未签名 / 未知算法 / 无验证器
    if allow_unsigned:
        report.warnings.append(
            "unsigned_package: 包未签名或签名算法未知（未启用真实输入；导入后不得启用 real_input 模式）"
        )
    else:
        report.rejected.append(
            Rejection("unsigned_package", "manifest.sig", "包未签名或签名算法未知，且本次导入不允许未签名包")
        )
