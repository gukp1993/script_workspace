"""运行会话生命周期管理与单实例约束（CTL-005/006）。

状态机::

    created ──start──▶ running ──pause──▶ paused ──resume──▶ resumed
                          ▲                  │                   │
                          └─────── resume ───┘        pause ──────┘
    running / paused / resumed ──stop──▶ stopped
    running / paused / resumed ──（异常）──▶ failed
    任意非终态 ──（启动恢复）──▶ interrupted
    终态：stopped / failed / interrupted

安全约束：
- ``real_input`` 会话必须先 ``confirm(session_id, operator)`` 通过人工闸门，
  审计记录写入会话文件；未确认 start → 409 ``manual_gate_required``；
- **全局只允许一个活跃 RealInput 会话**（CTL-006 / SAFE-019）：
  第二个 real_input 会话创建即 409 ``real_input_session_exists``；
- 受保护在线目标创建 real_input 会话直接 409 ``protected_online_no_real_input``
  （与 ModeGate / evaluator 的硬锁构成纵深防御）；
- ``pause`` / ``resume`` / ``stop`` 幂等：重复调用返回当前记录不报错。

持久化：每个会话一个 JSON 文件 ``<project_root>/sessions/<session_id>.json``，
含完整状态、人工闸门审计记录与迁移历史。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from common.clock import Clock, MonotonicClock
from common.ids import new_session_id
from policy_engine.modes import ModeGate, RunMode, parse_mode
from policy_engine.models import TargetRef

from control_plane.errors import ControlPlaneError
from control_plane.events import EventBroker
from control_plane.storage import ProjectStore
from control_plane.timeutil import utc_now_iso

#: 会话 ID 白名单（防路径穿越）
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

#: 终态集合
TERMINAL_STATES = frozenset({"stopped", "failed", "interrupted"})

#: 活跃（非终态）集合
ACTIVE_STATES = frozenset({"created", "running", "paused", "resumed"})

#: 状态迁移表（pause/resume/stop 的幂等性在方法层处理，不在此表）
TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"running"}),
    "running": frozenset({"paused", "stopped", "failed", "interrupted"}),
    "paused": frozenset({"resumed", "stopped", "failed", "interrupted"}),
    "resumed": frozenset({"paused", "stopped", "failed", "interrupted"}),
    "stopped": frozenset(),
    "failed": frozenset(),
    "interrupted": frozenset(),
}


class SessionManager:
    """会话状态机管理器（文件持久化 + 事件发布 + 人工闸门）。"""

    def __init__(
        self,
        root: Path,
        *,
        broker: EventBroker,
        store: ProjectStore,
        clock: Clock | None = None,
    ) -> None:
        self.root = Path(root)
        self._broker = broker
        self._store = store
        self._gate = ModeGate(clock=clock or MonotonicClock())

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
            raise ControlPlaneError(422, "invalid_session_id", f"会话 ID 非法：{session_id!r}")
        return self.root / f"{session_id}.json"

    def _save(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(str(record["session_id"]))
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, session_id: str) -> dict[str, Any]:
        """读取会话记录；不存在 404，损坏 500。"""
        path = self._path(session_id)
        if not path.is_file():
            raise ControlPlaneError(404, "session_not_found", f"会话不存在：{session_id}")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlPlaneError(500, "corrupt_storage", f"会话文件损坏：{session_id}") from exc
        if not isinstance(record, dict) or "session_id" not in record or "state" not in record:
            raise ControlPlaneError(500, "corrupt_storage", f"会话文件结构非法：{session_id}")
        return record

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出全部会话（跳过损坏文件，供恢复扫描与健康统计使用）。"""
        if not self.root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for f in sorted(self.root.glob("*.json")):
            if not f.is_file():
                continue
            try:
                record = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and "session_id" in record:
                out.append(record)
        return out

    def count(self) -> int:
        """会话文件总数（健康检查用）。"""
        if not self.root.is_dir():
            return 0
        return sum(1 for f in self.root.glob("*.json") if f.is_file())

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def create(self, project_id: str, target_id: str, mode: str) -> dict[str, Any]:
        """创建会话（created 态）；模式合法性按 policy_engine.RunMode 校验。"""
        run_mode = parse_mode(mode)
        if run_mode is None:
            raise ControlPlaneError(
                422,
                "invalid_mode",
                f"未知运行模式 {mode!r}",
                extra={"allowed": [m.value for m in RunMode]},
            )
        # 目标必须存在于项目（同时取受保护标记）；404 直接透出
        target = self._store.get_object(project_id, "targets", target_id)
        protected = target.get("protected_online") is True
        if run_mode is RunMode.REAL_INPUT:
            if protected:
                raise ControlPlaneError(
                    409,
                    "protected_online_no_real_input",
                    "受保护在线目标禁止真实输入（SAFE-019）",
                )
            # 全局单实例：同一时刻只允许一个活跃 RealInput 会话（CTL-006）
            self._ensure_single_real_input()
        now = utc_now_iso()
        record: dict[str, Any] = {
            "session_id": new_session_id(),
            "project_id": project_id,
            "target_id": target_id,
            "mode": run_mode.value,
            "state": "created",
            "created_at": now,
            "updated_at": now,
            "gate": None,
            "history": [{"at": now, "event": "session_created", "from": None, "to": "created"}],
        }
        self._save(record)
        self._broker.publish(
            "session_created",
            {"mode": run_mode.value, "project_id": project_id, "target_id": target_id},
            session_id=str(record["session_id"]),
        )
        return record

    def confirm(self, session_id: str, operator: str) -> dict[str, Any]:
        """real_input 人工闸门确认（CTL-005）；审计记录持久化到会话文件。"""
        record = self.get(session_id)
        if record.get("state") not in ACTIVE_STATES:
            raise ControlPlaneError(409, "invalid_transition", "会话已结束，无法进行人工确认")
        if record.get("mode") != RunMode.REAL_INPUT.value:
            raise ControlPlaneError(409, "gate_not_required", "只有 real_input 会话需要人工闸门确认")
        if not isinstance(operator, str) or not operator.strip():
            raise ControlPlaneError(422, "operator_required", "operator 不能为空")
        target = self._store.get_object(str(record["project_id"]), "targets", str(record["target_id"]))
        target_ref = TargetRef(
            target_id=str(record["target_id"]),
            protected_online=target.get("protected_online") is True,
        )
        # 受保护目标在 ModeGate 层再次被拒（纵深防御），拒绝同样入闸门审计
        confirmation = self._gate.confirm(session_id, operator, target=target_ref, mode=RunMode.REAL_INPUT)
        if confirmation is None:
            raise ControlPlaneError(
                409,
                "protected_online_no_real_input",
                "人工闸门拒绝：受保护在线目标禁止真实输入",
            )
        now = utc_now_iso()
        record["gate"] = {
            "gate_id": confirmation.gate_id,
            "operator": confirmation.operator,
            "mode": confirmation.mode,
            "confirmed_at": now,
        }
        record["updated_at"] = now
        record["history"].append({"at": now, "event": "session_gate_confirmed", "operator": confirmation.operator})
        self._save(record)
        self._broker.publish(
            "session_gate_confirmed",
            {"operator": confirmation.operator, "gate_id": confirmation.gate_id},
            session_id=session_id,
        )
        return record

    def start(self, session_id: str) -> dict[str, Any]:
        """启动会话：real_input 必须已通过人工闸门，否则 409 manual_gate_required。"""
        record = self.get(session_id)
        if record.get("mode") == RunMode.REAL_INPUT.value:
            if not record.get("gate"):
                raise ControlPlaneError(
                    409,
                    "manual_gate_required",
                    "真实输入会话必须先通过人工闸门确认（POST /sessions/{id}/confirm）",
                )
            # 纵深防御：启动时刻再校验全局单实例
            self._ensure_single_real_input(exclude=session_id)
        self._transition(record, "running", "session_started")
        return record

    def pause(self, session_id: str) -> dict[str, Any]:
        """暂停（running/resumed → paused）；已暂停时幂等返回。"""
        record = self.get(session_id)
        if record.get("state") == "paused":
            return record  # 幂等（CTL-005）
        self._transition(record, "paused", "session_paused")
        return record

    def resume(self, session_id: str) -> dict[str, Any]:
        """恢复（paused → resumed）；已处于运行/恢复态时幂等返回。"""
        record = self.get(session_id)
        if record.get("state") in ("running", "resumed"):
            return record  # 幂等（CTL-005）
        self._transition(record, "resumed", "session_resumed")
        return record

    def stop(self, session_id: str) -> dict[str, Any]:
        """停止（任意非终态 → stopped）；已终态时幂等返回（CTL-005）。"""
        record = self.get(session_id)
        if record.get("state") in TERMINAL_STATES:
            return record  # 停止幂等
        self._transition(record, "stopped", "session_stopped")
        return record

    def mark_interrupted(self, session_id: str, *, reason: str = "startup_recovery") -> dict[str, Any]:
        """把非终态会话标记为 interrupted（CTL-009 启动恢复路径）。"""
        record = self.get(session_id)
        if record.get("state") in TERMINAL_STATES:
            return record
        current = str(record.get("state"))
        now = utc_now_iso()
        record["state"] = "interrupted"
        record["updated_at"] = now
        record["history"].append(
            {"at": now, "event": "session_interrupted", "from": current, "to": "interrupted", "reason": reason}
        )
        self._save(record)
        self._broker.publish(
            "session_interrupted",
            {"reason": reason, "previous_state": current},
            session_id=session_id,
        )
        return record

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _transition(self, record: dict[str, Any], to_state: str, event: str) -> None:
        """按迁移表推进状态；非法迁移 409；成功后持久化并发事件。"""
        current = str(record.get("state"))
        if to_state not in TRANSITIONS.get(current, frozenset()):
            raise ControlPlaneError(
                409,
                "invalid_transition",
                f"状态 {current!r} 不允许迁移到 {to_state!r}",
                extra={"from": current, "to": to_state},
            )
        now = utc_now_iso()
        record["state"] = to_state
        record["updated_at"] = now
        record["history"].append({"at": now, "event": event, "from": current, "to": to_state})
        self._save(record)
        self._broker.publish(event, {"state": to_state}, session_id=str(record["session_id"]))

    def _ensure_single_real_input(self, *, exclude: str | None = None) -> None:
        """全局只允许一个活跃 RealInput 会话（CTL-006 / SAFE-019）。"""
        for other in self.list_sessions():
            if exclude is not None and other.get("session_id") == exclude:
                continue
            if other.get("mode") == RunMode.REAL_INPUT.value and other.get("state") in ACTIVE_STATES:
                raise ControlPlaneError(
                    409,
                    "real_input_session_exists",
                    "已存在活跃的真实输入会话（全局单实例约束）",
                    extra={"conflicting_session": other.get("session_id")},
                )
