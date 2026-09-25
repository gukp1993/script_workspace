"""Oracle 真值轨迹与注入输入记录（LAB-007 M0 子集）。

Oracle 记录两类"真相"，供 E2E / 回放断言使用：

1. 逐帧场景真值（:class:`FrameRecord`）：每帧的 :class:`SceneState` 与
   场景单调时间 ``t_monotonic``（= frame_index / fps，刻意不用墙钟，
   保证 to_json 可往返且确定性）；
2. 离散事件（:class:`OracleEvent`）：loading_started / loading_ended /
   target_appeared / target_disappeared / loot_appeared /
   loot_disappeared / popup_opened / popup_closed / scenario_started /
   scenario_ended 等，``t_monotonic`` 单调不减（写入时强校验）。

另外 :meth:`OracleTrace.record_input` 记录"注入给模拟器的模拟输入"
（key_down / key_up / click + 参数）。ArenaLab 只记录输入、绝不产生
任何真实键鼠调用——这是 LAB-007 Oracle 在 M0 的安全子集。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from arena_lab.render import SceneState

__all__ = [
    "OracleTrace",
    "OracleEvent",
    "FrameRecord",
    "InputRecord",
    "EVENT_SCENARIO_STARTED",
    "EVENT_SCENARIO_ENDED",
    "EVENT_LOADING_STARTED",
    "EVENT_LOADING_ENDED",
    "EVENT_TARGET_APPEARED",
    "EVENT_TARGET_DISAPPEARED",
    "EVENT_LOOT_APPEARED",
    "EVENT_LOOT_DISAPPEARED",
    "EVENT_POPUP_OPENED",
    "EVENT_POPUP_CLOSED",
]

# 离散事件名常量（场景脚本与断言共用，避免裸字符串拼写漂移）。
EVENT_SCENARIO_STARTED = "scenario_started"
EVENT_SCENARIO_ENDED = "scenario_ended"
EVENT_LOADING_STARTED = "loading_started"
EVENT_LOADING_ENDED = "loading_ended"
EVENT_TARGET_APPEARED = "target_appeared"
EVENT_TARGET_DISAPPEARED = "target_disappeared"
EVENT_LOOT_APPEARED = "loot_appeared"
EVENT_LOOT_DISAPPEARED = "loot_disappeared"
EVENT_POPUP_OPENED = "popup_opened"
EVENT_POPUP_CLOSED = "popup_closed"

# 单调性校验的浮点容差。
_EPS = 1e-9

# SceneState 的 dataclass 字段名（from_json 还原时过滤未知键）。
_STATE_FIELDS = frozenset(SceneState.__dataclass_fields__)


@dataclass(frozen=True)
class OracleEvent:
    """一个离散事件：事件名 + 场景单调时间（秒）。"""

    name: str
    t_monotonic: float


@dataclass(frozen=True)
class FrameRecord:
    """一帧的真值：场景单调时间 + 当帧 SceneState。"""

    t_monotonic: float
    state: SceneState


@dataclass(frozen=True)
class InputRecord:
    """一条注入输入：场景单调时间 + 动作名 + 参数（key/button/x/y ...）。"""

    t_monotonic: float
    action: str
    params: dict[str, Any] = field(default_factory=dict)


class OracleTrace:
    """场景运行的真值轨迹：逐帧状态、离散事件、注入输入。

    - 事件与帧的 ``t_monotonic`` 均为场景时间（frame_index / fps 派生），
      写入时强制单调不减（违反抛 ValueError，尽早暴露脚本错误）；
    - ``to_json()`` 输出确定性 JSON（排序键），``from_json()`` 完整还原。
    """

    def __init__(self, meta: dict[str, Any] | None = None) -> None:
        self._meta: dict[str, Any] = dict(meta or {})
        self._frames: list[FrameRecord] = []
        self._events: list[OracleEvent] = []
        self._inputs: list[InputRecord] = []

    # ---- 只读访问 --------------------------------------------------------

    @property
    def meta(self) -> dict[str, Any]:
        return dict(self._meta)

    @property
    def frames(self) -> tuple[FrameRecord, ...]:
        return tuple(self._frames)

    @property
    def events(self) -> tuple[OracleEvent, ...]:
        return tuple(self._events)

    @property
    def inputs(self) -> tuple[InputRecord, ...]:
        return tuple(self._inputs)

    # ---- 写入 ------------------------------------------------------------

    def record_frame(self, t_monotonic: float, state: SceneState) -> None:
        """记录一帧真值。t_monotonic 必须相对已有帧单调不减。"""
        t = float(t_monotonic)
        if self._frames and t < self._frames[-1].t_monotonic - _EPS:
            raise ValueError(
                f"帧时间必须单调不减：{t!r} < 上一帧 {self._frames[-1].t_monotonic!r}"
            )
        self._frames.append(FrameRecord(t_monotonic=t, state=state))

    def record_event(self, name: str, t_monotonic: float) -> None:
        """记录一个离散事件。t_monotonic 必须相对已有事件单调不减。"""
        if not name:
            raise ValueError("事件名不能为空")
        t = float(t_monotonic)
        if self._events and t < self._events[-1].t_monotonic - _EPS:
            raise ValueError(
                f"事件时间必须单调不减：{t!r} < 上一事件 {self._events[-1].t_monotonic!r}"
            )
        self._events.append(OracleEvent(name=str(name), t_monotonic=t))

    def record_input(self, t_monotonic: float, action: str, **params: Any) -> None:
        """记录一条注入的模拟输入（仅记录，绝不执行真实输入）。"""
        if not action:
            raise ValueError("输入动作名不能为空")
        self._inputs.append(
            InputRecord(t_monotonic=float(t_monotonic), action=str(action), params=dict(params))
        )

    # ---- 序列化 ----------------------------------------------------------

    def to_json(self) -> str:
        """导出为确定性 JSON 文本（排序键，可直接 json.loads 往返）。"""
        payload = {
            "format": "arena_lab.oracle/1",
            "meta": self._meta,
            "frames": [
                {"t_monotonic": record.t_monotonic, **asdict(record.state)}
                for record in self._frames
            ],
            "events": [asdict(event) for event in self._events],
            "inputs": [asdict(item) for item in self._inputs],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> OracleTrace:
        """从 to_json() 的文本还原 OracleTrace（输入必须是合法 JSON）。"""
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("OracleTrace JSON 必须是对象")
        trace = cls(meta=dict(data.get("meta") or {}))
        for item in data.get("frames") or []:
            state = SceneState(**{k: v for k, v in item.items() if k in _STATE_FIELDS})
            trace.record_frame(item["t_monotonic"], state)
        for item in data.get("events") or []:
            trace.record_event(item["name"], item["t_monotonic"])
        for item in data.get("inputs") or []:
            trace.record_input(item["t_monotonic"], item["action"], **(item.get("params") or {}))
        return trace
