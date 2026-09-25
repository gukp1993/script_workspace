"""轨迹 / 帧 / 回放报告只读端点（M3 E11：UI-013/014/015 数据源）。

路由（全部只读 GET，挂载在 ``/api/v1`` 下自动受既有防火墙中间件保护，
CTL-001/002）：

- ``GET /api/v1/projects/{pid}/traces``：列出项目 ``traces/`` 目录下的
  ``.jsonl`` 轨迹文件（文件名/大小/修改时间/事件数概览）；
- ``GET /api/v1/projects/{pid}/traces/{name}/events``：用
  :class:`trace_format.JsonlTraceReader` 读取事件列表（尾部损坏自动截断并
  标记 ``truncated_tail``），过滤语义复用
  :func:`trace_format.timeline.query_timeline`（``since``/``until`` 为
  ``ts_monotonic`` 闭区间，``types`` 逗号分隔事件类型，``state`` 匹配
  state_transition 的 from/to，``limit`` 截取前 N 条）；
- ``GET /api/v1/projects/{pid}/traces/{name}/frame/{ref}``：从 FrameStore
  目录（``<项目>/traces/frames/frames/<ref>.npy``）读帧并编码 PNG 返回；
  引用不存在 404、格式非法（非 64 位十六进制，可能含路径穿越）422；
- ``GET /api/v1/projects/{pid}/replay-reports``：列出 ``tests/replay/``
  目录下的回放/差异报告产物（内联前若干字符预览，供测试中心空态/渲染）。

安全约定：
- ``name`` 参与路径拼接：先按文件名白名单校验（禁分隔符/``..``），再经
  :func:`security_kit.path_guard.safe_resolve` 确认 resolve 后仍落在项目
  ``traces/`` 目录内（同时阻断符号链接逃逸）；
- ``ref`` 必须是 64 位小写十六进制（sha256），天然阻断路径穿越；
- 任何读取失败都映射为统一错误体，不回显文件内容等敏感细节。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, Query, Response

from security_kit.path_guard import PathGuardError, safe_resolve
from trace_format.timeline import query_timeline
from trace_format.writer import JsonlTraceReader

from control_plane.errors import ControlPlaneError

#: 轨迹文件名白名单（kebab/点/下划线 + 固定 .jsonl 后缀，杜绝路径穿越）
_TRACE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.jsonl")

#: 报告文件名白名单（md/txt/json/yaml/jsonl）
_REPORT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(md|txt|json|yaml|yml|jsonl)")

#: 合法帧引用：64 位小写十六进制（sha256，与 FrameStore 同口径）
_FRAME_REF_RE = re.compile(r"[0-9a-f]{64}")

#: 事件端点单次返回条数上限（防一次拉爆内存/带宽）
MAX_EVENT_LIMIT: int = 5000

#: 事件端点默认返回条数
DEFAULT_EVENT_LIMIT: int = 500

#: 报告列表内联预览的最大字符数（完整内容走 content 端点）
REPORT_PREVIEW_CHARS: int = 2000

#: 轨迹目录名（相对项目目录）
TRACES_DIRNAME: str = "traces"

#: 回放报告目录名（相对项目目录）
REPLAY_DIRNAME: str = "tests/replay"


# ---------------------------------------------------------------------------
# 路径与目录辅助
# ---------------------------------------------------------------------------


def _iso_mtime(path: Path) -> str:
    """文件修改时间的 UTC ISO-8601 字符串（读取失败回退空串）。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return ""


def _traces_dir(store: Any, project_id: str) -> Path:
    """项目 traces 目录（先校验项目存在，目录不存在时自动视为空）。"""
    pdir = store.project_dir(project_id)
    return pdir / TRACES_DIRNAME


