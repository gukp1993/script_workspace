"""项目/目标/策略/检测器/状态机/标定的文件存储与 CRUD（CTL-003）。

目录结构（与 domain_model 的 ``load_project`` 布局一致）::

    <project_root>/projects/<project_id>/
        project.yaml
        targets/<target_id>.yaml
        policies/<policy_id>.yaml
        detectors/<detector_id>.yaml
        machines/<machine_id>.yaml
        calibrations/<calibration_id>.yaml

写入约定：
- **写前校验**：所有对象写入前先经 domain_model 解析器校验，非法载荷
  返回 422（携带规则 ID 与 JSON Pointer 字段路径）；
- **乐观锁**：每个对象文件头部带 ``meta: {version, updated_at}``，PUT 必须
  携带与当前一致的 ``meta.version``（或 ``?version=`` 查询参数），否则 409；
  更新成功后版本号递增；
- **防路径穿越**：project_id / 对象 ID 均按白名单正则校验，任何包含
  路径分隔符或 ``..`` 的标识直接 422。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import yaml

from domain_model.errors import DomainValidationError
from domain_model.models import SCHEMA_VERSION
from domain_model.parsing import (
    parse_calibration,
    parse_detector,
    parse_machine,
    parse_policy,
    parse_project,
    parse_target,
)

from control_plane.errors import ControlPlaneError
from control_plane.timeutil import utc_now_iso

#: project_id 白名单（kebab-case，杜绝路径穿越）
PROJECT_ID_RE = re.compile(r"[a-z][a-z0-9_-]*")

#: 对象 ID 白名单（比 kebab-case 稍宽：标定 ID 惯例含点，如 1920x1080-100）
OBJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

#: 对象种类 -> (ID 字段名, domain_model 解析器)
KIND_PARSERS: dict[str, tuple[str, Callable[..., Any]]] = {
    "targets": ("target_id", parse_target),
    "policies": ("policy_id", parse_policy),
    "detectors": ("detector_id", parse_detector),
    "machines": ("machine_id", parse_machine),
    "calibrations": ("calibration_id", parse_calibration),
}

#: 项目目录下的固定子目录
SUBDIRS: tuple[str, ...] = ("targets", "policies", "detectors", "machines", "calibrations", "assets")


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    """安全写 YAML（safe_dump，禁用任意对象反序列化）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 并要求顶层为映射；损坏文件按存储损坏处理。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ControlPlaneError(500, "corrupt_storage", f"存储文件损坏：{path.name}") from exc
    if not isinstance(data, dict):
        raise ControlPlaneError(500, "corrupt_storage", f"存储文件顶层必须是映射：{path.name}")
    return data


