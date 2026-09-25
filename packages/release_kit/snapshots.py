"""草稿快照与历史比较（VER-003）。

- :class:`SnapshotManager.create`：把当前工作区冻结为命名快照
  （``snapshots/<时刻>-<名称>/``，附 snapshot.json 清单）；
- :meth:`SnapshotManager.load`：读取快照并做完整性校验（快照不可变）；
- :meth:`SnapshotManager.diff`：两次快照的对象级差异（新增/删除/修改，
  按对象 id + 内容哈希判定）与字段级差异（对 YAML dict 做递归 key 级对比）。

快照与发布共用冻结规则（排除 traces/releases/snapshots/backups 等运行产物）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from common.clock import Clock, MonotonicClock

from release_kit.errors import ReleaseError
from release_kit.hashing import content_digest, copy_frozen_files, freeze_directory, verify_directory
from release_kit.manifest import OBJECT_CATEGORIES, canonical_json

#: 快照清单文件名（位于快照目录根，不进入冻结清单）
SNAPSHOT_META = "snapshot.json"

#: 各类对象的标识字段名（与 manifest 一致）
_ID_FIELD: dict[str, str] = {
    "targets": "target_id",
    "policies": "policy_id",
    "detectors": "detector_id",
    "machines": "machine_id",
    "calibrations": "calibration_id",
}


@dataclass(frozen=True)
class SnapshotRef:
    """快照引用：名称、目录、创建时刻与内容指纹。"""

    name: str
    directory: Path
    created_at: float
    content_sha: str


@dataclass(frozen=True)
class SnapshotData:
    """快照内容：冻结清单 + 元信息（load 的返回值）。"""

    ref: SnapshotRef
    files: dict[str, str]


@dataclass
class ObjectDiff:
    """两次快照的差异报告。

    Attributes:
        added:         类别 -> 新增对象 id 列表。
        removed:       类别 -> 删除对象 id 列表。
        modified:      类别 -> 内容变化对象 id 列表（按内容哈希判定）。
        field_changes: ``"<类别>/<对象id>"`` -> ``字段路径 -> (旧值, 新值)``；
                       字段路径形如 ``states.exercise.transitions.0.when``。
        files_added / files_removed / files_modified:
                       原始文件级差异（含资产等非对象文件）。
    """

    added: dict[str, list[str]] = field(default_factory=dict)
    removed: dict[str, list[str]] = field(default_factory=dict)
    modified: dict[str, list[str]] = field(default_factory=dict)
    field_changes: dict[str, dict[str, tuple[Any, Any]]] = field(default_factory=dict)
    files_added: list[str] = field(default_factory=list)
    files_removed: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转可 JSON 化字典（字段级差异的值转字符串，便于展示）。"""
        return {
            "added": {k: list(v) for k, v in self.added.items()},
            "removed": {k: list(v) for k, v in self.removed.items()},
            "modified": {k: list(v) for k, v in self.modified.items()},
            "field_changes": {
                key: {path: [repr(old), repr(new)] for path, (old, new) in changes.items()}
                for key, changes in self.field_changes.items()
            },
            "files_added": list(self.files_added),
            "files_removed": list(self.files_removed),
            "files_modified": list(self.files_modified),
        }


