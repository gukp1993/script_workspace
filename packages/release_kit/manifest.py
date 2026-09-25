"""发布清单 ReleaseManifest（VER-001，ADR-0007 §2 发布冻结清单）。

发布单元 = **整个项目**（整体回滚的原子单位）。一次发布冻结：
- 五类领域对象清单（targets/policies/detectors/machines/calibrations，
  每个对象：id + 内容 sha256 + 版本号）；
- 资产清单（文件 sha256）与全量文件冻结清单（``files``，完整性校验依据）；
- 策略摘要（模式、人工闸门、预算上限）；
- 运行时兼容范围（SemVer 闭区间，VER-010）；
- 测试摘要（通过/失败/跳过计数 + 来源报告哈希，VER-005）。

清单只描述事实，不做文件 IO 之外的解释；``to_dict/from_dict/save/load``
保证跨进程往返一致，且同一内容构建的清单哈希稳定。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from domain_model.parsing import ProjectBundle

from release_kit.errors import ReleaseError
from release_kit.hashing import content_digest

#: 清单自身 Schema 版本（与领域 Schema 版本独立演进）
MANIFEST_SCHEMA_VERSION = "1"

#: 发布冻结的五类领域对象（目录名 -> 项目内目录）
OBJECT_CATEGORIES: tuple[str, ...] = ("targets", "policies", "detectors", "machines", "calibrations")

#: 各类对象的标识字段名
_ID_FIELD: dict[str, str] = {
    "targets": "target_id",
    "policies": "policy_id",
    "detectors": "detector_id",
    "machines": "machine_id",
    "calibrations": "calibration_id",
}


def _json_default(value: Any) -> Any:
    """规范化 JSON 的兜底序列化（tuple -> list，其余拒绝）。"""
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"清单序列化不支持类型：{type(value)!r}")


def canonical_json(payload: Any) -> str:
    """稳定序列化（排序键、紧凑分隔符）——所有哈希输入都经过它。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def hash_domain_object(obj: Any) -> str:
    """领域对象内容哈希：对 dataclass 规范化 JSON 求 SHA-256。

    与来源文件格式（键序/注释/空白）无关，只反映对象语义内容。
    """
    return hashlib.sha256(canonical_json(dataclasses.asdict(obj)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 清单子对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectEntry:
    """清单中的单个领域对象条目：id + 内容哈希 + 版本号。

    版本号规则：相对上一发布清单，内容相同则沿用旧版本号，
    内容变化则旧版本号 +1，新对象从 1 开始（可审计的对象级演进）。
    """

    id: str
    sha256: str
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "sha256": self.sha256, "version": self.version}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ObjectEntry":
        return cls(id=str(data["id"]), sha256=str(data["sha256"]), version=int(data["version"]))


@dataclass(frozen=True)
class RuntimeCompat:
    """运行时兼容范围（SemVer 闭区间，VER-010）。

    ``max_runtime`` 为 ``None`` 表示不设上限。
    """

    min_runtime: str = "0.0.0"
    max_runtime: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"min_runtime": self.min_runtime, "max_runtime": self.max_runtime}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeCompat":
        max_runtime = data.get("max_runtime")
        return cls(min_runtime=str(data["min_runtime"]), max_runtime=None if max_runtime is None else str(max_runtime))


@dataclass(frozen=True)
class TestSummary:
    """测试摘要（VER-005 质量闸门的发布侧留痕）。"""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    #: 来源测试报告的内容哈希（可追溯到具体一次测试运行）
    report_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "report_sha256": self.report_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestSummary":
        return cls(
            passed=int(data.get("passed", 0)),
            failed=int(data.get("failed", 0)),
            skipped=int(data.get("skipped", 0)),
            report_sha256=str(data.get("report_sha256", "")),
        )


# ---------------------------------------------------------------------------
# 发布清单
# ---------------------------------------------------------------------------


