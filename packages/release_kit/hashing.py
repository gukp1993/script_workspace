"""内容哈希与不可变发布目录（VER-002，SEC-004 协作）。

职责：
- :func:`content_hash`：字节/文件的 SHA-256（内容寻址的基础）；
- :func:`freeze_directory`：把工作区冻结为 ``相对路径 -> sha256`` 清单
  （排除 traces/releases/snapshots/backups/.git/__pycache__ 等运行产物）；
- :func:`content_digest`：对冻结清单再求总摘要（发布内容指纹，release_id 短哈希来源）；
- :func:`verify_directory`：目录完整性校验，返回"被篡改/缺失/多余"清单——
  发布后任何文件变化都会导致 verify 失败（VER-002 / AC-P0-12 的基础）；
- :func:`copy_frozen_files` / :func:`sync_frozen_files`：按冻结清单复制/同步，
  回滚与快照共用（整体恢复的原子单位是"冻结清单"）。

安全约定：只读属性（os.chmod）是**尽力而为**的防误改手段，不是强制访问
控制；不可变性的最终判据是哈希校验（verify_directory）。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

from release_kit.errors import ReleaseError

#: 冻结时按目录名排除（任意层级）：运行产物与版本管理目录不进入发布单元
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {"traces", "releases", "snapshots", "backups", ".git", "__pycache__", ".pytest_cache"}
)


def content_hash(source: bytes | str | Path) -> str:
    """计算 SHA-256（64 位小写十六进制）。

    - 传 ``bytes``：直接对字节求哈希；
    - 传 ``str | Path``：视为文件路径，对文件内容求哈希。
    """
    if isinstance(source, bytes):
        return hashlib.sha256(source).hexdigest()
    path = Path(source)
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm_rel(path: str | Path) -> str:
    """相对路径统一为 ``/`` 分隔（Windows 下清单键稳定）。"""
    return str(path).replace("\\", "/")


def _iter_frozen_files(root: Path) -> Iterator[Path]:
    """按稳定顺序枚举参与冻结的文件（排除运行产物目录）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地过滤 + 排序：遍历顺序确定，冻结清单可复现
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIR_NAMES)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def freeze_directory(src: str | Path) -> dict[str, str]:
    """递归冻结目录：``相对路径（/ 分隔）-> sha256``。

    排除：traces / releases / snapshots / backups（运行产物与版本库位置，
    任意层级）以及 .git / __pycache__ / .pytest_cache。
    """
    root = Path(src)
    listing: dict[str, str] = {}
    for file_path in _iter_frozen_files(root):
        rel = _norm_rel(file_path.relative_to(root))
        listing[rel] = content_hash(file_path)
    return listing


def content_digest(files: dict[str, str]) -> str:
    """对冻结清单求总内容摘要：``sha256("<hash>  <path>" 行序列)``。

    这是发布内容指纹：内容相同 => 摘要相同（发布幂等的依据）。
    """
    canonical = "\n".join(f"{sha}  {path}" for path, sha in sorted(files.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_directory(
    directory: str | Path,
    expected: dict[str, str],
    *,
    ignore_extra: bool = False,
) -> list[str]:
    """校验目录与冻结清单一致；返回问题清单（空列表 = 通过）。

    问题格式：
    - ``modified: <path>``  文件存在但哈希不一致（被篡改）；
    - ``missing: <path>``   清单内有、目录里没有；
    - ``unexpected: <path>`` 目录里有、清单内没有（``ignore_extra=False`` 时）。

    发布后任何文件变化都会被本函数报告（VER-002 / AC-P0-12）。
    """
    root = Path(directory)
    actual = freeze_directory(root)
    problems: list[str] = []
    for rel in sorted(expected):
        if rel not in actual:
            problems.append(f"missing: {rel}")
        elif actual[rel] != expected[rel]:
            problems.append(f"modified: {rel}")
    if not ignore_extra:
        for rel in sorted(set(actual) - set(expected)):
            problems.append(f"unexpected: {rel}")
    return problems


def copy_frozen_files(src_root: str | Path, files: dict[str, str], dest_root: str | Path) -> int:
    """把冻结清单中的文件从 ``src_root`` 复制到 ``dest_root``（目标可写）。

    复制时逐一校验哈希，来源被篡改立即抛出 :class:`ReleaseError`；
    目标文件统一恢复可写位（发布目录是只读的，工作区/快照必须是可写的）。
    返回复制文件数。
    """
    src = Path(src_root)
    dest = Path(dest_root)
    count = 0
    for rel, expected_sha in sorted(files.items()):
        source_file = src / rel
        actual_sha = content_hash(source_file)
        if actual_sha != expected_sha:
            raise ReleaseError(
                f"来源文件与冻结清单不一致，已中止复制：{rel}",
                reasons=[f"modified: {rel}"],
            )
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source_file.read_bytes())
        _make_writable(target)
        count += 1
    return count


def sync_frozen_files(src_root: str | Path, files: dict[str, str], dest_root: str | Path) -> list[str]:
    """把 ``dest_root`` 同步为冻结清单状态：复制清单内文件 + 删除多余文件。

    用于整体回滚/升级失败恢复：工作区中不在清单内的受管文件一律删除，
    保证"五类对象+资产"与目标版本完全一致、不残留半新半旧状态。
    返回被删除的多余文件相对路径列表。
    """
    copy_frozen_files(src_root, files, dest_root)
    removed: list[str] = []
    for rel in sorted(set(freeze_directory(dest_root)) - set(files)):
        target = Path(dest_root) / rel
        _make_writable(target)
        target.unlink()
        removed.append(rel)
    return removed


def _make_writable(path: Path) -> None:
    """尽力而为地恢复写权限（Windows 只读属性可被 os.chmod 清除）。"""
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def make_tree_readonly(root: str | Path) -> None:
    """尽力而为地把目录内所有文件设为只读（VER-002）。

    Windows 下 os.chmod 只影响只读属性位，**非强制访问控制**：有写权限的
    进程仍可手动去除只读位后篡改——不可变性的最终判据是 verify_directory
    的哈希校验。只处理文件、不动目录，便于清理与工具遍历。
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                os.chmod(os.path.join(dirpath, name), 0o444)
            except OSError:
                pass  # 尽力而为：失败不阻断发布
