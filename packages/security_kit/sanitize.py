"""路径、用户名、窗口标题等脱敏（SEC-007，ENG-005，TM-06）。

- :func:`sanitize_text`：单字符串脱敏——Windows 用户主目录（含中文用户名，
  如 ``C:/Users/顾柯鹏/…``）与 ``~`` 前缀 -> ``<user>``；盘符路径 ->
  ``<path>/<文件名>``（保留文件名便于排障）；长标题（含 ``" - "`` 的启发式）
  -> ``<title>``（token / truncate 两种模式可配）；
- :func:`sanitize_event_payload`：递归脱敏轨迹事件 payload，键名黑名单
  （username/user/home/title/path/file）直接以对应令牌替换取值；
- :func:`sanitize_trace`：逐事件脱敏后重写为"脱敏导出链"——哈希链按脱敏后
  内容**重新计算**，并以 ``sanitized=true`` 元事件开头标注导出性质；
- :func:`diagnostic_bundle`：诊断包收集（REL-004 单测版）——版本信息、
  脱敏后的配置与最近日志；**不含任何原图/截图**，附成员 sha256 清单供校验。

脱敏是**过删则过**（over-redaction）取向：宁可多遮，不可漏泄本机用户名、
绝对路径与窗口标题。
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from security_kit.path_guard import check_entry_path
from trace_format.writer import JsonlTraceReader, JsonlTraceWriter

__all__ = [
    "SECURITY_KIT_VERSION",
    "SanitizeConfig",
    "sanitize_text",
    "sanitize_event_payload",
    "sanitize_trace",
    "SanitizedTraceReport",
    "diagnostic_bundle",
    "DiagnosticBundleReport",
]

#: security_kit 版本（诊断包 version.txt 使用）。
SECURITY_KIT_VERSION: str = "0.1.0"

#: 脱敏导出链的元事件类型。
SANITIZED_META_EVENT_TYPE: str = "trace_sanitized"

#: 诊断包默认排除的"原图/截图"扩展名（AC REL-004：无未授权原图）。
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".npy"}
)

#: payload 键名黑名单（大小写不敏感）。
DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"username", "user", "home", "title", "path", "file"}
)

#: 标题启发式分隔符（常见窗口标题 "文档 - 应用" 形态）。
_TITLE_SEPARATOR: str = " - "

#: 盘符路径：盘符 + 分隔符 + 若干非分隔符段（不含空白，控制过度吞并）。
_DRIVE_PATH_RE: re.Pattern[str] = re.compile(
    r"[A-Za-z]:[\\/][^\s\\/:*?\"<>|\x00]*(?:[\\/][^\s\\/:*?\"<>|\x00]*)*"
)

#: ``~`` 或 ``~/``、``~\\`` 前缀（仅在字符串起始或分隔符/空白之后）。
_TILDE_RE: re.Pattern[str] = re.compile(r"(?:^|(?<=[\\/\s]))~(?=[\\/\s]|$)")


@dataclass(frozen=True, slots=True)
class SanitizeConfig:
    """脱敏配置（SEC-007：策略可配，默认最保守）。"""

    #: 本机用户主目录（自动取 Path.home()；也可显式指定以支持测试）。
    user_home: str = field(default_factory=lambda: str(Path.home()))
    #: 主目录/用户名令牌。
    user_token: str = "<user>"
    #: 盘符路径令牌（保留文件名）。
    path_token: str = "<path>"
    #: 窗口标题令牌。
    title_token: str = "<title>"
    #: 标题启发式最小长度（>= 该长度且含 " - " 才视为窗口标题）。
    title_min_len: int = 24
    #: 标题处理模式：``token``（整体替换，默认）/ ``truncate``（截断前缀）。
    title_mode: str = "token"
    #: truncate 模式保留的前缀字符数。
    title_keep_chars: int = 16
    #: 是否启用标题启发式。
    sanitize_titles: bool = True
    #: payload 键名黑名单。
    sensitive_keys: frozenset[str] = field(default_factory=lambda: DEFAULT_SENSITIVE_KEYS)

    def __post_init__(self) -> None:
        if self.title_mode not in ("token", "truncate"):
            raise ValueError("title_mode 只允许 'token' 或 'truncate'")

    def _home_pattern(self) -> re.Pattern[str]:
        """主目录正则：分隔符归一（/ 与 \\ 等价），后接分隔符或串尾。"""
        norm = self.user_home.replace("\\", "/").rstrip("/").rstrip("\\")
        body = re.escape(norm).replace("/", r"[\\/]")
        return re.compile(body + r"(?=[\\/]|$)", re.IGNORECASE)


def _replace_drive_paths(text: str, config: SanitizeConfig) -> str:
    """盘符路径 -> ``<path>/<文件名>``（保留最后一个段作为文件名）。"""

    def _repl(match: re.Match[str]) -> str:
        segments = [s for s in re.split(r"[\\/]+", match.group(0)) if s]
        filename = segments[-1] if len(segments) > 1 else ""
        return f"{config.path_token}/{filename}" if filename else config.path_token

    return _DRIVE_PATH_RE.sub(_repl, text)


def sanitize_text(s: str, config: SanitizeConfig | None = None) -> str:
    """脱敏单个字符串：标题 -> 主目录 -> ``~`` -> 盘符路径。

    顺序说明：标题启发式最先（整串含路径的标题直接整体令牌化，杜绝先
    部分脱敏造成的拼接泄漏）。
    """
    if not isinstance(s, str) or not s:
        return s
    config = config or SanitizeConfig()

    # 1) 窗口标题启发式：长标题（含 " - "）-> <title>
    if (
        config.sanitize_titles
        and len(s) >= config.title_min_len
        and _TITLE_SEPARATOR in s
    ):
        if config.title_mode == "token":
            return config.title_token
        return s[: config.title_keep_chars] + config.title_token

    # 2) 用户主目录（含中文用户名；分隔符两种写法都覆盖）
    s = config._home_pattern().sub(config.user_token, s)

    # 3) ~ 与 ~/、~\ 前缀
    s = _TILDE_RE.sub(config.user_token, s)

    # 4) 其余盘符路径 -> <path>/文件名
    s = _replace_drive_paths(s, config)
    return s


_KEY_TOKENS: dict[str, str] = {
    "username": "<user>",
    "user": "<user>",
    "home": "<user>",
    "title": "<title>",
}


def _sanitize_value(key: str, value: Any, config: SanitizeConfig) -> Any:
    """按键名黑名单与值内容递归脱敏。"""
    lowered = key.lower()
    if isinstance(value, dict):
        return {k: _sanitize_value(str(k), v, config) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(key, item, config) for item in value]
    if isinstance(value, str):
        if lowered in config.sensitive_keys:
            # 键名命中：整体令牌化（username/user/home -> <user>；title -> <title>）；
            # path/file 走 sanitize_text（保留文件名，遮蔽目录与用户名）。
            token = _KEY_TOKENS.get(lowered)
            if token is not None:
                return token
            return sanitize_text(value, config)
        return sanitize_text(value, config)
    return value


def sanitize_event_payload(
    payload: dict[str, Any], config: SanitizeConfig | None = None
) -> dict[str, Any]:
    """递归脱敏事件 payload；返回新的 dict（不改输入）。"""
    config = config or SanitizeConfig()
    if not isinstance(payload, dict):
        raise TypeError("payload 必须是 dict")
    result = {k: _sanitize_value(str(k), v, config) for k, v in payload.items()}
    return result


# ----------------------------------------------------------------------
# 轨迹脱敏导出（TRC-009 单测版落点）
# ----------------------------------------------------------------------


@dataclass(slots=True)
class SanitizedTraceReport:
    """sanitize_trace 的审计摘要。"""

    #: 源轨迹事件数。
    source_events: int
    #: 输出轨迹事件数（含 sanitized 元事件）。
    output_events: int
    #: 输出文件路径。
    output_path: Path
    #: 元事件哈希（脱敏导出链链首）。
    meta_event_hash: str


def sanitize_trace(
    src: str | Path,
    dst: str | Path,
    *,
    config: SanitizeConfig | None = None,
) -> SanitizedTraceReport:
    """把轨迹逐事件脱敏后重写为"脱敏导出链"。

    哈希链语义：脱敏改变了 payload，原链哈希必然失效；输出链按**脱敏后
    内容**重新计算全部哈希（JsonlTraceWriter 自动链式赋值），并以
    ``sanitized=true`` 元事件开头，明确这是脱敏导出而非原始轨迹。
    """
    config = config or SanitizeConfig()
    events = JsonlTraceReader(src).read()  # 最长可信前缀；损坏尾部不带出
    out_path = Path(dst)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with JsonlTraceWriter(out_path) as writer:
        meta = writer.append(
            SANITIZED_META_EVENT_TYPE,
            ts_monotonic=0.0,
            payload={
                "sanitized": True,
                "sanitizer": f"security_kit/{SECURITY_KIT_VERSION}",
                "source_events": len(events),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        for event in events:
            writer.append(
                event.type,
                ts_monotonic=event.ts_monotonic,
                correlation_id=event.correlation_id,
                session_id=event.session_id,
                payload=sanitize_event_payload(event.payload, config),
            )
    return SanitizedTraceReport(
        source_events=len(events),
        output_events=len(events) + 1,
        output_path=out_path,
        meta_event_hash=meta.hash,
    )


# ----------------------------------------------------------------------
# 诊断包（REL-004 单测版）
# ----------------------------------------------------------------------


@dataclass(slots=True)
class DiagnosticBundleReport:
    """diagnostic_bundle 的审计摘要。"""

    #: 包内成员名（含 bundle_manifest.json）。
    members: list[str] = field(default_factory=list)
    #: 因"不含原图"策略被排除的文件数。
    excluded_images: int = 0
    #: 输出 zip 路径。
    output_path: Path = Path("<unset>")

    @property
    def has_images(self) -> bool:
        """包内是否含有图片扩展名成员（恒为 False，自检失败会直接抛错）。"""
        return any(Path(m).suffix.lower() in IMAGE_EXTENSIONS for m in self.members)


def _bundle_member_name(rel: Path, folder: str) -> str:
    """包内成员名：diagnostic/<folder>/<相对文件夹内的路径>.txt（文本化）。

    ``rel`` 若已位于同名顶层目录下（如 ``logs/app.log`` 归入 logs），先剥掉
    该层，避免成员名出现 ``logs/logs/`` 双写。
    """
    if rel.parts and rel.parts[0] == folder and len(rel.parts) > 1:
        rel = Path(*rel.parts[1:])
    posix = rel.as_posix()
    if not posix.endswith(".txt"):
        posix += ".txt"
    return f"diagnostic/{folder}/{posix}"


def diagnostic_bundle(
    project_dir: str | Path,
    out_zip: str | Path,
    mask: object | None = None,
    *,
    config: SanitizeConfig | None = None,
) -> DiagnosticBundleReport:
    """收集脱敏诊断包：版本信息 + 脱敏配置 + 脱敏最近日志，**不含原图**。

    Args:
        project_dir: 项目目录（收集 project.yaml 等配置与 *.log 日志）。
        out_zip:     输出 zip 路径。
        mask:        可选 :class:`security_kit.privacy.PrivacyMask`；诊断包
                     本身不含像素，仅把遮罩配置（名称与 ROI）记入 privacy.json。
        config:      脱敏配置；缺省自动取本机主目录。

    Returns:
        :class:`DiagnosticBundleReport`（成员清单 + 排除原图数）。

    Raises:
        SecurityKitError: 写包后自检发现图片扩展名成员（防御性，不应发生）。
    """
    from security_kit.errors import SecurityKitError
    from security_kit.privacy import PrivacyMask

    project = Path(project_dir)
    config = config or SanitizeConfig()
    report = DiagnosticBundleReport()
    members: dict[str, bytes] = {}

    # 1) 版本信息（无敏感内容）
    version_info = {
        "security_kit": SECURITY_KIT_VERSION,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    members["diagnostic/version.txt"] = (
        json.dumps(version_info, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    # 2) 隐私配置说明（只有遮罩元数据，绝无像素）
    privacy_info: dict[str, Any] = {"images_included": False, "masks": []}
    if isinstance(mask, PrivacyMask):
        privacy_info["masks"] = list(mask.mask_names)
    members["diagnostic/privacy.json"] = (
        json.dumps(privacy_info, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    # 3) 配置文件（yaml/json，逐文件脱敏）；排除任何图片
    excluded = 0
    config_files = [
        p for p in sorted(project.rglob("*"))
        if p.is_file() and not p.is_symlink()
        and p.suffix.lower() in (".yaml", ".yml", ".json")
    ]
    for path in config_files:
        if path.suffix.lower() in IMAGE_EXTENSIONS:  # 理论不可达，双保险
            excluded += 1
            continue
        rel = path.relative_to(project)
        member = _bundle_member_name(rel, "config")
        text = sanitize_text(path.read_text(encoding="utf-8", errors="replace"), config)
        members[member] = text.encode("utf-8")

    # 4) 最近日志（*.log，逐行脱敏；上限保护诊断包自身大小）
    log_files = [
        p for p in sorted(project.rglob("*.log"))
        if p.is_file() and not p.is_symlink()
    ]
    for path in log_files[-50:]:
        rel = path.relative_to(project)
        member = _bundle_member_name(rel, "logs")
        lines: list[str] = []
        total = 0
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                sanitized_line = sanitize_text(line.rstrip("\n"), config)
                lines.append(sanitized_line)
                total += len(sanitized_line) + 1
                if total > 1_000_000:  # 单日志 1MB 上限
                    lines.append("<truncated>")
                    break
        members[member] = ("\n".join(lines) + "\n").encode("utf-8")

    excluded += sum(
        1 for p in project.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )
    report.excluded_images = excluded

    # 5) 成员校验清单（REL-004：内容可校验）——最后写入
    manifest_entries = [
        {"name": name, "bytes": len(payload), "sha256": _sha256_bytes(payload)}
        for name, payload in sorted(members.items())
    ]
    members["diagnostic/bundle_manifest.json"] = (
        json.dumps(
            {"images_included": False, "members": manifest_entries},
            ensure_ascii=False, indent=2,
        )
        + "\n"
    ).encode("utf-8")

    # 6) 写 zip（成员名本身也过 path 规则，杜绝包内穿越）
    out_path = Path(out_zip)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            if check_entry_path(name) is not None:
                raise SecurityKitError(f"诊断包成员名未通过路径规则：{name}")
            zf.writestr(name, payload)

    # 7) 写后自检：任何图片扩展名成员即视为实现缺陷，直接失败
    with zipfile.ZipFile(out_path) as zf:
        bad = [
            n for n in zf.namelist()
            if Path(n).suffix.lower() in IMAGE_EXTENSIONS
        ]
    if bad:
        raise SecurityKitError(f"诊断包含图片成员（违反 REL-004）：{bad}")

    report.members = sorted(members)
    report.output_path = out_path
    return report


def _sha256_bytes(payload: bytes) -> str:
    """字节串 sha256（诊断包成员清单用）。"""
    return hashlib.sha256(payload).hexdigest()