class ProjectStore:
    """工作区项目存储（``<project_root>/projects`` 的门面）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------
    # 路径与种类
    # ------------------------------------------------------------------

    def project_dir(self, project_id: str, *, must_exist: bool = True) -> Path:
        """解析并校验项目目录（防路径穿越）；不存在时 404。"""
        if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
            raise ControlPlaneError(422, "invalid_project_id", "project_id 非法（kebab-case，禁止路径分隔符）")
        pdir = self.root / project_id
        if must_exist and not pdir.is_dir():
            raise ControlPlaneError(404, "project_not_found", f"项目不存在：{project_id}")
        return pdir

    @staticmethod
    def _require_kind(kind: str) -> tuple[str, Callable[..., Any]]:
        """校验对象种类；未知种类 404（含 assets——资产走专用路由）。"""
        entry = KIND_PARSERS.get(kind)
        if entry is None:
            raise ControlPlaneError(404, "unknown_kind", f"未知对象类型 {kind!r}")
        return entry

    # ------------------------------------------------------------------
    # 项目
    # ------------------------------------------------------------------

    def create_project(self, project_id: str, name: str, description: str = "", notes: str = "") -> dict[str, Any]:
        """创建项目骨架（project.yaml + 子目录），project.yaml 经 parse_project 校验。"""
        pdir = self.project_dir(project_id, must_exist=False)
        if pdir.exists():
            raise ControlPlaneError(409, "project_exists", f"项目已存在：{project_id}")
        if not isinstance(name, str) or not name.strip():
            raise ControlPlaneError(422, "missing_field", "缺少必填字段 name")
        try:
            meta = parse_project(
                {
                    "schema_version": SCHEMA_VERSION,
                    "name": name,
                    "description": description if isinstance(description, str) else "",
                    "notes": notes if isinstance(notes, str) else "",
                },
                file="project.yaml",
            )
        except DomainValidationError as exc:
            raise ControlPlaneError(422, "validation_failed", "project.yaml 校验失败", issues=list(exc.issues)) from None
        for sub in SUBDIRS:
            (pdir / sub).mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION, "name": meta["name"], "description": meta["description"], "notes": meta["notes"]}
        _dump_yaml(pdir / "project.yaml", payload)
        return {"project_id": project_id, **payload, "counts": {kind: 0 for kind in KIND_PARSERS}}

    def list_projects(self) -> list[dict[str, Any]]:
        """列出全部项目（含各类对象计数）。"""
        if not self.root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for pdir in sorted(self.root.iterdir()):
            if not pdir.is_dir():
                continue
            meta: dict[str, Any] = {}
            for candidate in ("project.yaml", "project.yml"):
                f = pdir / candidate
                if f.is_file():
                    try:
                        meta = _load_yaml(f)
                    except ControlPlaneError:
                        meta = {}
                    break
            out.append(
                {
                    "project_id": pdir.name,
                    "name": meta.get("name", ""),
                    "description": meta.get("description", ""),
                    "counts": self.project_counts(pdir),
                }
            )
        return out

    def get_project(self, project_id: str) -> dict[str, Any]:
        """读取单个项目元数据与对象计数。"""
        pdir = self.project_dir(project_id)
        meta: dict[str, Any] = {}
        project_file = pdir / "project.yaml"
        if project_file.is_file():
            meta = _load_yaml(project_file)
        return {
            "project_id": project_id,
            "name": meta.get("name", ""),
            "description": meta.get("description", ""),
            "notes": meta.get("notes", ""),
            "counts": self.project_counts(pdir),
        }

    def project_counts(self, pdir: Path) -> dict[str, int]:
        """统计项目下各类对象文件数。"""
        return {kind: sum(1 for _ in (pdir / kind).glob("*.yaml")) for kind in KIND_PARSERS}

    def project_count(self) -> int:
        """工作区项目总数（健康检查用）。"""
        if not self.root.is_dir():
            return 0
        return sum(1 for p in self.root.iterdir() if p.is_dir())

    def workspace_has_protected_target(self) -> bool:
        """工作区内是否存在受保护在线目标（CTL-008 安全默认值判据）。"""
        if not self.root.is_dir():
            return False
        for pdir in sorted(self.root.iterdir()):
            tdir = pdir / "targets"
            if not pdir.is_dir() or not tdir.is_dir():
                continue
            for f in sorted(tdir.glob("*.yaml")):
                try:
                    data = _load_yaml(f)
                except ControlPlaneError:
                    continue
                if data.get("protected_online") is True:
                    return True
        return False

    # ------------------------------------------------------------------
    # 领域对象 CRUD
    # ------------------------------------------------------------------

    def list_objects(self, project_id: str, kind: str) -> list[dict[str, Any]]:
        """列出某类对象清单（含 meta 版本信息）。"""
        self._require_kind(kind)
        kind_dir = self.project_dir(project_id) / kind
        if not kind_dir.is_dir():
            return []
        return [_load_yaml(f) for f in sorted(kind_dir.glob("*.yaml"))]

    def get_object(self, project_id: str, kind: str, object_id: str) -> dict[str, Any]:
        """读取单个对象（含 meta）；不存在 404。"""
        self._require_kind(kind)
        # object_id 参与路径拼接，先做白名单校验（防穿越）
        if not isinstance(object_id, str) or not OBJECT_ID_RE.fullmatch(object_id):
            raise ControlPlaneError(422, "invalid_id", f"对象 ID 非法：{object_id!r}")
        path = self.project_dir(project_id) / kind / f"{object_id}.yaml"
        if not path.is_file():
            raise ControlPlaneError(404, "object_not_found", f"{kind}/{object_id} 不存在")
        return _load_yaml(path)

    def create_object(self, project_id: str, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        """创建对象：写前经 domain_model 校验；重复 ID 409；初始版本 1。"""
        id_field, _parser = self._require_kind(kind)
        if not isinstance(body, dict):
            raise ControlPlaneError(422, "invalid_body", "请求体必须是 JSON 对象")
        data = {k: v for k, v in body.items() if k != "meta"}
        obj_id = data.get(id_field)
        if not isinstance(obj_id, str) or not OBJECT_ID_RE.fullmatch(obj_id):
            raise ControlPlaneError(422, "invalid_id", f"{id_field} 缺失或非法")
        self._validate(kind, data)
        path = self.project_dir(project_id) / kind / f"{obj_id}.yaml"
        if path.exists():
            raise ControlPlaneError(409, "duplicate_id", f"{kind}/{obj_id} 已存在")
        payload: dict[str, Any] = {"meta": {"version": 1, "updated_at": utc_now_iso()}, **data}
        _dump_yaml(path, payload)
        return payload

    def update_object(
        self,
        project_id: str,
        kind: str,
        object_id: str,
        body: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """乐观锁更新：期望版本必须与当前一致，否则 409；成功后版本 +1。"""
        id_field, _parser = self._require_kind(kind)
        if not isinstance(body, dict):
            raise ControlPlaneError(422, "invalid_body", "请求体必须是 JSON 对象")
        if not isinstance(object_id, str) or not OBJECT_ID_RE.fullmatch(object_id):
            raise ControlPlaneError(422, "invalid_id", f"对象 ID 非法：{object_id!r}")
        path = self.project_dir(project_id) / kind / f"{object_id}.yaml"
        if not path.is_file():
            raise ControlPlaneError(404, "object_not_found", f"{kind}/{object_id} 不存在")
        current = _load_yaml(path)
        current_version = int(current.get("meta", {}).get("version", 0))

        expected = expected_version
        if expected is None and isinstance(body.get("meta"), dict):
            candidate = body["meta"].get("version")
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                expected = candidate
        if expected is None:
            # PUT 必须携带版本（CTL-003 乐观锁）
            raise ControlPlaneError(
                409,
                "version_conflict",
                "PUT 必须携带与当前一致的 meta.version",
                extra={"current_version": current_version, "reason": "version_required"},
            )
        if expected != current_version:
            raise ControlPlaneError(
                409,
                "version_conflict",
                "版本不匹配：对象已被修改，请基于最新版本重试",
                extra={"current_version": current_version, "expected_version": expected},
            )

        data = {k: v for k, v in body.items() if k != "meta"}
        if data.get(id_field) != object_id:
            raise ControlPlaneError(422, "id_mismatch", f"请求体 {id_field} 与路径不一致")
        self._validate(kind, data)
        payload = {"meta": {"version": current_version + 1, "updated_at": utc_now_iso()}, **data}
        _dump_yaml(path, payload)
        return payload

    def _validate(self, kind: str, data: dict[str, Any]) -> None:
        """写前校验：交给 domain_model 解析器；失败聚合为 422（含规则 ID/字段路径）。"""
        _id_field, parser = self._require_kind(kind)
        try:
            parser(data, file=f"<api:{kind}>")
        except DomainValidationError as exc:
            raise ControlPlaneError(422, "validation_failed", "对象校验失败", issues=list(exc.issues)) from None
