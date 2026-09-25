"""日志/截图保留与安全清理（SEC-006，TRC-010，TM-06/TM-09）。

- :class:`StorageLayout`：工作区目录布局（traces/ snapshots/ frames/ …）；
- :class:`RetentionPolicy`：按类别的最小留存天数（默认 trace 7 / snapshot 14
  / frame_store 3 / log 7 天）；
- :func:`enforce`：按文件 mtime 年龄清理过期数据；**发布基线保护**：被
  releases 目录中 manifest 引用的路径一律不删（:func:`protected_paths`，
  manifest 不可读时保守放大保护范围）；清理动作逐条审计（deleted /
  protected_kept / errors）；支持 :func:`dry_run` 干跑（默认开启，无副作用）。

时钟约定：注入的 :class:`common.clock.Clock` 以 **Unix 纪元秒** 口径提供
"当前时间"（与文件 ``st_mtime`` 同单位），便于用 ``FakeClock(start=epoch)``
确定性测试；生产传 :class:`WallClock`。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from security_kit.errors import PathGuardError
from security_kit.path_guard import safe_resolve

__all__ = ["StorageLayout", "RetentionPolicy", "CleanupReport", "WallClock",
           "enforce", "protected_paths", "SECONDS_PER_DAY"]

#: 一天的秒数（mtime 年龄换算）。
SECONDS_PER_DAY: int = 86_400


class WallClock:
    """生产用墙钟（Unix 纪元秒），满足 common.clock.Clock 协议。"""

    def now(self) -> float:
        return time.time()


class _ClockProto(Protocol):
    """enforce 依赖的最小时钟协议（与 common.clock.Clock 结构兼容）。"""

    def now(self) -> float: ...


@dataclass(frozen=True, slots=True)
class StorageLayout:
    """工作区目录布局（全部为 root 下的相对目录名）。"""

    root: Path
    #: 运行轨迹目录名。
    traces: str = "traces"
    #: 截图快照目录名。
    snapshots: str = "snapshots"
    #: 帧存储目录名。
    frames: str = "frames"
    #: 日志目录名。
    logs: str = "logs"
    #: 发布目录名（保护来源，永不清理）。
    releases: str = "releases"

    @property
    def traces_dir(self) -> Path:
        return self.root / self.traces

    @property
    def snapshots_dir(self) -> Path:
        return self.root / self.snapshots

    @property
    def frames_dir(self) -> Path:
        return self.root / self.frames

    @property
    def logs_dir(self) -> Path:
        return self.root / self.logs

    @property
    def releases_dir(self) -> Path:
        return self.root / self.releases


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """按类别的最小留存天数（SEC-006：默认最小留存）。"""

    #: 运行轨迹保留天数。
    trace_days: int = 7
    #: 截图快照保留天数。
    snapshot_days: int = 14
    #: 帧存储保留天数。
    frame_store_days: int = 3
    #: 日志保留天数。
    log_days: int = 7
    #: 清理后是否顺带剪除空目录（仅托管目录的子目录，不动托管目录本身）。
    prune_empty_dirs: bool = True

    def __post_init__(self) -> None:
        for name in ("trace_days", "snapshot_days", "frame_store_days", "log_days"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 不能为负")


@dataclass(slots=True)
class CleanupReport:
    """一次清理的逐条审计（SEC-006：清理有审计）。"""

    #: 已删除（dry_run 时为"将删除"）的文件路径。
    deleted: list[Path] = field(default_factory=list)
    #: 因被发布基线引用而保留的文件路径。
    protected_kept: list[Path] = field(default_factory=list)
    #: 删除失败等错误（逐条字符串）。
    errors: list[str] = field(default_factory=list)
    #: 扫描到的受管文件总数。
    scanned: int = 0
    #: 是否干跑（True = 未产生任何写操作）。
    dry_run: bool = True


def _collect_strings(value: Any, out: list[str]) -> None:
    """深度收集 manifest 中的全部字符串值（保守保护的数据来源）。"""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect_strings(v, out)


def protected_paths(project_dir: str | Path) -> set[Path]:
    """收集发布基线引用的受保护路径集合（SEC-006/TRC-010 保守实现）。

    读取 ``<project>/releases/**/manifest.{json,yaml,yml}``，深度收集其中的
    字符串；凡是能解析为项目内现存路径（且通过 safe_resolve 防穿越）的都
    加入保护集合。**保守策略**：任何 manifest 不可读/解析失败时，把整个
    项目目录视为受保护（manifest 可能引用 traces/snapshots 等任意路径，
    宁可暂不清理，不可误删发布基线）。
    """
    project = Path(project_dir)
    releases = project / "releases"
    protected: set[Path] = set()
    if not releases.is_dir():
        return protected

    manifest_names = ("manifest.json", "manifest.yaml", "manifest.yml")
    manifests = [p for p in sorted(releases.rglob("*")) if p.name in manifest_names and p.is_file()]
    for manifest in manifests:
        data: Any = None
        try:
            if manifest.suffix == ".json":
                data = json.loads(manifest.read_text(encoding="utf-8"))
            else:
                data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            data = None
        strings: list[str] = []
        if data is None:
            # 解析失败：保守放大——整个项目目录受保护（含 traces/snapshots）
            protected.add(project.resolve())
            continue
        _collect_strings(data, strings)
        for raw in strings:
            candidate = raw.replace("\\", "/").strip()
            if not candidate or candidate.startswith(("/", "~")) or ":" in candidate:
                continue
            try:
                resolved = safe_resolve(project, candidate)
            except PathGuardError:
                continue
            if resolved.exists():
                protected.add(resolved)
    return protected


def _is_protected(path: Path, protected: set[Path]) -> bool:
    """path 自身、其祖先（被保护目录内的文件）或后代（被保护文件的外层目录）命中即保护。"""
    for p in protected:
        if path == p or p in path.parents or path in p.parents:
            return True
    return False


def _prune_empty_dirs(managed: Path) -> int:
    """自底向上剪除托管目录下的空子目录；返回剪除数量（托管目录本身保留）。"""
    pruned = 0
    if not managed.is_dir():
        return 0
    subdirs = [p for p in managed.rglob("*") if p.is_dir()]
    for d in sorted(subdirs, key=lambda p: len(p.parts), reverse=True):
        try:
            next(d.iterdir())
            continue  # 非空
        except StopIteration:
            pass
        except OSError:
            continue
        try:
            d.rmdir()
            pruned += 1
        except OSError:
            continue
    return pruned


def enforce(
    root: StorageLayout,
    clock: _ClockProto,
    policy: RetentionPolicy,
    *,
    dry_run: bool = True,
    project_dir: str | Path | None = None,
) -> CleanupReport:
    """按保留策略清理过期数据；返回逐条审计报告。

    Args:
        root:        工作区目录布局。
        clock:       Unix 纪元秒口径的时钟（测试注入 FakeClock）。
        policy:      保留策略。
        dry_run:     True（默认）只报告不删除，零副作用。
        project_dir: 提供时读取该项目的 releases manifest，保护被引用路径。

    语义说明：``deleted`` 在 dry_run 下表示"将要删除"的清单（报告口径），
    ``protected_kept`` 恒为实际保留的受保护文件。
    """
    now = clock.now()
    protected = protected_paths(project_dir) if project_dir is not None else set()
    report = CleanupReport(dry_run=bool(dry_run))

    categories: tuple[tuple[Path, int], ...] = (
        (root.traces_dir, policy.trace_days),
        (root.snapshots_dir, policy.snapshot_days),
        (root.frames_dir, policy.frame_store_days),
        (root.logs_dir, policy.log_days),
    )
    for directory, days in categories:
        if not directory.is_dir():
            continue
        cutoff = now - days * SECONDS_PER_DAY
        files = sorted(p for p in directory.rglob("*") if p.is_file())
        report.scanned += len(files)
        for f in files:
            try:
                mtime = f.stat().st_mtime
            except OSError as exc:
                report.errors.append(f"stat 失败 {f}: {exc}")
                continue
            if mtime >= cutoff:
                continue  # 未过期，保留
            if protected and _is_protected(f.resolve(), protected):
                report.protected_kept.append(f)
                continue
            if not dry_run:
                try:
                    f.unlink()
                except OSError as exc:
                    report.errors.append(f"删除失败 {f}: {exc}")
                    continue
            report.deleted.append(f)
        if policy.prune_empty_dirs and not dry_run:
            _prune_empty_dirs(directory)
    return report
