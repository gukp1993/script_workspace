"""安全解包与路径边界（SEC-002 完整版，TST-010 的规则落点）。

规则 ID（与 M3 release_kit 的 transfer 预检约定保持一致）：

======================  =======================================================
规则 ID                 含义
======================  =======================================================
``absolute_path``       盘符（``C:/…``）/ UNC（``\\\\server\\share``）/ POSIX 绝对路径
``parent_escape``       任意层级的 ``..`` 路径段（Zip Slip）
``device_path``         设备路径与保留设备名（``\\\\?\\``、CON/NUL/COM1…）
``symlink_escape``      解析（follow symlink/junction）后越出根目录
``path_too_long``       超长路径（>260 警告级，>4096 错误级）
``illegal_path``        空路径、非法字符（``<>:"|?*``、控制符、结尾点/空格）
``file_too_large``      单文件解压后大小超上限
``total_size_exceeded`` 累计解压大小超上限
``compression_ratio_abuse`` 压缩比超上限（zip bomb）
``executable_payload``  黑名单扩展名（exe/dll/bat/ps1/py… 可执行载荷）
``too_many_entries``    条目数超上限（重复文件资源耗尽）
``encrypted_entry``     加密条目（无法安全预检内容）
``corrupt_zip``         损坏 Zip（BadZipFile）
======================  =======================================================

设计要点：

- :func:`check_entry_path` 是纯字符串规则（不触碰文件系统）；
- :func:`safe_resolve` 在其上追加"resolve 后必须仍在 root 内"的符号链接
  逃逸检查，是所有落盘路径的唯一出口；
- :func:`safe_extract_zip` **先全量预检、后落盘**：任何条目命中错误级规则
  即整包拒绝，解压目录一个字节都不写（先解压到 dest 内的暂存目录，
  全部成功后再原子化地搬入 dest，防 ZIP 元数据谎报大小）。
"""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from security_kit.errors import PathGuardError

__all__ = [
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "EXECUTABLE_EXTENSIONS",
    "MAX_PATH_WIN",
    "PathIssue",
    "SizeLimits",
    "ExtractReport",
    "check_entry_path",
    "safe_resolve",
    "safe_extract_zip",
    "precheck_zip",
]

#: 问题级别：错误（阻断）/ 警告（记录不阻断）。
SEVERITY_ERROR: str = "error"
SEVERITY_WARNING: str = "warning"

#: Windows MAX_PATH 限制（超过即产生兼容性/截断风险）。
MAX_PATH_WIN: int = 260

#: 绝对不可接受的上限（超过视为错误而非警告）。
_MAX_PATH_HARD: int = 4096

#: Windows 保留设备名（任意扩展名形式如 ``CON.txt`` 同样命中）。
_RESERVED_DEVICE_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: Windows 非法路径字符（文件名与目录名均不允许）。
_ILLEGAL_CHARS: str = '<>:"|?*'

#: 可执行载荷黑名单扩展名（SEC-004 用例：含可执行文件/脚本的项目包默认拒绝）。
EXECUTABLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe", ".dll", ".com", ".scr", ".msi", ".sys",
        ".bat", ".cmd", ".ps1", ".psm1",
        ".py", ".pyw", ".pyc",
        ".js", ".jse", ".vbs", ".vbe", ".wsf", ".wsh", ".hta",
        ".lnk", ".jar",
    }
)

#: 暂存目录名（位于 dest 内；同名条目一律拒绝，防覆盖/混淆）。
STAGING_DIRNAME: str = ".security_kit_staging"

#: 盘符前缀：``C:``（后跟分隔符或直接跟内容均为可疑，一律按绝对路径拒绝）。
_DRIVE_RE: re.Pattern[str] = re.compile(r"^[A-Za-z]:")

#: 设备路径前缀：``\\?\`` / ``\\.\``（含 ``\\?\UNC\``）。
_DEVICE_PREFIXES: tuple[str, ...] = ("\\\\?\\", "\\\\.\\", "//?/", "//./")

#: 单段中的保留设备名（取第一个点之前的主干比较）。
_DEVICE_SEG_RE: re.Pattern[str] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)

#: 复制流式缓冲大小。
_COPY_CHUNK: int = 64 * 1024


@dataclass(frozen=True, slots=True)
class PathIssue:
    """一条路径/包体检出的问题。

    Attributes:
        rule_id:  规则 ID（见模块 docstring 的规则表）。
        path:     命中规则的条目名（原始字符串）。
        severity: ``"error"``（阻断）或 ``"warning"``（仅记录）。
        message:  人类可读原因。
    """

    rule_id: str
    path: str
    severity: str
    message: str


