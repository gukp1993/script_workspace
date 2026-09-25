"""WebSocket 事件总线与有界重连缓冲（CTL-007）。

设计要点：
- 全局单调递增序列号 ``seq``（进程内唯一），事件一经发布不可变更；
- 有界缓冲（默认 1024 条）：断线重连时带 ``last_seq`` 可补发缺失事件；
  若 ``last_seq`` 已落在缓冲窗口之外（无法无缝衔接），服务端先下发一条
  ``resync`` 控制帧，客户端应改用 REST ``GET /api/v1/events?since_seq=``
  全量拉取兜底；
- 安全约定：**帧像素数据禁止经 WS 传输**——本模块只有 ``send_json`` 文本
  帧通道，不存在任何二进制帧路径；图像像素一律走视觉资产文件存储
  （CTL-004）。payload 仅承载小体积结构化事实（状态、计数、引用）；
- WS 令牌经查询参数传递（浏览器 WebSocket 无法自定义请求头）：
  ``/api/v1/ws?token=<token>&last_seq=<n>``；
- 连接后客户端可发送 ``{"subscribe": "<session_id>" | null}`` 订阅过滤，
  以及 ``{"ping": ...}`` 心跳（服务端回 ``{"type": "pong"}``）；
- 发布（publish）必须在事件循环线程内调用——控制面的所有发布点
  （REST 处理器、lifespan）均满足该约束。
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request, WebSocket
from starlette.websockets import WebSocketDisconnect

from common.ids import new_id

from control_plane.security import ws_security_reject
from control_plane.timeutil import utc_now_iso

router = APIRouter()


@dataclass(frozen=True)
class Event:
    """一条已发布事件。

    Attributes:
        seq:        全局单调递增序列号（从 1 开始）。
        type:       事件类型，如 ``session_started``。
        payload:    结构化负载（不含帧像素等大体积数据）。
        session_id: 关联会话（全局事件为 None）。
        ts:         发布时间（UTC ISO-8601）。
    """

    seq: int
    type: str
    payload: dict[str, Any]
    session_id: str | None
    ts: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好的字典。"""
        return {
            "seq": self.seq,
            "type": self.type,
            "payload": dict(self.payload),
            "session_id": self.session_id,
            "ts": self.ts,
        }


class EventBroker:
    """全局事件总线：单调 seq + 有界重连缓冲 + 进程内订阅队列。"""

    def __init__(self, *, buffer_size: int = 1024) -> None:
        self._buffer: deque[Event] = deque(maxlen=buffer_size)
        self._seq: int = 0
        self._subscribers: list[asyncio.Queue[Event]] = []

    @property
    def latest_seq(self) -> int:
        """当前最新序列号（尚未发布任何事件时为 0）。"""
        return self._seq

    def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> Event:
        """发布一条事件并推送给全部订阅者（必须在事件循环线程内调用）。"""
        self._seq += 1
        event = Event(
            seq=self._seq,
            type=event_type,
            payload=dict(payload or {}),
            session_id=session_id,
            ts=utc_now_iso(),
        )
        self._buffer.append(event)
        for queue in list(self._subscribers):
            queue.put_nowait(event)
        return event

    def subscribe(self) -> asyncio.Queue[Event]:
        """注册一个订阅队列（在 WS 连接建立时调用）。"""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        """移除订阅队列（连接关闭时调用，幂等）。"""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def replay(self, last_seq: int) -> tuple[list[Event], bool]:
        """按 ``last_seq`` 计算补发集合。

        Returns:
            (应补发的事件列表, 是否需要 resync)。当 ``last_seq`` 小于
            缓冲窗口起点减一（缺口已超出缓冲）时返回 resync 标记。
        """
        cursor = max(0, last_seq)
        if not self._buffer:
            return [], False
        if cursor < self._buffer[0].seq - 1:
            return [], True
        return [event for event in self._buffer if event.seq > cursor], False

    def since(self, last_seq: int) -> dict[str, Any]:
        """REST 兜底拉取：返回缺失事件与最新 seq；缺口过大时要求 resync。"""
        cursor = max(0, last_seq)
        events, resync = self.replay(cursor)
        return {
            "events": [event.to_dict() for event in events],
            "latest_seq": self._seq,
            "resync_required": resync,
        }


