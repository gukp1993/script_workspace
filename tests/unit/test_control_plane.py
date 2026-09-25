"""E10 控制面单测（CTL-001~008、CTL-009 基础版）。

覆盖：
- 安全：令牌缺失/错误 401、恶意 Origin 403、非回环 Host 403、健康检查放行；
- CRUD：项目/目标/策略/检测器/状态机往返一致、非法载荷 422（含规则 ID）、
  乐观锁 409 与版本递增；
- 会话：shadow 直接启动、real_input 人工闸门、全局单实例、幂等停止、
  暂停/恢复、受保护目标拒绝；
- WS：令牌必需、seq 单调、断线重连补发、缓冲溢出 resync 与 REST 兜底；
- 资产：上传去重、路径穿越拒绝、超限 413、非法扩展 415；
- 恢复：遗留 running 会话启动后被标记 interrupted；
- 设置：安全默认值与受保护目标硬约束。

全部用例基于 fastapi.testclient.TestClient + tmp_path，无真实网络监听。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from control_plane.app import create_app
from control_plane.config import ControlPlaneConfig

#: 测试令牌与端口（Origin/Host 校验依赖端口一致）
TOKEN = "unit-test-token"
PORT = 17653
HEADERS = {"X-VAW-Token": TOKEN}
BASE_URL = f"http://127.0.0.1:{PORT}"

#: 合法领域对象载荷（与 domain_model 校验器对齐）
VALID_TARGET: dict[str, Any] = {
    "schema_version": 1,
    "target_id": "arena-lab",
    "executable": "ArenaLab.exe",
    "title_regex": "^ArenaLab",
    "protected_online": False,
    "allowed_display_modes": ["windowed"],
    "notes": "",
}
VALID_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "policy_id": "default",
    "mode": "shadow",
    "require_manual_start": True,
    "max_runtime_minutes": 20,
    "max_actions_per_minute": 120,
}
VALID_DETECTOR: dict[str, Any] = {
    "schema_version": 1,
    "detector_id": "health_bar",
    "type": "color_bar_ratio",
    "roi": [0.04, 0.04, 0.22, 0.025],
    "threshold": 0.5,
    "stable_frames": 1,
    "field_name": "health_ratio",
}
VALID_MACHINE: dict[str, Any] = {
    "schema_version": 1,
    "machine_id": "main",
    "initial": "idle",
    "states": {"idle": {"terminal": True}},
}


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """构建应用与测试客户端（小事件缓冲便于触发 resync；资产上限 64B）。"""
    config = ControlPlaneConfig(
        project_root=tmp_path,
        token=TOKEN,
        port=PORT,
        event_buffer_size=8,
        asset_max_bytes=64,
    )
    with TestClient(create_app(config), base_url=BASE_URL) as client:
        yield SimpleNamespace(client=client, config=config, root=tmp_path)


# ---------------------------------------------------------------------------
# 夹具辅助
# ---------------------------------------------------------------------------


def _make_project(c: TestClient, project_id: str = "demo", name: str = "Demo") -> dict[str, Any]:
    r = c.post("/api/v1/projects", json={"project_id": project_id, "name": name}, headers=HEADERS)
    assert r.status_code == 201, r.text
    return r.json()


def _make_target(c: TestClient, project_id: str = "demo", target_id: str = "arena-lab", *, protected: bool = False) -> None:
    payload = {**VALID_TARGET, "target_id": target_id, "protected_online": protected}
    r = c.post(f"/api/v1/projects/{project_id}/targets", json=payload, headers=HEADERS)
    assert r.status_code == 201, r.text


def _make_session(c: TestClient, mode: str = "shadow", project_id: str = "demo", target_id: str = "arena-lab") -> dict[str, Any]:
    r = c.post(
        "/api/v1/sessions",
        json={"project_id": project_id, "target_id": target_id, "mode": mode},
        headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _detail(r: Any) -> dict[str, Any]:
    """提取统一错误体 detail。"""
    body = r.json()
    assert isinstance(body.get("detail"), dict), body
    return body["detail"]


# ---------------------------------------------------------------------------
# 安全（CTL-001/002）
# ---------------------------------------------------------------------------


def test_health_without_token_401(env: SimpleNamespace) -> None:
    r = env.client.get("/api/v1/health")
    assert r.status_code == 401
    # 响应体不泄露令牌等敏感细节
    assert TOKEN not in r.text


def test_health_wrong_token_401(env: SimpleNamespace) -> None:
    r = env.client.get("/api/v1/health", headers={"X-VAW-Token": "wrong-token"})
    assert r.status_code == 401
    assert TOKEN not in r.text


def test_malicious_origin_403(env: SimpleNamespace) -> None:
    r = env.client.get("/api/v1/health", headers={**HEADERS, "Origin": "http://evil.example.com"})
    assert r.status_code == 403
    assert _detail(r)["error"] == "forbidden_origin"


def test_non_loopback_host_403(env: SimpleNamespace) -> None:
    r = env.client.get("/api/v1/health", headers={**HEADERS, "Host": "evil.example.com"})
    assert r.status_code == 403
    assert _detail(r)["error"] == "forbidden_host"


def test_health_with_token_200(env: SimpleNamespace) -> None:
    c = env.client
    r = c.get("/api/v1/health", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["projects"] == 0
    assert body["sessions"] == 0
    # 允许列表内的 Origin 不被拒绝（浏览器同源调用）
    ok = c.get("/api/v1/health", headers={**HEADERS, "Origin": f"http://127.0.0.1:{PORT}"})
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# CRUD 与乐观锁（CTL-003）
# ---------------------------------------------------------------------------


def test_project_crud_roundtrip(env: SimpleNamespace) -> None:
    c = env.client
    project = _make_project(c)
    assert project["project_id"] == "demo"
    _make_target(c)
    for kind, payload in (("policies", VALID_POLICY), ("detectors", VALID_DETECTOR), ("machines", VALID_MACHINE)):
        r = c.post(f"/api/v1/projects/demo/{kind}", json=payload, headers=HEADERS)
        assert r.status_code == 201, r.text
        assert r.json()["meta"]["version"] == 1

    # 项目计数
    r = c.get("/api/v1/projects/demo", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["counts"] == {
        "targets": 1,
        "policies": 1,
        "detectors": 1,
        "machines": 1,
        "calibrations": 0,
    }

    # 列表清单
    r = c.get("/api/v1/projects/demo/detectors", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["objects"][0]["detector_id"] == "health_bar"

    # 单读往返一致（除 meta 外与提交载荷一致）
    r = c.get("/api/v1/projects/demo/detectors/health_bar", headers=HEADERS)
    assert r.status_code == 200
    obj = r.json()
    for key, value in VALID_DETECTOR.items():
        assert obj[key] == value, key
    assert obj["meta"]["version"] == 1
    assert "updated_at" in obj["meta"]


def test_invalid_payload_422_with_rule_id(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    bad = {**VALID_DETECTOR, "roi": [0.5, 0.0, 0.6, 0.2]}  # x+w=1.1 越界
    r = c.post("/api/v1/projects/demo/detectors", json=bad, headers=HEADERS)
    assert r.status_code == 422
    detail = _detail(r)
    assert detail["error"] == "validation_failed"
    issues = detail["issues"]
    assert any(i["rule"] == "roi_out_of_bounds" and i["pointer"].startswith("/roi") for i in issues)
    # 校验失败不落盘
    assert c.get("/api/v1/projects/demo/detectors", headers=HEADERS).json()["count"] == 0


def test_version_conflict_409(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    # 未携带版本 → 409（PUT 必须带匹配 version）
    r = c.put("/api/v1/projects/demo/targets/arena-lab", json=VALID_TARGET, headers=HEADERS)
    assert r.status_code == 409
    detail = _detail(r)
    assert detail["error"] == "version_conflict"
    assert detail["current_version"] == 1
    # 版本不匹配 → 409
    r = c.put(
        "/api/v1/projects/demo/targets/arena-lab",
        json={"meta": {"version": 99}, **VALID_TARGET, "notes": "x"},
        headers=HEADERS,
    )
    assert r.status_code == 409
    assert _detail(r)["expected_version"] == 99


def test_version_increments_on_update(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    url = "/api/v1/projects/demo/targets/arena-lab"
    r = c.put(url, json={"meta": {"version": 1}, **VALID_TARGET, "notes": "v2"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["meta"]["version"] == 2
    assert r.json()["notes"] == "v2"
    r = c.put(url, json={"meta": {"version": 2}, **VALID_TARGET, "notes": "v3"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["meta"]["version"] == 3
    # 再读一致
    assert c.get(url, headers=HEADERS).json()["meta"]["version"] == 3


def test_duplicate_object_409(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    r = c.post("/api/v1/projects/demo/targets", json=VALID_TARGET, headers=HEADERS)
    assert r.status_code == 409
    assert _detail(r)["error"] == "duplicate_id"


def test_project_id_traversal_rejected_422(env: SimpleNamespace) -> None:
    r = env.client.post("/api/v1/projects", json={"project_id": "../evil", "name": "X"}, headers=HEADERS)
    assert r.status_code == 422
    assert _detail(r)["error"] == "invalid_project_id"


# ---------------------------------------------------------------------------
# 会话生命周期（CTL-005/006）
# ---------------------------------------------------------------------------


def test_shadow_session_start_direct(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    session = _make_session(c, mode="shadow")
    assert session["state"] == "created"
    r = c.post(f"/api/v1/sessions/{session['session_id']}/start", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["state"] == "running"
    # 未知模式 422
    r = c.post(
        "/api/v1/sessions",
        json={"project_id": "demo", "target_id": "arena-lab", "mode": "turbo"},
        headers=HEADERS,
    )
    assert r.status_code == 422
    assert _detail(r)["error"] == "invalid_mode"


def test_real_input_manual_gate_flow(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    session = _make_session(c, mode="real_input")
    sid = session["session_id"]

    # 未确认直接 start → 409 manual_gate_required
    r = c.post(f"/api/v1/sessions/{sid}/start", headers=HEADERS)
    assert r.status_code == 409
    assert _detail(r)["error"] == "manual_gate_required"

    # 空 operator → 422
    r = c.post(f"/api/v1/sessions/{sid}/confirm", json={"operator": "   "}, headers=HEADERS)
    assert r.status_code == 422
    assert _detail(r)["error"] == "operator_required"

    # 确认 → start 成功，审计记录持久化
    r = c.post(f"/api/v1/sessions/{sid}/confirm", json={"operator": "op-tester"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["gate"]["operator"] == "op-tester"
    r = c.post(f"/api/v1/sessions/{sid}/start", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["state"] == "running"
    fetched = c.get(f"/api/v1/sessions/{sid}", headers=HEADERS).json()
    assert fetched["gate"]["operator"] == "op-tester"
    assert any(h["event"] == "session_gate_confirmed" for h in fetched["history"])


def test_second_real_input_session_409(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    first = _make_session(c, mode="real_input")
    # 第二个 real_input 会话（活跃中）→ 409 全局单实例
    r = c.post(
        "/api/v1/sessions",
        json={"project_id": "demo", "target_id": "arena-lab", "mode": "real_input"},
        headers=HEADERS,
    )
    assert r.status_code == 409
    detail = _detail(r)
    assert detail["error"] == "real_input_session_exists"
    assert detail["conflicting_session"] == first["session_id"]
    # shadow 模式不受单实例约束影响
    shadow = _make_session(c, mode="shadow")
    assert shadow["state"] == "created"


def test_stop_is_idempotent(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    session = _make_session(c, mode="shadow")
    sid = session["session_id"]
    assert c.post(f"/api/v1/sessions/{sid}/start", headers=HEADERS).json()["state"] == "running"
    r1 = c.post(f"/api/v1/sessions/{sid}/stop", headers=HEADERS)
    assert r1.status_code == 200
    assert r1.json()["state"] == "stopped"
    # 重复 stop：无异常，状态保持
    r2 = c.post(f"/api/v1/sessions/{sid}/stop", headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["state"] == "stopped"


def test_pause_resume_state_machine(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    sid = _make_session(c, mode="shadow")["session_id"]
    assert c.post(f"/api/v1/sessions/{sid}/start", headers=HEADERS).json()["state"] == "running"
    # created 不可暂停
    created_sid = _make_session(c, mode="shadow")["session_id"]
    r = c.post(f"/api/v1/sessions/{created_sid}/pause", headers=HEADERS)
    assert r.status_code == 409

    r = c.post(f"/api/v1/sessions/{sid}/pause", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["state"] == "paused"
    # pause 幂等
    r = c.post(f"/api/v1/sessions/{sid}/pause", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["state"] == "paused"
    r = c.post(f"/api/v1/sessions/{sid}/resume", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["state"] == "resumed"
    # resumed 也可再次暂停
    r = c.post(f"/api/v1/sessions/{sid}/pause", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["state"] == "paused"
    r = c.post(f"/api/v1/sessions/{sid}/resume", headers=HEADERS)
    assert r.status_code == 200
    r = c.post(f"/api/v1/sessions/{sid}/stop", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["state"] == "stopped"


def test_real_input_on_protected_target_refused(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c, "prot")
    _make_target(c, "prot", "online-shop", protected=True)
    r = c.post(
        "/api/v1/sessions",
        json={"project_id": "prot", "target_id": "online-shop", "mode": "real_input"},
        headers=HEADERS,
    )
    assert r.status_code == 409
    assert _detail(r)["error"] == "protected_online_no_real_input"


# ---------------------------------------------------------------------------
# WebSocket 事件总线（CTL-007）
# ---------------------------------------------------------------------------

# starlette 的 websocket_connect 硬编码 ws://testserver（不使用 base_url），
# 测试需显式携带回环 Host 头；真实 uvicorn 下 Host 天然是 127.0.0.1:<port>。
WS_HEADERS = {"host": f"127.0.0.1:{PORT}"}


def _ws_url(query: str = "") -> str:
    """构造带查询串的 WS 端点 URL。"""
    return f"/api/v1/ws{query}"


def test_ws_token_required(env: SimpleNamespace) -> None:
    c = env.client
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(_ws_url(), headers=WS_HEADERS):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(_ws_url("?token=wrong-token"), headers=WS_HEADERS):
            pass


def test_ws_events_seq_monotonic_and_heartbeat(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    with c.websocket_connect(_ws_url(f"?token={TOKEN}&last_seq=0"), headers=WS_HEADERS) as ws:
        session = _make_session(c)  # 触发 session_created 事件
        ev_started = ws.receive_json()  # 启动事件（lifespan 发布）
        ev_created = ws.receive_json()
        assert ev_started["type"] == "control_plane_started"
        assert ev_created["type"] == "session_created"
        assert ev_created["session_id"] == session["session_id"]
        assert ev_created["seq"] == ev_started["seq"] + 1  # 全局单调
        # 心跳
        ws.send_json({"ping": 1})
        pong = ws.receive_json()
        assert pong["type"] == "pong"


def test_ws_reconnect_replays_missing(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    latest = c.get("/api/v1/events", params={"since_seq": 0}, headers=HEADERS).json()["latest_seq"]
    with c.websocket_connect(_ws_url(f"?token={TOKEN}&last_seq={latest}"), headers=WS_HEADERS) as ws:
        first = _make_session(c)
        ev1 = ws.receive_json()
        assert ev1["type"] == "session_created"
        assert ev1["session_id"] == first["session_id"]
        last = ev1["seq"]
        second = _make_session(c)
        ev2 = ws.receive_json()
        assert ev2["seq"] == last + 1  # seq 单调
        assert ev2["session_id"] == second["session_id"]
    # 断线期间又产生一条事件
    third = _make_session(c)
    # 带 last_seq 重连：补发缺失事件（断线前的第二条 + 断线期间的第三条）
    with c.websocket_connect(_ws_url(f"?token={TOKEN}&last_seq={last}"), headers=WS_HEADERS) as ws:
        back1 = ws.receive_json()
        back2 = ws.receive_json()
        assert back1["seq"] == last + 1
        assert back1["type"] == "session_created"
        assert back1["session_id"] == second["session_id"]
        assert back2["seq"] == last + 2
        assert back2["session_id"] == third["session_id"]


def test_ws_resync_on_buffer_overflow(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    _make_target(c)
    # 缓冲 8 条：启动事件(1) + 10 个会话事件(2..11) → 窗口起点为 4
    for _ in range(10):
        _make_session(c)
    # WS：last_seq=1 已超出缓冲窗口 → 收到 resync
    with c.websocket_connect(_ws_url(f"?token={TOKEN}&last_seq=1"), headers=WS_HEADERS) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "resync"
        assert frame["payload"]["reason"] == "buffer_overflow"
    # REST 兜底：同样要求 resync；从窗口内拉取则返回连续事件
    r = c.get("/api/v1/events", params={"since_seq": 1}, headers=HEADERS)
    body = r.json()
    assert body["resync_required"] is True
    assert body["events"] == []
    r = c.get("/api/v1/events", params={"since_seq": 9}, headers=HEADERS)
    body = r.json()
    assert body["resync_required"] is False
    assert [e["seq"] for e in body["events"]] == [10, 11]


# ---------------------------------------------------------------------------
# 视觉资产（CTL-004）
# ---------------------------------------------------------------------------


def test_asset_upload_dedup_and_roundtrip(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    content = b"\x89PNG\r\n\x1a\nfake-image-payload"
    sha = hashlib.sha256(content).hexdigest()

    r = c.post(
        "/api/v1/projects/demo/assets",
        params={"path": "assets/templates/btn.png", "kind": "template"},
        content=content,
        headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    entry = r.json()
    assert entry["sha256"] == sha
    assert entry["deduplicated"] is False
    assert (env.root / "assets-store" / f"{sha}.png").is_file()

    # 相同内容、不同 asset_id：内容去重（库中仍只有一个文件）
    r = c.post(
        "/api/v1/projects/demo/assets",
        params={"path": "assets/templates/btn2.png", "kind": "template", "asset_id": "btn-two"},
        content=content,
        headers=HEADERS,
    )
    assert r.status_code == 201
    assert r.json()["deduplicated"] is True
    assert r.json()["sha256"] == sha
    assert len(list((env.root / "assets-store").iterdir())) == 1

    # 重复 asset_id → 409
    r = c.post(
        "/api/v1/projects/demo/assets",
        params={"path": "assets/templates/btn3.png", "asset_id": "btn-two"},
        content=b"other-bytes",
        headers=HEADERS,
    )
    assert r.status_code == 409

    # 清单与内容回读
    listing = c.get("/api/v1/projects/demo/assets", headers=HEADERS).json()
    assert listing["count"] == 2
    r = c.get("/api/v1/projects/demo/assets/btn-two/content", headers=HEADERS)
    assert r.status_code == 200
    assert r.content == content


def test_asset_path_traversal_rejected(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    for evil in ("../evil.png", "assets/../../evil.png", "/abs/evil.png", "C:/evil.png"):
        r = c.post("/api/v1/projects/demo/assets", params={"path": evil}, content=b"x", headers=HEADERS)
        assert r.status_code == 403, evil
        assert _detail(r)["error"] == "path_escape"


def test_asset_too_large_413(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    r = c.post(
        "/api/v1/projects/demo/assets",
        params={"path": "assets/templates/big.png"},
        content=b"x" * 65,  # 配置上限 64 字节
        headers=HEADERS,
    )
    assert r.status_code == 413
    assert _detail(r)["error"] == "payload_too_large"


def test_asset_bad_extension_415(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c)
    r = c.post(
        "/api/v1/projects/demo/assets",
        params={"path": "assets/templates/btn.exe"},
        content=b"x",
        headers=HEADERS,
    )
    assert r.status_code == 415
    assert _detail(r)["error"] == "unsupported_media_type"


# ---------------------------------------------------------------------------
# 启动恢复（CTL-009 基础版）
# ---------------------------------------------------------------------------


def test_recovery_marks_running_session_interrupted(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    legacy = {
        "session_id": "sess-recover1",
        "project_id": "demo",
        "target_id": "arena-lab",
        "mode": "shadow",
        "state": "running",
        "created_at": "2026-09-24T00:00:00.000+00:00",
        "updated_at": "2026-09-24T00:00:00.000+00:00",
        "gate": None,
        "history": [],
    }
    (sessions_dir / "sess-recover1.json").write_text(json.dumps(legacy), encoding="utf-8")

    config = ControlPlaneConfig(project_root=tmp_path, token=TOKEN, port=PORT)
    with TestClient(create_app(config), base_url=BASE_URL) as client:
        r = client.get("/api/v1/sessions/sess-recover1", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "interrupted"
        assert any(h["event"] == "session_interrupted" for h in body["history"])
        # 恢复事件已发布
        events = client.get("/api/v1/events", params={"since_seq": 0}, headers=HEADERS).json()["events"]
        assert any(e["type"] == "session_interrupted" for e in events)


# ---------------------------------------------------------------------------
# 设置与安全默认值（CTL-008）
# ---------------------------------------------------------------------------


def test_settings_defaults(env: SimpleNamespace) -> None:
    r = env.client.get("/api/v1/settings", headers=HEADERS)
    assert r.status_code == 200
    assert r.json() == {
        "default_mode": "shadow",
        "trace_retention_days": 7,
        "unattended_schedule": "disabled",
    }


def test_settings_update_and_validation(env: SimpleNamespace) -> None:
    c = env.client
    r = c.put("/api/v1/settings", json={"trace_retention_days": 14}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json() == {
        "default_mode": "shadow",
        "trace_retention_days": 14,
        "unattended_schedule": "disabled",
    }
    # 已持久化
    assert c.get("/api/v1/settings", headers=HEADERS).json()["trace_retention_days"] == 14
    # 非法值
    r = c.put("/api/v1/settings", json={"trace_retention_days": 0}, headers=HEADERS)
    assert r.status_code == 422
    r = c.put("/api/v1/settings", json={"unknown_key": True}, headers=HEADERS)
    assert r.status_code == 422
    assert _detail(r)["error"] == "unknown_field"
    r = c.put("/api/v1/settings", json={"default_mode": "turbo"}, headers=HEADERS)
    assert r.status_code == 422
    assert _detail(r)["error"] == "invalid_enum"


def test_settings_protected_real_input_rejected(env: SimpleNamespace) -> None:
    c = env.client
    _make_project(c, "prot")
    _make_target(c, "prot", "online-shop", protected=True)
    # 存在受保护目标时：default_mode=real_input 被拒
    r = c.put("/api/v1/settings", json={"default_mode": "real_input"}, headers=HEADERS)
    assert r.status_code == 409
    assert _detail(r)["error"] == "protected_online_no_real_input"
    # 开启无人值守调度同样被拒
    r = c.put("/api/v1/settings", json={"unattended_schedule": "enabled"}, headers=HEADERS)
    assert r.status_code == 409
    # 拒绝路径不落盘：默认值保持安全
    settings = c.get("/api/v1/settings", headers=HEADERS).json()
    assert settings["default_mode"] == "shadow"
    assert settings["unattended_schedule"] == "disabled"