@dataclass(frozen=True, slots=True)
class SizeLimits:
    """解包/导入的大小与数量限制（SEC-005 用例：超限安全终止）。"""

    #: 单文件解压后大小上限（字节）。
    max_file_bytes: int = 100 * 1024 * 1024
    #: 全包累计解压大小上限（字节）。
    max_total_bytes: int = 512 * 1024 * 1024
    #: 单条目压缩比上限（解压后/压缩后）；zip bomb 主判据。
    max_compression_ratio: float = 200.0
    #: 条目数上限（防"海量重复文件"拖垮导入）。
    max_entries: int = 20_000
    #: 黑名单扩展名（可执行载荷）。
    blocked_extensions: frozenset[str] = field(default_factory=lambda: EXECUTABLE_EXTENSIONS)


@dataclass(slots=True)
class ExtractReport:
    """safe_extract_zip / precheck_zip 的审计报告。"""

    #: 是否放行（无错误级问题且成功落盘；precheck 场景下=无错误级问题）。
    ok: bool
    #: 实际落盘的条目名（被拒绝时恒为空列表）。
    extracted: list[str] = field(default_factory=list)
    #: 全部检出问题（含警告级）。
    issues: list[PathIssue] = field(default_factory=list)
    #: 预检的条目总数。
    entries_checked: int = 0
    #: 实际写入字节数（被拒绝时为 0）。
    bytes_written: int = 0

    @property
    def error_rules(self) -> list[str]:
        """去重后的错误级规则 ID 列表（断言用）。"""
        seen: list[str] = []
        for issue in self.issues:
            if issue.severity == SEVERITY_ERROR and issue.rule_id not in seen:
                seen.append(issue.rule_id)
        return seen

    @property
    def warning_rules(self) -> list[str]:
        """去重后的警告级规则 ID 列表。"""
        seen: list[str] = []
        for issue in self.issues:
            if issue.severity == SEVERITY_WARNING and issue.rule_id not in seen:
                seen.append(issue.rule_id)
        return seen


# ----------------------------------------------------------------------
# 字符串级路径规则（不触碰文件系统）
# ----------------------------------------------------------------------


def check_entry_path(name: str) -> PathIssue | None:
    """校验压缩包条目名/相对路径字符串；返回首个命中的问题，安全返回 None。

    检查优先级：空/非法字符 -> 盘符绝对路径 -> 设备路径前缀 -> UNC/POSIX
    绝对路径 -> ``..`` 段 -> 保留设备名段 -> 非法字符段 -> 超长。
    """
    if not isinstance(name, str) or not name.strip():
        return PathIssue("illegal_path", str(name), SEVERITY_ERROR, "路径为空或全空白")
    if "\x00" in name or any(ord(c) < 32 for c in name):
        return PathIssue("illegal_path", name, SEVERITY_ERROR, "路径含控制字符")

    # 1) 盘符绝对路径（C:/…、C:\…，甚至 C:… 盘符相对形式也一并拒绝）
    if _DRIVE_RE.fullmatch(name[:2]):
        return PathIssue("absolute_path", name, SEVERITY_ERROR, "禁止盘符绝对路径")

    # 2) 设备路径前缀（\\?\、\\.\）优先于 UNC 判定
    if name.startswith(_DEVICE_PREFIXES):
        return PathIssue("device_path", name, SEVERITY_ERROR, "禁止设备路径前缀（\\\\?\\ 或 \\\\.\\）")

    # 3) UNC / POSIX 绝对路径
    if name.startswith("\\\\") or name.startswith("//"):
        return PathIssue("absolute_path", name, SEVERITY_ERROR, "禁止 UNC 绝对路径")
    if name.startswith("\\") or name.startswith("/"):
        return PathIssue("absolute_path", name, SEVERITY_ERROR, "禁止以路径分隔符开头的绝对路径")

    # 4) 逐段检查（zip 条目按约定用 '/'，但恶意包可能混入 '\'，一并防御）。
    #    按单字符切分（不用 + 折叠），连续分隔符产生的空段本身就是非法路径。
    #    目录条目允许单个结尾分隔符（"dir/"），先剥掉再切分。
    body = name[:-1] if name.endswith(("/", "\\")) else name
    segments = re.split(r"[\\/]", body)
    for seg in segments:
        if seg == "..":
            return PathIssue("parent_escape", name, SEVERITY_ERROR, "路径含 .. 段（Zip Slip）")
        if not seg:
            return PathIssue("illegal_path", name, SEVERITY_ERROR, "路径含空段（连续分隔符）")
        stem = seg.split(".", 1)[0]
        if _DEVICE_SEG_RE.fullmatch(stem):
            return PathIssue("device_path", name, SEVERITY_ERROR, f"保留设备名 {stem}（含扩展名形式同样禁止）")
        for c in seg:
            if c in _ILLEGAL_CHARS:
                return PathIssue("illegal_path", name, SEVERITY_ERROR, f"路径含非法字符 {c!r}")
        if seg != seg.rstrip(" ."):
            return PathIssue("illegal_path", name, SEVERITY_ERROR, "路径段以点或空格结尾（Windows 会静默截断）")

    # 5) 超长路径：>260 警告级；>4096 错误级（任何文件系统都不可靠）
    if len(name) > _MAX_PATH_HARD:
        return PathIssue("path_too_long", name, SEVERITY_ERROR, f"路径长度 {len(name)} 超过硬上限 {_MAX_PATH_HARD}")
    if len(name) > MAX_PATH_WIN:
        return PathIssue(
            "path_too_long", name, SEVERITY_WARNING,
            f"路径长度 {len(name)} 超过 Windows MAX_PATH（{MAX_PATH_WIN}），存在截断风险",
        )
    return None