def _sanitize_name(name: str) -> str:
    """快照名限制为文件安全字符。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", name).strip("-.")
    return cleaned or "snapshot"


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """把嵌套 dict/list 展平为 ``key 路径 -> 叶子值``（路径段用 ``.`` 连接）。

    例：``{"states": {"idle": {"transitions": [{"when": "x"}]}}}``
    -> ``{"states.idle.transitions.0.when": "x"}``
    """
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, sub in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten(sub, path))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            flat.update(_flatten(sub, f"{prefix}.{index}"))
    else:
        flat[prefix] = value
    return flat


def _read_objects(root: Path) -> dict[str, dict[str, tuple[dict[str, Any], str]]]:
    """读取目录下五类对象的原始 YAML dict：``类别 -> id -> (dict, 内容哈希)``。

    宽容读取：快照可能处于编辑中间态，单个文件解析失败记为
    ``<file:文件名>`` 伪对象，不中断 diff（文件级差异仍然可见）。
    """
    objects: dict[str, dict[str, tuple[dict[str, Any], str]]] = {}
    for category in OBJECT_CATEGORIES:
        bucket: dict[str, tuple[dict[str, Any], str]] = {}
        cat_dir = root / category
        if cat_dir.is_dir():
            for file_path in sorted(cat_dir.iterdir()):
                if file_path.suffix.lower() not in (".yaml", ".yml", ".json") or not file_path.is_file():
                    continue
                try:
                    data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
                except yaml.YAMLError:
                    data = None
                if isinstance(data, dict):
                    obj_id = str(data.get(_ID_FIELD[category], f"<file:{file_path.name}>"))
                else:
                    obj_id, data = f"<file:{file_path.name}>", {}
                bucket[obj_id] = (data, canonical_json(data))
        objects[category] = bucket
    return objects


class SnapshotManager:
    """草稿快照管理器（VER-003）。

    Attributes:
        project_dir:   被快照的项目工作区。
        snapshots_dir: 快照根目录（``snapshots/``）。
        clock:         注入时钟（缺省单调时钟）。
    """

    def __init__(
        self,
        project_dir: str | Path,
        snapshots_dir: str | Path,
        clock: Clock | None = None,
    ) -> None:
        self._project_dir = Path(project_dir)
        self._snapshots_dir = Path(snapshots_dir)
        self._clock: Clock = clock or MonotonicClock()

    def create(self, name: str) -> SnapshotRef:
        """创建命名快照：冻结当前工作区 + 记录元信息。"""
        files = freeze_directory(self._project_dir)
        created_at = float(self._clock.now())
        base_name = f"{int(created_at):010d}-{_sanitize_name(name)}"
        target = self._snapshots_dir / base_name
        suffix = 1
        while target.exists():
            target = self._snapshots_dir / f"{base_name}-{suffix}"
            suffix += 1
        copy_frozen_files(self._project_dir, files, target)
        meta = {"name": name, "created_at": created_at, "content_sha": content_digest(files), "files": files}
        (target / SNAPSHOT_META).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return SnapshotRef(name=name, directory=target, created_at=created_at, content_sha=meta["content_sha"])

    def list(self) -> list[SnapshotRef]:
        """列出全部快照（按创建时刻升序）。"""
        refs: list[SnapshotRef] = []
        if not self._snapshots_dir.is_dir():
            return refs
        for entry in sorted(self._snapshots_dir.iterdir()):
            meta_path = entry / SNAPSHOT_META
            if not entry.is_dir() or not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            refs.append(
                SnapshotRef(
                    name=str(meta.get("name", entry.name)),
                    directory=entry,
                    created_at=float(meta.get("created_at", 0.0)),
                    content_sha=str(meta.get("content_sha", "")),
                )
            )
        return sorted(refs, key=lambda ref: (ref.created_at, ref.name))

    def load(self, ref: SnapshotRef | str) -> SnapshotData:
        """读取快照内容；快照被篡改时抛 :class:`ReleaseError`。"""
        directory = ref.directory if isinstance(ref, SnapshotRef) else self._resolve_by_name(ref)
        meta_path = directory / SNAPSHOT_META
        if not meta_path.is_file():
            raise ReleaseError(f"快照不存在或缺少清单：{directory}")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"快照清单无法解析：{directory}（{exc}）") from exc
        files = {str(k): str(v) for k, v in meta.get("files", {}).items()}
        problems = verify_directory(directory, files, ignore_extra=True)
        if problems:
            raise ReleaseError(f"快照完整性校验失败：{directory}", reasons=problems)
        return SnapshotData(
            ref=SnapshotRef(
                name=str(meta.get("name", directory.name)),
                directory=directory,
                created_at=float(meta.get("created_at", 0.0)),
                content_sha=str(meta.get("content_sha", "")),
            ),
            files=files,
        )

    def diff(self, ref_a: SnapshotRef | str, ref_b: SnapshotRef | str) -> ObjectDiff:
        """比较两个快照：对象级（新增/删除/修改）+ 字段级（key 路径级）。"""
        data_a = self.load(ref_a)
        data_b = self.load(ref_b)
        diff = ObjectDiff(
            files_added=sorted(set(data_b.files) - set(data_a.files)),
            files_removed=sorted(set(data_a.files) - set(data_b.files)),
            files_modified=sorted(
                rel for rel in set(data_a.files) & set(data_b.files) if data_a.files[rel] != data_b.files[rel]
            ),
        )
        objects_a = _read_objects(data_a.ref.directory)
        objects_b = _read_objects(data_b.ref.directory)
        for category in OBJECT_CATEGORIES:
            bucket_a, bucket_b = objects_a[category], objects_b[category]
            added = sorted(set(bucket_b) - set(bucket_a))
            removed = sorted(set(bucket_a) - set(bucket_b))
            modified = sorted(
                obj_id
                for obj_id in set(bucket_a) & set(bucket_b)
                if bucket_a[obj_id][1] != bucket_b[obj_id][1]
            )
            if added:
                diff.added[category] = added
            if removed:
                diff.removed[category] = removed
            if modified:
                diff.modified[category] = modified
            for obj_id in modified:
                flat_a = _flatten(bucket_a[obj_id][0])
                flat_b = _flatten(bucket_b[obj_id][0])
                changes: dict[str, tuple[Any, Any]] = {}
                for path in sorted(set(flat_a) | set(flat_b)):
                    old = flat_a.get(path)
                    new = flat_b.get(path)
                    if old != new:
                        changes[path] = (old, new)
                if changes:
                    diff.field_changes[f"{category}/{obj_id}"] = changes
        return diff

    def _resolve_by_name(self, name: str) -> Path:
        """按快照名（或目录名）解析快照目录。"""
        for ref in self.list():
            if ref.name == name or ref.directory.name == name:
                return ref.directory
        raise ReleaseError(f"未找到快照：{name}")