def _safe_trace_file(store: Any, project_id: str, name: str) -> Path:
    """解析轨迹文件路径：白名单 + safe_resolve 双重校验。

    Raises:
        ControlPlaneError: 422 名单非法/路径穿越；404 文件不存在。
    """
    if not isinstance(name, str) or not _TRACE_NAME_RE.fullmatch(name):
        raise ControlPlaneError(422, "invalid_trace_name", "轨迹文件名非法（须为 *.jsonl，禁止路径分隔符与 ..）")
    base = _traces_dir(store, project_id)
    if not base.is_dir():
        raise ControlPlaneError(404, "trace_not_found", f"轨迹不存在：{name}")
    try:
        candidate = safe_resolve(base, name)
    except PathGuardError as exc:
        raise ControlPlaneError(422, "path_rejected", f"路径校验被拒绝：{exc.rule_id}") from None
    if candidate.parent != base.resolve() or not candidate.is_file():
        raise ControlPlaneError(404, "trace_not_found", f"轨迹不存在：{name}")
    return candidate


def _safe_report_file(store: Any, project_id: str, name: str) -> Path:
    """解析回放报告文件路径（规则同 _safe_trace_file，白名单不同）。"""
    if not isinstance(name, str) or not _REPORT_NAME_RE.fullmatch(name):
        raise ControlPlaneError(422, "invalid_report_name", "报告文件名非法（md/txt/json/yaml/jsonl，禁止路径分隔符）")
    base = _replay_dir(store, project_id)
    if not base.is_dir():
        raise ControlPlaneError(404, "report_not_found", f"报告不存在：{name}")
    try:
        candidate = safe_resolve(base, name)
    except PathGuardError as exc:
        raise ControlPlaneError(422, "path_rejected", f"路径校验被拒绝：{exc.rule_id}") from None
    if candidate.parent != base.resolve() or not candidate.is_file():
        raise ControlPlaneError(404, "report_not_found", f"报告不存在：{name}")
    return candidate


def _replay_dir(store: Any, project_id: str) -> Path:
    """项目 tests/replay 目录（不存在视为空）。"""
    pdir = store.project_dir(project_id)
    return pdir / REPLAY_DIRNAME


def _count_events(path: Path) -> int:
    """事件数概览：非空行计数（轨迹为 JSONL，一行一事件；损坏尾部同样计入）。"""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return -1  # 读取失败以 -1 表示（概览不阻断列表）


# ---------------------------------------------------------------------------
# 事件与帧的读取/编码
# ---------------------------------------------------------------------------


def _parse_types(types: str | None) -> list[str] | None:
    """逗号分隔事件类型参数 -> 列表（空串/缺省返回 None 表示不过滤）。"""
    if not types:
        return None
    items = [item.strip() for item in types.split(",") if item.strip()]
    return items or None


def _event_dict(event: Any) -> dict[str, Any]:
    """TraceEvent -> 可 JSON 序列化的 dict（含链式哈希字段）。"""
    return event.to_dict(include_hash=True)


def _encode_png(pixels: np.ndarray) -> bytes:
    """像素数组编码为 PNG（RGB 输入转 OpenCV 期望的 BGR 通道序）。"""
    arr = np.ascontiguousarray(pixels)
    if arr.ndim == 2:
        ok, buf = cv2.imencode(".png", arr)
    else:
        ok, buf = cv2.imencode(".png", arr[:, :, :3][:, :, ::-1])
    if not ok:  # pragma: no cover - 正常输入不会触发
        raise RuntimeError("PNG 编码失败")
    return buf.tobytes()


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------