def safe_resolve(root: Path, rel: str) -> Path:
    """把 ``rel`` 解析到 ``root`` 内的绝对路径；任何越界企图抛 :class:`PathGuardError`。

    在字符串规则之上追加符号链接逃逸检查：resolve（跟随 symlink/junction）
    后的结果必须仍在 resolve 后的 root 之内。这是所有落盘路径的唯一出口。
    """
    issue = check_entry_path(rel)
    if issue is not None and issue.severity == SEVERITY_ERROR:
        raise PathGuardError(issue.rule_id, rel, issue.message)

    root_resolved = Path(root).resolve()
    candidate = (Path(root) / rel).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PathGuardError(
            "symlink_escape", rel, "解析后越出根目录（符号链接/junction 逃逸）"
        )
    return candidate


# ----------------------------------------------------------------------
# Zip 安全解包：先全量预检，后落盘
# ----------------------------------------------------------------------


def _entry_issues(
    name: str,
    info: zipfile.ZipInfo,
    limits: SizeLimits,
    *,
    is_dir: bool,
) -> list[PathIssue]:
    """单条目的元数据级规则（字符串路径 + 大小/比例/扩展名）。"""
    issues: list[PathIssue] = []
    issue = check_entry_path(name)
    if issue is not None:
        issues.append(issue)

    # 暂存目录名保留：防同名条目与 staging 机制互相干扰
    if name == STAGING_DIRNAME or name.startswith(STAGING_DIRNAME + "/"):
        issues.append(PathIssue("illegal_path", name, SEVERITY_ERROR, "条目名与解包暂存目录冲突"))

    if info.flag_bits & 0x1:
        issues.append(PathIssue("encrypted_entry", name, SEVERITY_ERROR, "加密条目无法安全预检"))

    if not is_dir:
        suffix = Path(name).suffix.lower()
        if suffix in limits.blocked_extensions:
            issues.append(
                PathIssue("executable_payload", name, SEVERITY_ERROR, f"黑名单扩展名 {suffix}（可执行载荷）")
            )
        if info.file_size > limits.max_file_bytes:
            issues.append(
                PathIssue(
                    "file_too_large", name, SEVERITY_ERROR,
                    f"解压后 {info.file_size} 字节超过单文件上限 {limits.max_file_bytes}",
                )
            )
        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > limits.max_compression_ratio and info.file_size > 0:
            issues.append(
                PathIssue(
                    "compression_ratio_abuse", name, SEVERITY_ERROR,
                    f"压缩比 {ratio:.1f} 超过上限 {limits.max_compression_ratio}（疑似 zip bomb）",
                )
            )
    return issues


def _aggregate_issues(
    infos: Iterable[zipfile.ZipInfo],
    limits: SizeLimits,
) -> tuple[list[PathIssue], int]:
    """全包预检：逐条目规则 + 累计大小/条目数上限。"""
    issues: list[PathIssue] = []
    total = 0
    count = 0
    for info in infos:
        name = info.filename
        is_dir = info.is_dir()
        count += 1
        issues.extend(_entry_issues(name, info, limits, is_dir=is_dir))
        if not is_dir:
            total += info.file_size
    if count > limits.max_entries:
        issues.append(
            PathIssue("too_many_entries", "<package>", SEVERITY_ERROR,
                      f"条目数 {count} 超过上限 {limits.max_entries}")
        )
    if total > limits.max_total_bytes:
        issues.append(
            PathIssue("total_size_exceeded", "<package>", SEVERITY_ERROR,
                      f"累计解压大小 {total} 字节超过上限 {limits.max_total_bytes}")
        )
    return issues, count


