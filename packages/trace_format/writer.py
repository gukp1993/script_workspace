"""轨迹落盘：追加写（自动 seq/prev_hash/hash）与崩溃容错读取（TRC-002）。

- JsonlTraceWriter：每条 append 自动赋 seq/prev_hash/hash 并写一行 JSONL，
  默认逐条 flush（也可批量后手动 flush/close）；
- JsonlTraceReader.read()：返回"最长可信前缀"——遇到残缺行、缺字段或
  哈希/链校验失败时停止读取，并标记 truncated_tail=True，绝不抛出
  未处理异常；完好前缀可继续用 verify 复核。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from trace_format.events import GENESIS_HASH, TraceEvent, TraceEventType


class JsonlTraceWriter:
    """JSONL 轨迹追加写器（TRC-002）。"""

    def __init__(self, path: str | Path, *, flush_every: bool = True) -> None:
        self.path = Path(path)
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_every = bool(flush_every)
        self._events: list[TraceEvent] = []
        self._fh = self.path.open("a", encoding="utf-8", newline="\n")

    # ------------------------------------------------------------------ 写入
    def append(
        self,
        type: str | TraceEventType,
        *,
        ts_monotonic: float,
        correlation_id: str = "",
        session_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> TraceEvent:
        """追加一条事件：自动赋 seq/prev_hash/hash；返回完整事件。"""
        seq = len(self._events)
        prev_hash = self._events[-1].hash if self._events else GENESIS_HASH
        event = TraceEvent.create(
            seq=seq,
            prev_hash=prev_hash,
            ts_monotonic=ts_monotonic,
            type=type,
            correlation_id=correlation_id,
            session_id=session_id,
            payload=payload,
        )
        self._events.append(event)
        self._write_line(event)
        return event

    def append_event(self, data: Mapping[str, Any]) -> TraceEvent:
        """以原始 dict 追加事件；seq/prev_hash/hash 由 writer 重新赋值。"""
        return self.append(
            data.get("type", TraceEventType.ANOMALY.value),
            ts_monotonic=float(data.get("ts_monotonic", 0.0)),
            correlation_id=str(data.get("correlation_id", "")),
            session_id=str(data.get("session_id", "")),
            payload=dict(data.get("payload") or {}),
        )

    # ------------------------------------------------------------------ 管理
    def flush(self) -> None:
        """刷新底层文件缓冲。"""
        self._fh.flush()

    def close(self) -> None:
        """刷新并关闭文件（幂等）。"""
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> "JsonlTraceWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        """已写入事件（内存视图）。"""
        return tuple(self._events)

    @property
    def last_hash(self) -> str:
        """链尾哈希（空轨迹为 GENESIS_HASH）。"""
        return self._events[-1].hash if self._events else GENESIS_HASH

    def _write_line(self, event: TraceEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        self._fh.write(line + "\n")
        if self._flush_every:
            self._fh.flush()


class JsonlTraceReader:
    """JSONL 轨迹读取器：返回最长可信前缀并标记尾部损坏（TRC-002）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.truncated_tail: bool = False
        self.errors: list[str] = []

    def read(self) -> list[TraceEvent]:
        """读取全部可信事件；尾部损坏 -> 返回完好前缀 + truncated_tail=True。"""
        events: list[TraceEvent] = []
        self.truncated_tail = False
        self.errors = []
        prev_stored_hash = GENESIS_HASH
        expected_seq = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue  # 空行（含结尾换行）不视为损坏
                try:
                    event = TraceEvent.from_dict(json.loads(line))
                except (json.JSONDecodeError, ValueError) as exc:
                    self.truncated_tail = True
                    self.errors.append(f"line {lineno}: {exc}")
                    break
                chain_ok = (
                    event.seq == expected_seq
                    and event.prev_hash == prev_stored_hash
                    and event.compute_hash() == event.hash
                )
                if not chain_ok:
                    # 哈希断链/序号断裂：保守截断，保留完好的前缀。
                    self.truncated_tail = True
                    self.errors.append(f"line {lineno}: 哈希链校验失败")
                    break
                events.append(event)
                prev_stored_hash = event.hash
                expected_seq = event.seq + 1
        return events
