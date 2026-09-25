"""视觉资产文件存储（CTL-004 基础版）。

限制与防护：
- 扩展名白名单：``.png`` / ``.jpg`` / ``.yaml`` / ``.json``，非法 415；
- 大小上限：默认 20MB（可经配置调整），超限 413；
- 路径穿越防护：拒绝绝对路径、``..``、盘符/设备路径与任何越出项目根的
  相对路径，命中即 403（完整的安全解包——符号链接逃逸、Zip Slip、设备
  路径等——在 M2/M3 由 SEC-002 覆盖，此处为基础防护）；
- 内容 SHA-256 去重：内容统一存 ``<project_root>/assets-store/<sha><ext>``，
  相同内容只落盘一次。

资产清单写入 ``<projects>/<project_id>/assets/assets.yaml``，条目经
``domain_model.parse_asset`` 校验后落盘，保证始终可被 ``load_project`` 消费。
清单中的 ``path`` 是资产在**项目内**的逻辑相对路径；实际内容经 ``sha256``
从 assets-store 读取（同一内容跨项目共享，不重复存储）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from domain_model.errors import DomainValidationError
from domain_model.models import SCHEMA_VERSION
from domain_model.parsing import parse_asset

from control_plane.errors import ControlPlaneError
from control_plane.events import EventBroker
from control_plane.storage import ProjectStore
from control_plane.timeutil import utc_now_iso

#: 允许的资产扩展名（CTL-004）
ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".yaml", ".json"})

#: 扩展名 -> 响应 Content-Type
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".yaml": "application/yaml",
    ".json": "application/json",
}

#: 资产 ID 白名单（与 VisualAsset.asset_id 校验一致）
_ASSET_ID_RE = re.compile(r"[a-z][a-z0-9_-]*")


def _slug_asset_id(name: str) -> str:
    """把文件名主干折叠为合法 asset_id（kebab-case）。"""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not _ASSET_ID_RE.fullmatch(slug):
        slug = f"asset-{slug}"
    return slug


class AssetStore:
    """视觉资产存储门面（内容库 + 每项目清单）。"""

    def __init__(
        self,
        project_root: Path,
        *,
        store: ProjectStore,
        broker: EventBroker,
        max_bytes: int,
    ) -> None:
        self.project_root = Path(project_root)
        #: 内容去重库：<project_root>/assets-store/<sha256><ext>
        self.store_root = self.project_root / "assets-store"
        self._store = store
        self._broker = broker
        self.max_bytes = int(max_bytes)

    # ------------------------------------------------------------------
    # 上传与查询
    # ------------------------------------------------------------------

    def upload(
        self,
        project_id: str,
        rel_path: str,
        content: bytes,
        *,
        kind: str = "template",
        asset_id: str | None = None,
    ) -> dict[str, Any]:
        """上传资产：校验 -> 去重落盘 -> 更新清单 -> 发布事件。

        Args:
            project_id: 目标项目 ID。
            rel_path:   项目内逻辑相对路径（如 ``assets/templates/btn.png``）。
            content:    原始字节内容。
            kind:       资产类型（template/mask）。
            asset_id:   可选显式 ID；缺省由文件名派生。
        """
        pdir = self._store.project_dir(project_id, must_exist=True)
        logical = self._safe_rel_path(pdir, rel_path)
        ext = Path(logical).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ControlPlaneError(
                415,
                "unsupported_media_type",
                f"不允许的资产扩展名 {ext!r}",
                extra={"allowed": sorted(ALLOWED_EXTENSIONS)},
            )
        if kind not in ("template", "mask"):
            raise ControlPlaneError(422, "invalid_enum", "kind 只允许 template 或 mask")
        if not isinstance(content, bytes) or not content:
            raise ControlPlaneError(422, "empty_body", "资产内容不能为空")
        if len(content) > self.max_bytes:
            raise ControlPlaneError(
                413,
                "payload_too_large",
                f"资产超过大小上限（{self.max_bytes} 字节）",
                extra={"limit_bytes": self.max_bytes, "actual_bytes": len(content)},
            )
        resolved_id = asset_id if asset_id else _slug_asset_id(Path(logical).stem)
        if not isinstance(resolved_id, str) or not _ASSET_ID_RE.fullmatch(resolved_id):
            raise ControlPlaneError(422, "invalid_id", "asset_id 必须是小写字母开头的 kebab-case 字符串")

        sha = hashlib.sha256(content).hexdigest()
        self.store_root.mkdir(parents=True, exist_ok=True)
        store_file = self.store_root / f"{sha}{ext}"
        deduplicated = store_file.exists()
        if not deduplicated:
            store_file.write_bytes(content)

        manifest = self._load_manifest(pdir)
        if any(entry.get("asset_id") == resolved_id for entry in manifest["assets"]):
            raise ControlPlaneError(409, "duplicate_asset", f"asset_id {resolved_id!r} 已存在")
        entry: dict[str, Any] = {
            "asset_id": resolved_id,
            "path": logical,
            "sha256": sha,
            "version": 1,
            "kind": kind,
        }
        manifest["assets"].append(entry)
        self._save_manifest(pdir, manifest)
        self._broker.publish(
            "asset_uploaded",
            {"asset_id": resolved_id, "sha256": sha, "deduplicated": deduplicated, "bytes": len(content)},
        )
        return {**entry, "deduplicated": deduplicated, "stored_at": utc_now_iso()}

    def list_assets(self, project_id: str) -> dict[str, Any]:
        """列出项目资产清单。"""
        pdir = self._store.project_dir(project_id)
        assets = self._load_manifest(pdir)["assets"]
        return {"project_id": project_id, "assets": assets, "count": len(assets)}

    def get_asset(self, project_id: str, asset_id: str) -> dict[str, Any]:
        """读取单个资产清单条目；不存在 404。"""
        pdir = self._store.project_dir(project_id)
        for entry in self._load_manifest(pdir)["assets"]:
            if entry.get("asset_id") == asset_id:
                return entry
        raise ControlPlaneError(404, "asset_not_found", f"资产不存在：{asset_id}")

    def read_content(self, project_id: str, asset_id: str) -> tuple[bytes, str]:
        """按清单 sha256 从去重库读取内容；返回 (字节, Content-Type)。"""
        entry = self.get_asset(project_id, asset_id)
        sha = str(entry.get("sha256", ""))
        ext = Path(str(entry.get("path", ""))).suffix.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or ext not in ALLOWED_EXTENSIONS:
            raise ControlPlaneError(500, "corrupt_storage", "资产清单条目非法")
        # 防穿越：文件名仅由受控 sha 与白名单扩展名拼接，且必须落在库根内
        file_path = (self.store_root / f"{sha}{ext}").resolve()
        if file_path.parent != self.store_root.resolve():
            raise ControlPlaneError(403, "path_escape", "资产存储路径越界")
        if not file_path.is_file():
            raise ControlPlaneError(404, "asset_content_missing", "资产内容文件缺失")
        return file_path.read_bytes(), MEDIA_TYPES.get(ext, "application/octet-stream")

    # ------------------------------------------------------------------
    # 路径防护与清单
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_rel_path(pdir: Path, rel_path: str) -> str:
        """校验并归一化项目内相对路径；任何穿越企图一律 403。"""
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ControlPlaneError(422, "missing_field", "path 不能为空")
        normalized = rel_path.replace("\\", "/").strip()
        if normalized.startswith("/"):
            raise ControlPlaneError(403, "path_escape", "禁止绝对路径")
        if ":" in normalized:
            raise ControlPlaneError(403, "path_escape", "禁止盘符/设备路径")
        segments = normalized.split("/")
        if any(seg in ("..", "") for seg in segments):
            raise ControlPlaneError(403, "path_escape", "路径穿越被拒绝（.. 或空路径段）")
        base = pdir.resolve()
        candidate = (base / Path(*segments)).resolve()
        if candidate != base and base not in candidate.parents:
            raise ControlPlaneError(403, "path_escape", "路径越出项目根目录")
        return normalized

    def _manifest_path(self, pdir: Path) -> Path:
        """项目资产清单路径：assets/assets.yaml。"""
        return pdir / "assets" / "assets.yaml"

    def _load_manifest(self, pdir: Path) -> dict[str, Any]:
        """读取清单；缺失/损坏时回退空清单（损坏不让上传通道瘫痪）。"""
        path = self._manifest_path(pdir)
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("assets"), list):
                return {"assets": [e for e in data["assets"] if isinstance(e, dict)]}
        return {"assets": []}

    def _save_manifest(self, pdir: Path, manifest: dict[str, Any]) -> None:
        """保存清单；写入前逐条经 parse_asset 校验（非法 422，保证清单可消费）。"""
        for entry in manifest["assets"]:
            try:
                parse_asset(dict(entry), file="assets/assets.yaml")
            except DomainValidationError as exc:
                raise ControlPlaneError(
                    422, "validation_failed", "资产清单校验失败", issues=list(exc.issues)
                ) from None
        path = self._manifest_path(pdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION, "assets": manifest["assets"]}
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
