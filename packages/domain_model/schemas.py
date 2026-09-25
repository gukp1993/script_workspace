"""JSON Schema 加载帮助函数（DOM-002）。

Schema 文件位于仓库根 ``schemas/``，draft 2020-12，``$id`` 统一使用
``https://vaw.local/schemas/<kind>.schema.json``。
"""

from __future__ import annotations

import json
from pathlib import Path

from domain_model.errors import DomainValidationError, Issue
from domain_model.models import SCHEMA_VERSION

#: 支持的 Schema 种类（与 schemas/ 目录下文件一一对应）
SCHEMA_KINDS: tuple[str, ...] = ("project", "target", "detector", "machine", "policy", "calibration")

#: Schema 根 $id 前缀
SCHEMA_ID_PREFIX = "https://vaw.local/schemas"

#: JSON Schema draft 版本标识
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


def _schema_dirs() -> list[Path]:
    """按优先级返回可能的 schemas 目录（包相对 -> 当前工作目录）。"""
    package_root = Path(__file__).resolve().parents[2]  # packages/domain_model/schemas.py -> 仓库根
    return [package_root / "schemas", Path.cwd() / "schemas"]


def schema_path(kind: str) -> Path:
    """返回指定种类的 Schema 文件路径。

    Raises:
        DomainValidationError: kind 不受支持或 Schema 目录不存在。
    """
    if kind not in SCHEMA_KINDS:
        raise DomainValidationError(
            [Issue(file="<schema>", pointer="", rule="unknown_schema_kind",
                   message=f"未知 Schema 种类 {kind!r}", hint=f"允许值：{list(SCHEMA_KINDS)}")]
        )
    for base in _schema_dirs():
        candidate = base / f"{kind}.schema.json"
        if candidate.is_file():
            return candidate
    raise DomainValidationError(
        [Issue(file="<schema>", pointer="", rule="schema_file_missing",
               message=f"找不到 {kind}.schema.json；已尝试：{[str(b) for b in _schema_dirs()]}")]
    )


def load_schema(kind: str) -> dict:
    """加载并最小自检一份 JSON Schema（draft 2020-12 + $id + schema_version const 1）。"""
    path = schema_path(kind)
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainValidationError(
            [Issue(file=path.name, pointer="", rule="schema_file_invalid", message=f"Schema 文件无法读取：{exc}")]
        ) from exc
    issues: list[Issue] = []
    if schema.get("$schema") != DRAFT_2020_12:
        issues.append(Issue(file=path.name, pointer="/$schema", rule="schema_file_invalid",
                            message=f"Schema 必须声明 {DRAFT_2020_12}"))
    if schema.get("$id") != f"{SCHEMA_ID_PREFIX}/{kind}.schema.json":
        issues.append(Issue(file=path.name, pointer="/$id", rule="schema_file_invalid",
                            message=f"Schema $id 必须是 {SCHEMA_ID_PREFIX}/{kind}.schema.json"))
    const_version = (schema.get("properties") or {}).get("schema_version", {}).get("const")
    if const_version != SCHEMA_VERSION:
        issues.append(Issue(file=path.name, pointer="/properties/schema_version/const", rule="schema_file_invalid",
                            message=f"schema_version 必须是 const {SCHEMA_VERSION}"))
    if issues:
        raise DomainValidationError(issues)
    return schema