@dataclass
class ReleaseManifest:
    """项目级发布清单（VER-001）。

    Attributes:
        release_id:     发布 ID：``v<递增序号>-<内容短哈希>``。
        schema_version: 清单 Schema 版本（常量 "1"）。
        created_at:     创建时刻（注入 clock 的 ``now()`` 值）。
        project:        项目快照引用：name + 项目内容快照摘要。
        objects:        五类对象清单：类别 -> ObjectEntry 列表。
        assets:         资产清单：资产相对路径 -> 实际文件 sha256。
        policy_summary: 策略摘要（模式/人工闸门/预算）列表。
        runtime_compat: 运行时兼容范围。
        test_summary:   测试摘要。
        files:          全量冻结清单：相对路径 -> sha256（发布不可变校验依据）。
        content_sha:    冻结清单总摘要（发布内容指纹）。
    """

    release_id: str
    schema_version: str
    created_at: float
    project: dict[str, Any]
    objects: dict[str, list[ObjectEntry]] = field(default_factory=dict)
    assets: dict[str, str] = field(default_factory=dict)
    policy_summary: list[dict[str, Any]] = field(default_factory=list)
    runtime_compat: RuntimeCompat = field(default_factory=RuntimeCompat)
    test_summary: TestSummary = field(default_factory=TestSummary)
    files: dict[str, str] = field(default_factory=dict)
    content_sha: str = ""

    # -- 序列化 ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 化的字典（键序确定，往返稳定）。"""
        return {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "created_at": self.created_at,
            "project": dict(self.project),
            "objects": {cat: [entry.to_dict() for entry in entries] for cat, entries in self.objects.items()},
            "assets": dict(self.assets),
            "policy_summary": [dict(item) for item in self.policy_summary],
            "runtime_compat": self.runtime_compat.to_dict(),
            "test_summary": self.test_summary.to_dict(),
            "files": dict(self.files),
            "content_sha": self.content_sha,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReleaseManifest":
        """从字典重建清单；Schema 版本不符或字段缺失时抛 :class:`ReleaseError`。"""
        if not isinstance(data, dict):
            raise ReleaseError("清单必须是 JSON 映射")
        version = data.get("schema_version")
        if version != MANIFEST_SCHEMA_VERSION:
            raise ReleaseError(
                f"清单 Schema 版本不受支持：{version!r}（当前支持 {MANIFEST_SCHEMA_VERSION}）"
            )
        for key in ("release_id", "created_at", "files", "content_sha"):
            if key not in data:
                raise ReleaseError(f"清单缺少必填字段：{key}")
        objects_raw = data.get("objects", {})
        if not isinstance(objects_raw, dict):
            raise ReleaseError("清单 objects 必须是映射")
        return cls(
            release_id=str(data["release_id"]),
            schema_version=str(version),
            created_at=float(data["created_at"]),
            project=dict(data.get("project", {})),
            objects={
                cat: [ObjectEntry.from_dict(entry) for entry in entries]
                for cat, entries in objects_raw.items()
            },
            assets={str(k): str(v) for k, v in data.get("assets", {}).items()},
            policy_summary=[dict(item) for item in data.get("policy_summary", [])],
            runtime_compat=RuntimeCompat.from_dict(dict(data.get("runtime_compat", {}))),
            test_summary=TestSummary.from_dict(dict(data.get("test_summary", {}))),
            files={str(k): str(v) for k, v in data["files"].items()},
            content_sha=str(data["content_sha"]),
        )

    def save(self, path: str | Path) -> Path:
        """保存为 JSON 文件（UTF-8、两空格缩进、结尾换行）。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=_json_default) + "\n"
        target.write_text(text, encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "ReleaseManifest":
        """从 JSON 文件加载清单；解析失败抛 :class:`ReleaseError`。"""
        source = Path(path)
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"清单文件无法读取或解析：{source}（{exc}）") from exc
        return cls.from_dict(data)

    # -- 便捷视图 ----------------------------------------------------------

    def object_ids(self, category: str) -> list[str]:
        """某类对象的 ID 列表（按清单顺序）。"""
        return [entry.id for entry in self.objects.get(category, [])]

    def find_object(self, category: str, obj_id: str) -> ObjectEntry | None:
        """按类别与 ID 查找对象条目。"""
        for entry in self.objects.get(category, []):
            if entry.id == obj_id:
                return entry
        return None


# ---------------------------------------------------------------------------
# 清单构建
# ---------------------------------------------------------------------------


def _previous_version_map(
    previous: ReleaseManifest | None, category: str
) -> dict[str, tuple[str, int]]:
    """上一发布清单中某类对象的 ``id -> (旧哈希, 旧版本号)``。"""
    if previous is None:
        return {}
    return {entry.id: (entry.sha256, entry.version) for entry in previous.objects.get(category, [])}


def build_object_catalog(
    bundle: ProjectBundle, previous: ReleaseManifest | None = None
) -> dict[str, list[ObjectEntry]]:
    """构建五类对象清单：id + 对象内容哈希 + 版本号（见 :class:`ObjectEntry`）。"""
    catalog: dict[str, list[ObjectEntry]] = {}
    for category in OBJECT_CATEGORIES:
        bucket = getattr(bundle, category)
        prev = _previous_version_map(previous, category)
        entries: list[ObjectEntry] = []
        for obj_id in sorted(bucket):
            sha = hash_domain_object(bucket[obj_id])
            if obj_id in prev:
                prev_sha, prev_version = prev[obj_id]
                version = prev_version if prev_sha == sha else prev_version + 1
            else:
                version = 1
            entries.append(ObjectEntry(id=obj_id, sha256=sha, version=version))
        catalog[category] = entries
    return catalog


def build_policy_summary(bundle: ProjectBundle) -> list[dict[str, Any]]:
    """策略摘要：发布冻结清单要求策略配置可审计（ADR-0007 §2）。"""
    summary: list[dict[str, Any]] = []
    for policy_id in sorted(bundle.policies):
        policy = bundle.policies[policy_id]
        summary.append(
            {
                "policy_id": policy.policy_id,
                "mode": policy.mode_value.value,
                "require_manual_start": policy.require_manual_start,
                "max_runtime_minutes": policy.max_runtime_minutes,
                "max_actions_per_minute": policy.max_actions_per_minute,
                "max_total_actions": policy.max_total_actions,
                "unattended_schedule": policy.unattended_value.value,
            }
        )
    return summary


def build_asset_listing(bundle: ProjectBundle, files: dict[str, str]) -> dict[str, str]:
    """资产清单：资产相对路径 -> 实际文件 sha256。

    取**实际文件**哈希（冻结清单值）而非清单登记值——登记值与实际不一致
    时静态分析（reference_missing_asset）已阻断发布，这里以实际内容为准。
    """
    listing: dict[str, str] = {}
    for asset_id in sorted(bundle.assets):
        norm_path = bundle.assets[asset_id].path.replace("\\", "/")
        if norm_path in files:
            listing[norm_path] = files[norm_path]
    return listing


def build_release_manifest(
    *,
    bundle: ProjectBundle,
    files: dict[str, str],
    release_id: str,
    created_at: float,
    runtime_compat: RuntimeCompat | None = None,
    test_summary: TestSummary | None = None,
    previous: ReleaseManifest | None = None,
) -> ReleaseManifest:
    """从项目聚合根 + 冻结清单构建发布清单。

    Args:
        bundle:         已解析的项目聚合根（发布前置检查已通过）。
        files:          工作区冻结清单（:func:`release_kit.hashing.freeze_directory`）。
        release_id:     发布 ID（由发布器按序号+短哈希分配）。
        created_at:     注入时钟的当前时刻。
        runtime_compat: 运行时兼容范围（缺省不设限）。
        test_summary:   测试摘要（缺省为空摘要）。
        previous:       上一发布清单（用于对象版本号递增；首次发布传 None）。
    """
    content_sha = content_digest(files)
    return ReleaseManifest(
        release_id=release_id,
        schema_version=MANIFEST_SCHEMA_VERSION,
        created_at=created_at,
        project={"name": str(bundle.project.get("name", "")), "snapshot": content_sha},
        objects=build_object_catalog(bundle, previous=previous),
        assets=build_asset_listing(bundle, files),
        policy_summary=build_policy_summary(bundle),
        runtime_compat=runtime_compat or RuntimeCompat(),
        test_summary=test_summary or TestSummary(),
        files=dict(files),
        content_sha=content_sha,
    )
