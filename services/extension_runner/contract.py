"""扩展能力契约与清单（PLG-001，基于 DOM-005 能力模型）。

安全约定：
- **能力白名单外默认拒绝**：清单只能声明 :data:`EXTENSION_CAPABILITY_REGISTRY`
  中已注册的能力；未注册能力（含一切 ``input.*`` 真实输入能力）出现在
  清单里即拒绝加载——扩展**没有任何途径**获得真实输入能力；
- ``entry`` 必须是 ``模块名:函数名`` 形式，函数签名为 ``func(context, payload)``；
- ``sha256`` 是**代码包哈希**（清单所在目录下全部 ``*.py`` 的内容指纹，
  见 :func:`compute_code_hash`），加载时强制复核，篡改即拒载；
- ``resource_limits`` 基础版只收 ``max_memory_mb`` / ``max_runtime_s``
  两个声明字段；父进程目前强制执行超时与输出大小上限（内存限额由
  Windows Job Object 承担，属后续里程碑，见 runner.py 说明）。

清单文件（YAML）形如::

    schema_version: 1
    extension_id: notify-toast
    version: 1.0.0
    capabilities: [notify.desktop]
    entry: notify_toast:run
    resource_limits:
      max_memory_mb: 256
      max_runtime_s: 30
    sha256: <64 位十六进制>
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from extension_runner.errors import ManifestError

#: 清单 Schema 版本（const 1，与领域模型一致）
MANIFEST_SCHEMA_VERSION = 1

#: 扩展可用能力注册表（默认拒绝：不在此清单中的能力一律不可声明/不可用）。
#: 故意**不含** ``input.*``——扩展进程永远无法取得真实输入能力（PLG-002：
#: 扩展无法绕过 Input Broker；真实输入只属于主进程内的策略闸门之后）。
EXTENSION_CAPABILITY_REGISTRY: frozenset[str] = frozenset(
    {
        "notify.desktop",    # 桌面通知（托盘/气泡）
        "trace.read",        # 只读运行轨迹
        "perception.read",   # 只读感知快照
    }
)

_ID_RE = re.compile(r"[a-z][a-z0-9_-]*")
_VERSION_RE = re.compile(r"\d+(\.\d+){0,3}")
_MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_FUNC_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

#: 清单允许的顶层字段（未知字段拒绝，防拼写错误静默生效）
_MANIFEST_FIELDS: frozenset[str] = frozenset(
    {"schema_version", "extension_id", "version", "capabilities", "entry",
     "resource_limits", "sha256", "description"}
)


@dataclass(frozen=True)
class ResourceLimits:
    """资源限额声明（基础版：仅作为清单契约字段与父进程超时上限）。"""

    max_memory_mb: int = 256
    max_runtime_s: float = 30.0


@dataclass(frozen=True)
class ExtensionManifest:
    """扩展清单（加载期已通过全部校验，含代码包哈希复核）。"""

    extension_id: str
    version: str
    capabilities: tuple[str, ...]
    entry: str
    resource_limits: ResourceLimits
    sha256: str
    description: str = ""
    #: 清单文件绝对路径（父进程用它派生代码目录并传给子进程）
    manifest_path: str = ""
    #: 来源文件名（展示/审计用）
    source_file: str = "manifest.yaml"
    #: 附带字段（保留给 host 展示层，不参与校验）
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def entry_module(self) -> str:
        """entry 的模块名（``module:function`` 前半段）。"""
        return self.entry.split(":", 1)[0]

    @property
    def entry_function(self) -> str:
        """entry 的函数名（``module:function`` 后半段）。"""
        return self.entry.split(":", 1)[1]

    @property
    def code_dir(self) -> Path:
        """代码包目录（清单所在目录）。"""
        return Path(self.manifest_path).resolve().parent

    def describe(self) -> dict:
        """host 列表展示用的摘要（不含内部字段）。"""
        return {
            "extension_id": self.extension_id,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "entry": self.entry,
            "max_memory_mb": self.resource_limits.max_memory_mb,
            "max_runtime_s": self.resource_limits.max_runtime_s,
            "sha256": self.sha256,
            "description": self.description,
        }


# ---------------------------------------------------------------------------
# 代码包哈希
# ---------------------------------------------------------------------------


def compute_code_hash(code_dir: str | Path) -> str:
    """计算代码包哈希：目录下全部 ``*.py``（递归，排除 ``__pycache__``）。

    指纹 = ``sha256("相对路径:文件sha256\\n" 按相对路径排序拼接)``；
    空目录（无 .py 文件）返回 64 个 ``0``——空代码包不构成有效扩展，
    调用方应同时要求 entry 模块存在（子进程导入失败即崩溃报错）。
    """
    root = Path(code_dir)
    lines: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if any(part == "__pycache__" for part in path.parts):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{rel}:{digest}")
    if not lines:
        return "0" * 64
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 清单加载与校验
# ---------------------------------------------------------------------------


def _require_issues(issues: list[str], path_text: str) -> None:
    """把收集到的问题抛成单个 :class:`ManifestError`。"""
    if issues:
        detail = "; ".join(issues)
        raise ManifestError(f"扩展清单校验失败（{path_text}）：{detail}")


def load_manifest(path: str | Path, *, verify_hash: bool = True) -> ExtensionManifest:
    """读取并校验扩展清单；任何问题抛 :class:`ManifestError`（默认拒绝）。

    Args:
        path: 清单文件路径（manifest.yaml / manifest.yml / manifest.json）。
        verify_hash: 是否复核代码包哈希（测试内部分析时可关）。
    """
    manifest_path = Path(path)
    path_text = manifest_path.name or str(manifest_path)
    if not manifest_path.is_file():
        raise ManifestError(f"扩展清单不存在：{manifest_path}")
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"扩展清单无法读取：{exc}") from exc
    if manifest_path.suffix.lower() == ".json":
        try:
            data = __import__("json").loads(text)
        except ValueError as exc:
            raise ManifestError(f"清单 JSON 解析失败：{exc}") from exc
    else:
        try:
            data = yaml.safe_load(text)  # safe_load：禁止任意对象反序列化
        except yaml.YAMLError as exc:
            raise ManifestError(f"清单 YAML 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("清单顶层必须是键值映射")

    issues: list[str] = []
    for key in data:
        if key not in _MANIFEST_FIELDS:
            issues.append(f"不支持的字段 {key!r}")
    _require_issues(issues, path_text)

    version = data.get("schema_version")
    if version != MANIFEST_SCHEMA_VERSION or isinstance(version, bool):
        raise ManifestError(
            f"schema_version 只支持 {MANIFEST_SCHEMA_VERSION}，得到 {version!r}（{path_text}）"
        )

    extension_id = data.get("extension_id")
    if not isinstance(extension_id, str) or not _ID_RE.fullmatch(extension_id or ""):
        issues.append("extension_id 必须是以小写字母开头的 kebab-case 字符串")

    ext_version = data.get("version")
    if not isinstance(ext_version, str) or not _VERSION_RE.fullmatch(ext_version or ""):
        issues.append("version 必须是形如 1 / 1.2 / 1.2.3 的版本号字符串")

    capabilities = data.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        issues.append("capabilities 必须是字符串数组")
    else:
        for cap in capabilities:
            if cap not in EXTENSION_CAPABILITY_REGISTRY:
                issues.append(
                    f"能力 {cap!r} 不在扩展能力白名单中（默认拒绝）；"
                    f"允许值：{sorted(EXTENSION_CAPABILITY_REGISTRY)}"
                )
        if len(set(capabilities)) != len(capabilities):
            issues.append("capabilities 存在重复声明")

    entry = data.get("entry")
    if not isinstance(entry, str) or entry.count(":") != 1:
        issues.append("entry 必须是 模块名:函数名 形式")
    else:
        module, func = entry.split(":", 1)
        if not _MODULE_RE.fullmatch(module) or not _FUNC_RE.fullmatch(func):
            issues.append(f"entry {entry!r} 的模块名或函数名非法")

    raw_limits = data.get("resource_limits", {})
    limits = ResourceLimits()
    if not isinstance(raw_limits, dict):
        issues.append("resource_limits 必须是映射")
    else:
        mem = raw_limits.get("max_memory_mb", limits.max_memory_mb)
        if not isinstance(mem, int) or isinstance(mem, bool) or mem < 1:
            issues.append("max_memory_mb 必须是 >=1 的整数")
        else:
            limits = ResourceLimits(
                max_memory_mb=mem,
                max_runtime_s=limits.max_runtime_s,
            )
        runtime = raw_limits.get("max_runtime_s", limits.max_runtime_s)
        if isinstance(runtime, bool) or not isinstance(runtime, (int, float)) or runtime <= 0:
            issues.append("max_runtime_s 必须是 >0 的数字")
        else:
            limits = ResourceLimits(
                max_memory_mb=limits.max_memory_mb,
                max_runtime_s=float(runtime),
            )

    sha256_value = data.get("sha256")
    if not isinstance(sha256_value, str) or not _SHA256_RE.fullmatch(sha256_value or ""):
        issues.append("sha256 必须是 64 位小写十六进制")
    _require_issues(issues, path_text)

    code_dir = manifest_path.resolve().parent
    if verify_hash and isinstance(sha256_value, str):
        actual = compute_code_hash(code_dir)
        if actual != sha256_value:
            raise ManifestError(
                f"代码包哈希不符（{path_text}）：清单声明 {sha256_value[:12]}…，"
                f"实际 {actual[:12]}…；代码可能被篡改，拒绝加载"
            )

    description = data.get("description", "")
    if not isinstance(description, str):
        description = ""
    return ExtensionManifest(
        extension_id=extension_id,
        version=ext_version,
        capabilities=tuple(capabilities),
        entry=entry,
        resource_limits=limits,
        sha256=sha256_value,
        description=description,
        manifest_path=str(manifest_path.resolve()),
        source_file=path_text,
    )