def precheck_zip(zip_path: str | Path, limits: SizeLimits | None = None) -> ExtractReport:
    """只预检不解包：返回报告（供导入对话框/发布预检复用，TST-010 断言入口）。"""
    limits = limits or SizeLimits()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        return ExtractReport(
            ok=False, issues=[PathIssue("corrupt_zip", str(zip_path), SEVERITY_ERROR, f"无法读取 Zip：{exc}")]
        )
    issues, count = _aggregate_issues(infos, limits)
    return ExtractReport(
        ok=not any(i.severity == SEVERITY_ERROR for i in issues),
        issues=issues,
        entries_checked=count,
    )


def _copy_limited(src: zipfile.ZipExtFile, dst: Path, limit: int) -> int:
    """限量流式复制：防 ZIP 元数据谎报 file_size 的最后一道闸。"""
    written = 0
    with dst.open("wb") as fh:
        while True:
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise PathGuardError("file_too_large", dst.name,
                                     f"实际写入超过 {limit} 字节（元数据与内容不符）")
            fh.write(chunk)
    return written


def safe_extract_zip(
    zip_path: str | Path,
    dest: str | Path,
    limits: SizeLimits | None = None,
) -> ExtractReport:
    """安全解包：**全部条目通过预检才开始落盘**；任何违规一个条目都不解压。

    流程：
    1. 打开 Zip（损坏 -> ``corrupt_zip``，零写入）；
    2. 全量预检（路径规则 + 大小/比例/扩展名/条目数，含对 dest 内已存在
       符号链接的逃逸检查）——**任何问题（含警告级，如超长路径）** -> 直接
       返回报告，dest 不创建；
    3. 解压到 dest 内暂存目录（写入过程仍逐字节限量，防元数据谎报）；
    4. 全部成功后把暂存内容搬入 dest，删除暂存目录。

    失败时暂存目录被整体清除，dest 保持空（或不存在）。
    """
    limits = limits or SizeLimits()
    dest_path = Path(dest)

    # 1) 打开 + 2) 全量预检：任何问题（**含警告级**）都不落盘——
    #    超长路径等警告项在真实文件系统上同样不可靠，宁拒不解。
    report = precheck_zip(zip_path, limits)
    if report.issues:
        report.ok = False
        return report
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:  # 理论上 precheck 已拦
        return ExtractReport(
            ok=False, issues=[PathIssue("corrupt_zip", str(zip_path), SEVERITY_ERROR, f"无法读取 Zip：{exc}")]
        )

    with zf:
        infos = zf.infolist()
        dest_path.mkdir(parents=True, exist_ok=True)
        staging = dest_path / STAGING_DIRNAME
        staging.mkdir(exist_ok=True)
        extracted: list[str] = []
        written_total = 0
        try:
            for info in infos:
                name = info.filename
                # 符号链接逃逸按**最终落点** dest/<name> 判定：dest 内被预埋
                # 的 symlink/junction 指向外部时，解析结果越出 dest 即拒绝。
                safe_resolve(dest_path, name)
                # 实际写入目标在暂存目录内：name 已通过字符串规则（无 ..、
                # 非绝对、无非法字符），staging/<name> 必然落在 staging 内。
                target = staging / Path(name.replace("\\", "/"))
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src_fh:
                    written_total += _copy_limited(src_fh, target, limits.max_file_bytes)
                extracted.append(name)
        except PathGuardError as exc:
            # 元数据谎报/逃逸兜底：整体清除暂存，dest 保持空
            shutil.rmtree(staging, ignore_errors=True)
            report.issues.append(
                PathIssue(exc.rule_id, exc.path, SEVERITY_ERROR, exc.message)
            )
            report.ok = False
            return report
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            report.issues.append(
                PathIssue("corrupt_zip", "<package>", SEVERITY_ERROR, f"解压读写失败：{exc}")
            )
            report.ok = False
            return report

        # 4) 成功：搬入 dest 根，移除暂存目录
        for item in sorted(staging.iterdir()):
            shutil.move(str(item), str(dest_path / item.name))
        staging.rmdir()

    report.extracted = extracted
    report.bytes_written = written_total
    return report