def register_trace_routes(app: FastAPI, config: Any = None) -> None:
    """注册轨迹/帧/回放报告只读路由。

    必须在 ``/projects/{pid}/{kind}`` 通配路由**之前**调用（否则 ``traces``
    会被当作对象种类）。config 仅用于取 project_root 的备用通道，常规
    数据访问走 ``app.state.store``。
    """

    store = app.state.store

    @app.get("/api/v1/projects/{project_id}/traces")
    async def list_traces(project_id: str) -> dict[str, Any]:
        base = _traces_dir(store, project_id)
        items: list[dict[str, Any]] = []
        if base.is_dir():
            for f in sorted(base.glob("*.jsonl")):
                if not f.is_file():
                    continue
                items.append(
                    {
                        "name": f.name,
                        "size_bytes": f.stat().st_size,
                        "modified_at": _iso_mtime(f),
                        "event_count": _count_events(f),
                    }
                )
        return {"project_id": project_id, "traces": items, "count": len(items)}

    @app.get("/api/v1/projects/{project_id}/traces/{name}/events")
    async def trace_events(
        project_id: str,
        name: str,
        since: float | None = Query(default=None, description="ts_monotonic 闭区间下界"),
        until: float | None = Query(default=None, description="ts_monotonic 闭区间上界"),
        types: str | None = Query(default=None, description="逗号分隔的事件类型"),
        state: str | None = Query(default=None, description="state_transition 的 from/to 任一匹配"),
        limit: int = Query(default=DEFAULT_EVENT_LIMIT, ge=1, le=MAX_EVENT_LIMIT, description="返回条数上限"),
    ) -> dict[str, Any]:
        path = _safe_trace_file(store, project_id, name)
        reader = JsonlTraceReader(path)
        events = reader.read()  # 尾部损坏：返回完好前缀 + truncated_tail
        filtered = query_timeline(
            events,
            since=since,
            until=until,
            types=_parse_types(types),
            state=state,
        )
        returned = filtered[: max(1, min(int(limit), MAX_EVENT_LIMIT))]
        return {
            "project_id": project_id,
            "trace": name,
            "events": [_event_dict(e) for e in returned],
            "returned": len(returned),
            "matched": len(filtered),
            "total": len(events),
            "truncated_tail": reader.truncated_tail,
            "error_count": len(reader.errors),
        }

    @app.get(
        "/api/v1/projects/{project_id}/traces/{name}/frame/{ref}",
        response_class=Response,
        responses={200: {"content": {"image/png": {}}}},
    )
    async def trace_frame(project_id: str, name: str, ref: str) -> Response:
        # name 先过同一套校验（防借道 frame 路径触达任意 jsonl 元数据）
        _safe_trace_file(store, project_id, name)
        if not isinstance(ref, str) or not _FRAME_REF_RE.fullmatch(ref):
            raise ControlPlaneError(422, "invalid_frame_ref", "帧引用非法（须为 64 位小写十六进制 sha256）")
        # FrameStore 目录布局：<项目>/traces/frames/frames/<ref>.npy（帧留痕由
        # runtime_engine.FrameStore / trace_format.FrameStore 落盘，ref 即 sha256）
        blob = _traces_dir(store, project_id) / "frames" / "frames" / f"{ref}.npy"
        resolved = blob.resolve()
        frames_root = (_traces_dir(store, project_id) / "frames" / "frames").resolve()
        if resolved.parent != frames_root:
            raise ControlPlaneError(422, "path_rejected", "帧路径越界")
        if not resolved.is_file():
            raise ControlPlaneError(404, "frame_not_found", f"帧引用不存在：{ref[:12]}…")
        try:
            pixels = np.load(resolved, allow_pickle=False)
        except (OSError, ValueError):
            raise ControlPlaneError(500, "corrupt_frame", "帧数据损坏，无法读取") from None
        return Response(content=_encode_png(pixels), media_type="image/png")

    @app.get("/api/v1/projects/{project_id}/replay-reports")
    async def list_replay_reports(project_id: str) -> dict[str, Any]:
        base = _replay_dir(store, project_id)
        items: list[dict[str, Any]] = []
        if base.is_dir():
            for f in sorted(base.glob("*")):
                if not f.is_file() or not _REPORT_NAME_RE.fullmatch(f.name):
                    continue
                preview = ""
                try:
                    preview = f.read_text(encoding="utf-8", errors="replace")[:REPORT_PREVIEW_CHARS]
                except OSError:
                    preview = ""
                items.append(
                    {
                        "name": f.name,
                        "rel_path": f.relative_to(base).as_posix(),
                        "size_bytes": f.stat().st_size,
                        "modified_at": _iso_mtime(f),
                        "preview": preview,
                    }
                )
        return {"project_id": project_id, "reports": items, "count": len(items)}

    @app.get("/api/v1/projects/{project_id}/replay-reports/{name}/content")
    async def replay_report_content(project_id: str, name: str) -> dict[str, Any]:
        path = _safe_report_file(store, project_id, name)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise ControlPlaneError(500, "corrupt_storage", "报告文件无法读取") from None
        return {"project_id": project_id, "name": name, "content": text, "size_bytes": path.stat().st_size}
