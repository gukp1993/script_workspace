"""整体回滚、升级备份与失败恢复、运行时兼容检查（VER-008/009/010）。

- :meth:`ReleaseRollback.rollback_to`：**整体回滚**——五类对象 + 资产 +
  策略从发布目录一起恢复到工作区（先自动备份），回滚后
  ``verify_directory`` 必须通过（AC-P0-12）；发布历史只增不删。
- :meth:`ReleaseRollback.upgrade_with_backup`：升级前自动备份 -> 执行
  迁移 callable -> 失败自动恢复原状并返回失败诊断（AC-P0-14：
  原项目可再次打开、无半迁移对象、诊断含失败步骤）。
- :func:`compat_check` / :func:`compat_message`：SemVer 闭区间兼容检查
  （VER-010），不兼容给出明确错误 + 迁移建议文案。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common.clock import Clock, MonotonicClock

from release_kit.errors import CompatError, MigrationStepError, ReleaseError
from release_kit.hashing import (
    content_digest,
    copy_frozen_files,
    freeze_directory,
    sync_frozen_files,
    verify_directory,
)
from release_kit.manifest import ReleaseManifest

#: 备份清单文件名
_BACKUP_META = "backup.json"


# ---------------------------------------------------------------------------
# SemVer 工具（VER-010）
# ---------------------------------------------------------------------------


def parse_semver(value: str) -> tuple[int, int, int]:
    """解析 SemVer（``1.2.3`` / ``1.2`` / ``1``）为可比较的三元组。"""
    parts = str(value).strip().split(".")
    if not 1 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        raise ReleaseError(f"非法的语义化版本：{value!r}（应为 1.2.3 形式）")
    numbers = [int(part) for part in parts]
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


def _compare_versions(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    """版本三元组比较：负 / 零 / 正。"""
    return (a > b) - (a < b)


def compat_message(manifest: ReleaseManifest, current_runtime: str) -> str:
    """运行时不兼容的完整错误文案（含迁移建议，VER-010）。"""
    compat = manifest.runtime_compat
    requirement = f">= {compat.min_runtime}"
    if compat.max_runtime is not None:
        requirement += f" 且 <= {compat.max_runtime}"
    return (
        f"项目包 {manifest.release_id} 要求运行时版本 {requirement}，"
        f"当前运行时为 {current_runtime}，不兼容，禁止运行/回滚。"
        "迁移建议：先升级运行时到兼容范围；或在新运行时上导入本项目包并执行 "
        "Schema 迁移（升级前自动备份，失败自动恢复），完成后再使用。"
    )


def compat_check(manifest: ReleaseManifest, current_runtime: str) -> bool:
    """运行时兼容检查（VER-010）：``min <= current <= max``（闭区间）。

    ``max_runtime`` 为 None 表示不设上限；版本串非法抛 :class:`ReleaseError`。
    """
    current = parse_semver(current_runtime)
    lower = parse_semver(manifest.runtime_compat.min_runtime)
    if _compare_versions(current, lower) < 0:
        return False
    if manifest.runtime_compat.max_runtime is not None:
        upper = parse_semver(manifest.runtime_compat.max_runtime)
        if _compare_versions(current, upper) > 0:
            return False
    return True


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollbackResult:
    """一次整体回滚的结果。"""

    release_id: str
    backup_dir: Path
    files_restored: int


@dataclass(frozen=True)
class UpgradeResult:
    """一次升级（Schema 迁移）的结果（AC-P0-14）。"""

    ok: bool
    #: 失败后是否成功恢复了原状
    restored: bool
    backup_dir: Path
    #: 人读诊断（含失败步骤与错误信息）
    diagnostics: list[str]
    error: str | None = None


# ---------------------------------------------------------------------------
# 回滚器
# ---------------------------------------------------------------------------


class ReleaseRollback:
    """整体回滚与升级失败恢复（VER-008/009）。

    Attributes:
        project_dir: 项目工作区。
        releases_dir: 发布根目录。
        backups_dir: 备份根目录（缺省 ``<project_dir>/backups``，
                     冻结规则自动排除，不会进入发布单元）。
    """

    def __init__(
        self,
        project_dir: str | Path,
        releases_dir: str | Path,
        *,
        backups_dir: str | Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._project_dir = Path(project_dir)
        self._releases_dir = Path(releases_dir)
        self._backups_dir = Path(backups_dir) if backups_dir is not None else self._project_dir / "backups"
        self._clock: Clock = clock or MonotonicClock()

    # -- 备份 ---------------------------------------------------------------

    def create_backup(self, label: str = "pre-rollback") -> Path:
        """备份当前工作区到 ``backups/<时刻>-<label>/``（含冻结清单元信息）。"""
        files = freeze_directory(self._project_dir)
        created_at = float(self._clock.now())
        safe_label = re.sub(r"[^A-Za-z0-9_.-]", "-", label) or "backup"
        base_name = f"{int(created_at):010d}-{safe_label}"
        target = self._backups_dir / base_name
        suffix = 1
        while target.exists():
            target = self._backups_dir / f"{base_name}-{suffix}"
            suffix += 1
        copy_frozen_files(self._project_dir, files, target)
        meta = {"label": label, "created_at": created_at, "content_sha": content_digest(files), "files": files}
        (target / _BACKUP_META).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return target

    # -- 整体回滚（AC-P0-12） ------------------------------------------------

    def rollback_to(self, release_id: str, *, current_runtime: str | None = None) -> RollbackResult:
        """整体回滚到指定发布：五类对象 + 资产 + 策略一起恢复。

        流程：加载清单 -> 兼容检查（可选）-> 发布目录完整性校验 ->
        自动升级式备份 -> 整体同步（复制 + 删除多余受管文件）->
        回滚后完整性校验（必须通过，否则抛错）。发布历史不删除，
        v2 等后续版本仍可列出与审计（运行记录继续引用各自版本）。

        Args:
            release_id:      目标发布 ID。
            current_runtime: 提供时先做 VER-010 兼容检查，不兼容抛
                             :class:`CompatError`（含迁移建议文案）。
        """
        release_dir = self._releases_dir / release_id
        manifest_path = release_dir / "manifest.json"
        if not manifest_path.is_file():
            raise ReleaseError(f"发布不存在或缺少清单：{release_id}")
        manifest = ReleaseManifest.load(manifest_path)

        if current_runtime is not None and not compat_check(manifest, current_runtime):
            raise CompatError(compat_message(manifest, current_runtime))

        # 回滚来源必须完整可信：发布目录被篡改时拒绝回滚
        source_problems = verify_directory(release_dir, manifest.files, ignore_extra=True)
        if source_problems:
            raise ReleaseError(
                f"发布目录完整性校验失败，拒绝回滚：{release_id}", reasons=source_problems
            )

        backup_dir = self.create_backup("pre-rollback")
        sync_frozen_files(release_dir, manifest.files, self._project_dir)

        # AC-P0-12：回滚后工作区必须与目标版本冻结哈希完全一致
        remaining = verify_directory(self._project_dir, manifest.files, ignore_extra=True)
        if remaining:
            raise ReleaseError(f"回滚后完整性校验失败：{release_id}", reasons=remaining)
        return RollbackResult(release_id=release_id, backup_dir=backup_dir, files_restored=len(manifest.files))

    # -- 升级备份与失败恢复（AC-P0-14） ---------------------------------------

    def upgrade_with_backup(self, migrate_fn: Callable[[Path], None]) -> UpgradeResult:
        """升级前自动备份 -> 执行迁移 callable -> 失败自动恢复原状。

        ``migrate_fn(project_dir)`` 约定：任一步骤失败抛
        :class:`MigrationStepError`（携带步骤名）；其他异常的失败步骤
        记为 ``unspecified``。失败时工作区被恢复为升级前状态并逐文件
        校验，保证原项目可再次打开、无半迁移对象。

        Args:
            migrate_fn: 迁移 callable（如 DOM-004 的 Schema 迁移）。
        """
        files_before = freeze_directory(self._project_dir)
        backup_dir = self.create_backup("pre-upgrade")
        try:
            migrate_fn(self._project_dir)
        except Exception as exc:  # noqa: BLE001 - 任何迁移失败都必须恢复原状
            step = getattr(exc, "step", None) or "unspecified"
            if not isinstance(exc, MigrationStepError):
                # 非约定异常也按失败处理，但保留类型信息供诊断
                step = step if step != "unspecified" else f"unspecified({type(exc).__name__})"
            sync_frozen_files(backup_dir, files_before, self._project_dir)
            restore_problems = verify_directory(self._project_dir, files_before, ignore_extra=True)
            diagnostics = [
                f"升级失败，已从备份恢复原状：{backup_dir}",
                f"失败步骤：{step}",
                f"错误信息：{exc}",
            ]
            if restore_problems:
                diagnostics.append("恢复后校验仍存在问题（需人工介入）：" + "; ".join(restore_problems))
            return UpgradeResult(
                ok=False,
                restored=not restore_problems,
                backup_dir=backup_dir,
                diagnostics=diagnostics,
                error=str(exc),
            )
        return UpgradeResult(ok=True, restored=False, backup_dir=backup_dir, diagnostics=[])