def _new_event_id() -> str:
    """生成事件帧 ID（用于 resync/订阅确认等控制帧的关联）。"""
    return new_id("evt")


async def _handle_client_message(
    websocket: WebSocket,
    message: str,
    filter_session: str | None,
    cursor: int,
) -> str | None:
    """处理客户端下行消息（订阅过滤 / 心跳）；返回新的会话过滤器。"""
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        await websocket.send_json({"type": "error", "id": _new_event_id(), "payload": {"reason": "invalid_json"}})
        return filter_session
    if not isinstance(data, dict):
        await websocket.send_json({"type": "error", "id": _new_event_id(), "payload": {"reason": "invalid_message"}})
        return filter_session
    if "ping" in data:
        # 心跳：回 pong 并携带当前游标，便于客户端校验 seq 连续性
        await websocket.send_json({"type": "pong", "seq": cursor})
        return filter_session
    if "subscribe" in data:
        target = data["subscribe"]
        new_filter = target if isinstance(target, str) and target else None
        await websocket.send_json({"type": "subscribed", "payload": {"session_id": new_filter}})
        return new_filter
    await websocket.send_json({"type": "error", "id": _new_event_id(), "payload": {"reason": "unsupported_message"}})
    return filter_session


@router.get("/api/v1/events")
async def list_events(request: Request, since_seq: int = 0) -> dict[str, Any]:
    """REST 兜底事件拉取（WS 不可用 / resync 后全量补齐用）。"""
    broker: EventBroker = request.app.state.broker
    return broker.since(since_seq)


@router.websocket("/api/v1/ws")
async def websocket_events(websocket: WebSocket, token: str = "", last_seq: int = 0) -> None:
    """事件推送端点：令牌经查询参数传递，支持重连补发与 resync。"""
    config = websocket.app.state.config
    broker: EventBroker = websocket.app.state.broker

    reject_code = ws_security_reject(websocket, config)
    if reject_code is not None:
        # 握手阶段直接拒绝（4401 令牌无效 / 4403 Host 或 Origin 非法）
        await websocket.close(code=reject_code)
        return
    await websocket.accept()

    # 先注册订阅队列再回放缓冲，保证回放与直播之间不丢事件（按 seq 去重）
    queue = broker.subscribe()
    cursor = max(0, last_seq)
    filter_session: str | None = None
    recv_task: asyncio.Task[str] | None = None
    get_task: asyncio.Task[Event] | None = None
    try:
        events, resync = broker.replay(cursor)
        if resync:
            await websocket.send_json(
                {
                    "type": "resync",
                    "seq": broker.latest_seq,
                    "id": _new_event_id(),
                    "payload": {"reason": "buffer_overflow", "since_seq": cursor},
                }
            )
            cursor = broker.latest_seq
        else:
            for event in events:
                await websocket.send_json(event.to_dict())
                cursor = event.seq

        recv_task = asyncio.create_task(websocket.receive_text())
        get_task = asyncio.create_task(queue.get())
        while True:
            done, _pending = await asyncio.wait({recv_task, get_task}, return_when=asyncio.FIRST_COMPLETED)
            if recv_task in done:
                try:
                    message = recv_task.result()
                except WebSocketDisconnect:
                    break
                filter_session = await _handle_client_message(websocket, message, filter_session, cursor)
                recv_task = asyncio.create_task(websocket.receive_text())
            if get_task in done:
                event = get_task.result()
                if event.seq > cursor and (filter_session is None or event.session_id == filter_session):
                    await websocket.send_json(event.to_dict())
                    cursor = event.seq
                get_task = asyncio.create_task(queue.get())
    except (WebSocketDisconnect, RuntimeError):
        pass  # 客户端断开属正常路径
    finally:
        broker.unsubscribe(queue)
        for task in (recv_task, get_task):
            if task is not None and not task.done():
                task.cancel()
