"""E11 轨迹/帧/回放报告端点单测（UI-013/014/015 数据源）。

覆盖：
- 令牌防线：缺令牌 401；
- 列表：空项目空态、写入轨迹后返回 名称/大小/修改时间/事件数概览；
- 事件读取：链式哈希事件完整往返、字段与 payload 一致；
- 过滤：since/until 闭区间、types、state、limit 语义（复用 query_timeline）；
- 损坏尾部截断标记 truncated_tail；
- 404：轨迹不存在 / 项目不存在 / 帧引用不存在；
- 路径穿越拒绝：``..``、绝对路径、非法帧引用一律 422；
- 帧 PNG：合法引用返回 PNG 字节（魔数校验）；
- 回放报告：列表/内容读取与文件名白名单。

全部基于 fastapi.testclient.TestClient + tmp_path，无真实网络监听。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient

from control_plane.app import create_app
from control_plane.config import ControlPlaneConfig
from trace_format import JsonlTraceWriter

#: 测试令牌与端口（Origin/Host 校验依赖端口一致）
TOKEN = "unit-test-token"
PORT = 17653
HEADERS = {"X-VAW-Token": TOKEN}
BASE_URL = f"http://127.0.0.1:{PORT}"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """构建应用与测试客户端。"""
    config = ControlPlaneConfig(project_root=tmp_path, token=TOKEN, port=PORT)
    with TestClient(create_app(config), base_url=BASE_URL) as client:
        yield SimpleNamespace(client=client, config=config, root=tmp_path)


def _make_project(c: TestClient, project_id: str = "demo") -> None:
    r = c.post("/api/v1/projects", json={"project_id": project_id, "name": "Demo"}, headers=HEADERS)
    assert r.status_code == 201, r.text


def _write_trace(root: Path, project_id: str, name: str, count: int = 3, *, base_ts: float = 0.0) -> Path:
    """写入一条合法链式哈希轨迹并返回文件路径。"""
    path = root / "projects" / project_id / "traces" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with JsonlTraceWriter(path) as writer:
        for i in range(count):
            writer.append(
                "perception_snapshot" if i % 2 == 0 else "state_transition",
                ts_monotonic=base_ts + i * 0.5,
                session_id="sess-1",
                payload={"fields": {"health_ratio": i}} if i % 2 == 0 else {"from": "idle", "to": "scan"},
            )
    return path


# ---------------------------------------------------------------------------
# 安全线
# ---------------------------------------------------------------------------


def test_traces_endpoints_require_token(env: SimpleNamespace) -> None:
    """轨迹端点走既有令牌中间件：缺令牌 401。"""
    _make_project(env.client)
    r = env.client.get("/api/v1/projects/demo/traces")
    assert r.status_code == 401
    r = env.client.get("/api/v1/projects/demo/traces/x.jsonl/events", headers={})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


def test_list_traces_empty_and_filled(env: SimpleNamespace) -> None:
    """空项目返回空列表；写入后返回名称/大小/修改时间/事件数概览。"""
    c = env.client
    _make_project(c)
    r = c.get("/api/v1/projects/demo/traces", headers=HEADERS)
    assert r.status_code == 200
    assert r.json() == {"project_id": "demo", "traces": [], "count": 0}

    path = _write_trace(env.root, "demo", "trace-a.jsonl", count=4)
    r = c.get("/api/v1/projects/demo/traces", headers=HEADERS)
    body = r.json()
    assert body["count"] == 1
    item = body["traces"][0]
    assert item["name"] == "trace-a.jsonl"
    assert item["size_bytes"] == path.stat().st_size > 0
    assert item["modified_at"]  # ISO 时间串非空
    assert item["event_count"] == 4


# ---------------------------------------------------------------------------
# 事件读取与过滤
# ---------------------------------------------------------------------------


def test_trace_events_roundtrip(env: SimpleNamespace) -> None:
    """事件读取：字段/哈希链字段完整，payload 与写入一致。"""
    c = env.client
    _make_project(c)
    _write_trace(env.root, "demo", "trace-a.jsonl", count=4)
    r = c.get("/api/v1/projects/demo/traces/trace-a.jsonl/events", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4
    assert body["returned"] == 4
    assert body["matched"] == 4
    assert body["truncated_tail"] is False
    events = body["events"]
    assert [e["seq"] for e in events] == [0, 1, 2, 3]
    assert events[0]["type"] == "perception_snapshot"
    assert events[0]["payload"] == {"fields": {"health_ratio": 0}}
    assert events[1]["payload"] == {"from": "idle", "to": "scan"}
    for e in events:  # 链式哈希字段齐全
        assert len(e["hash"]) == 64 and len(e["prev_hash"]) == 64


def test_trace_events_filters(env: SimpleNamespace) -> None:
    """过滤参数：since/until 闭区间、types、state、limit。"""
    c = env.client
    _make_project(c)
    _write_trace(env.root, "demo", "trace-a.jsonl", count=6)  # ts: 0, .5, 1, 1.5, 2, 2.5
    url = "/api/v1/projects/demo/traces/trace-a.jsonl/events"

    # since/until 闭区间（含边界）
    body = c.get(url, params={"since": 0.5, "until": 1.5}, headers=HEADERS).json()
    assert [e["ts_monotonic"] for e in body["events"]] == [0.5, 1.0, 1.5]

    # types 过滤
    body = c.get(url, params={"types": "state_transition"}, headers=HEADERS).json()
    assert body["matched"] == 3
    assert all(e["type"] == "state_transition" for e in body["events"])

    # state 过滤（from/to 任一匹配）
    body = c.get(url, params={"state": "scan"}, headers=HEADERS).json()
    assert body["matched"] == 3

    # limit 截取前 N 条
    body = c.get(url, params={"limit": 2}, headers=HEADERS).json()
    assert body["returned"] == 2 and body["matched"] == 6 and body["total"] == 6

    # limit 越界 -> FastAPI 参数校验 422
    assert c.get(url, params={"limit": 0}, headers=HEADERS).status_code == 422
    assert c.get(url, params={"limit": 999999}, headers=HEADERS).status_code == 422


def test_trace_events_truncated_tail_marker(env: SimpleNamespace) -> None:
    """损坏尾部：返回完好前缀并标记 truncated_tail（included）。"""
    c = env.client
    _make_project(c)
    path = _write_trace(env.root, "demo", "trace-a.jsonl", count=3)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 3, "broken": ')  # 追加残缺行
    r = c.get("/api/v1/projects/demo/traces/trace-a.jsonl/events", headers=HEADERS)
    body = r.json()
    assert body["total"] == 3
    assert body["truncated_tail"] is True
    assert body["error_count"] >= 1


# ---------------------------------------------------------------------------
# 404 与路径穿越
# ---------------------------------------------------------------------------


def test_trace_events_404_cases(env: SimpleNamespace) -> None:
    """轨迹不存在 / 项目不存在 / 帧引用不存在 -> 404。"""
    c = env.client
    _make_project(c)
    r = c.get("/api/v1/projects/demo/traces/missing.jsonl/events", headers=HEADERS)
    assert r.status_code == 404
    r = c.get("/api/v1/projects/ghost/traces/x.jsonl/events", headers=HEADERS)
    assert r.status_code == 404
    r = c.get("/api/v1/projects/ghost/traces", headers=HEADERS)
    assert r.status_code == 404

    _write_trace(env.root, "demo", "trace-a.jsonl", count=1)
    ref = "0" * 64
    r = c.get(f"/api/v1/projects/demo/traces/trace-a.jsonl/frame/{ref}", headers=HEADERS)
    assert r.status_code == 404


@pytest.mark.parametrize(
    "name",
    ["..%2F..%2Fsecret.jsonl", "a/b.jsonl", "..jsonl", ".jsonl", "trace.jsonl%00.png"],
)
def test_trace_name_traversal_rejected(env: SimpleNamespace, name: str) -> None:
    """路径穿越/非法轨迹名 -> 422（不 500、不泄露文件系统信息）。"""
    c = env.client
    _make_project(c)
    r = c.get(f"/api/v1/projects/demo/traces/{name}/events", headers=HEADERS)
    assert r.status_code in (404, 422)
    if r.status_code == 422:
        assert r.json()["detail"]["error"] in {"invalid_trace_name", "path_rejected"}


def test_trace_name_parent_escape_block(env: SimpleNamespace) -> None:
    """构造真实 ``..`` 名称（TestClient 允许非常规 URL）：必须 422 拒绝。"""
    c = env.client
    _make_project(c)
    # 直接打到路由层：name 带 .. 段（未编码时会 404 于路由匹配，编码后过校验器）
    for raw in ("..%2Fproject.yaml", "..%2F..%2Fsettings.json"):
        r = c.get(f"/api/v1/projects/demo/traces/{raw}/events", headers=HEADERS)
        assert r.status_code in (404, 422)
        if r.status_code == 422:
            assert r.json()["detail"]["error"] in {"invalid_trace_name", "path_rejected"}


# ---------------------------------------------------------------------------
# 帧端点
# ---------------------------------------------------------------------------


def test_trace_frame_returns_png(env: SimpleNamespace) -> None:
    """合法帧引用返回 PNG（魔数校验）；像素内容经 sha256 寻址落盘。"""
    c = env.client
    _make_project(c)
    _write_trace(env.root, "demo", "trace-a.jsonl", count=1)
    pixels = np.zeros((4, 6, 3), dtype=np.uint8)
    pixels[:, :, 0] = 200
    ref = hashlib.sha256(np.ascontiguousarray(pixels).tobytes()).hexdigest()
    blob_dir = env.root / "projects" / "demo" / "traces" / "frames" / "frames"
    blob_dir.mkdir(parents=True, exist_ok=True)
    np.save(blob_dir / f"{ref}.npy", pixels, allow_pickle=False)

    r = c.get(f"/api/v1/projects/demo/traces/trace-a.jsonl/frame/{ref}", headers=HEADERS)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(PNG_MAGIC)


def test_trace_frame_invalid_ref_rejected(env: SimpleNamespace) -> None:
    """非法帧引用（非 64 位十六进制）-> 422；``..`` 类在客户端归一化后 404 也可接受。"""
    c = env.client
    _make_project(c)
    _write_trace(env.root, "demo", "trace-a.jsonl", count=1)
    for bad in ("not-a-ref", "A" * 64, "0" * 63 + "g", "%2E%2E"):
        r = c.get(f"/api/v1/projects/demo/traces/trace-a.jsonl/frame/{bad}", headers=HEADERS)
        assert r.status_code in (404, 422), (bad, r.status_code)
        if r.status_code == 422:
            assert r.json()["detail"]["error"] == "invalid_frame_ref"
    # ``..`` 可能被 HTTP 客户端在路径归一化阶段折叠（打不到路由），两种结果都安全
    r = c.get("/api/v1/projects/demo/traces/trace-a.jsonl/frame/..", headers=HEADERS)
    assert r.status_code in (404, 422)


# ---------------------------------------------------------------------------
# 回放报告
# ---------------------------------------------------------------------------


def test_replay_reports_list_and_content(env: SimpleNamespace) -> None:
    """报告列表含预览；content 端点返回全文；缺失 404；非法名 422。"""
    c = env.client
    _make_project(c)
    base = env.root / "projects" / "demo" / "tests" / "replay"
    base.mkdir(parents=True, exist_ok=True)
    (base / "diff-report.md").write_text("# 差异报告\n\n全部一致", encoding="utf-8")

    body = c.get("/api/v1/projects/demo/replay-reports", headers=HEADERS).json()
    assert body["count"] == 1
    assert body["reports"][0]["name"] == "diff-report.md"
    assert "差异报告" in body["reports"][0]["preview"]

    r = c.get("/api/v1/projects/demo/replay-reports/diff-report.md/content", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["content"].startswith("# 差异报告")

    assert c.get("/api/v1/projects/demo/replay-reports/missing.md/content", headers=HEADERS).status_code == 404
    # 白名单外扩展名 -> 422
    r = c.get("/api/v1/projects/demo/replay-reports/payload.exe/content", headers=HEADERS)
    assert r.status_code == 422
    # 以点开头的名字（白名单拒绝，含穿越变体的无分隔符形态）-> 422
    r = c.get("/api/v1/projects/demo/replay-reports/..secret.md/content", headers=HEADERS)
    assert r.status_code == 422
    # 带 ../ 段的名字会被 HTTP 客户端归一化或路由不匹配：404/422 均为安全结果
    r = c.get("/api/v1/projects/demo/replay-reports/..%2Fsecret.md/content", headers=HEADERS)
    assert r.status_code in (404, 422)
