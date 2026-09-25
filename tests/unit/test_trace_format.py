"""TRC-001/002 最小版单测：链式哈希、篡改检测、尾部损坏容错与确定性。

FSM-012 的契约前奏：同输入序列两次生成 -> 事件哈希序列完全一致。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from trace_format import (
    GENESIS_HASH,
    TRACE_FORMAT_VERSION,
    JsonlTraceReader,
    JsonlTraceWriter,
    TraceEvent,
    TraceEventType,
    canonical_json,
    verify,
)

SESSION = "sess-trace"
CID = "cid-trace"


def write_sample(path: Path, count: int = 5) -> list[str]:
    """写入 count 条确定性样例事件，返回写入器的哈希序列。"""
    with JsonlTraceWriter(path) as writer:
        for i in range(count):
            event_type = (
                TraceEventType.STATE_TRANSITION if i % 2 == 0 else TraceEventType.EXECUTED
            )
            writer.append(
                event_type,
                ts_monotonic=float(i),
                correlation_id=CID,
                session_id=SESSION,
                payload={"step": i, "from": "idle", "to": f"s{i}"},
            )
        return [e.hash for e in writer.events]


def rewrite_line(path: Path, index: int, mutate) -> None:
    """把文件第 index 行（0 基）替换为其 JSON 变体（保持合法 JSONL）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    data = json.loads(lines[index])
    mutate(data)
    lines[index] = json.dumps(data, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- TRC-001


def test_roundtrip_chain_is_valid() -> None:
    """写入-读回：seq 连续、prev_hash 链衔接、verify 零报告。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.jsonl"
        expected = write_sample(path, 5)
        events = JsonlTraceReader(path).read()
        assert [e.hash for e in events] == expected
        assert [e.seq for e in events] == [0, 1, 2, 3, 4]
        assert events[0].prev_hash == GENESIS_HASH
        assert all(
            events[i].prev_hash == events[i - 1].hash for i in range(1, len(events))
        )
        assert all(len(e.hash) == 64 for e in events)
        assert verify(events) == []


def test_event_types_version_and_genesis_contract() -> None:
    """格式契约：8 类事件、版本号 "1"、创世哈希为 64 个 0。"""
    assert TRACE_FORMAT_VERSION == "1"
    assert GENESIS_HASH == "0" * 64
    assert {t.value for t in TraceEventType} == {
        "frame_captured",
        "perception_snapshot",
        "state_transition",
        "intent_issued",
        "policy_decision",
        "executed",
        "estop",
        "anomaly",
    }


def test_hash_is_chain_of_prev_and_canonical_json() -> None:
    """哈希公式：hash = sha256(上一事件 hash + 本事件规范化 JSON)。"""
    data0 = {
        "seq": 0, "ts_monotonic": 1.0, "correlation_id": CID,
        "session_id": SESSION, "type": "estop", "payload": {"a": 1},
        "prev_hash": GENESIS_HASH,
    }
    h0 = hashlib.sha256(
        (GENESIS_HASH + canonical_json(data0)).encode("utf-8")
    ).hexdigest()
    data1 = dict(data0, seq=1, prev_hash=h0, type="executed", payload={"a": 2})
    h1 = hashlib.sha256((h0 + canonical_json(data1)).encode("utf-8")).hexdigest()
    assert h0 != h1  # 链式：prev_hash 参与哈希，断一点即断全链
    # 与 TraceEvent.create / TraceEvent.compute_hash 的实现一致。
    event = TraceEvent.create(
        seq=0, prev_hash=GENESIS_HASH, ts_monotonic=1.0, type="estop",
        correlation_id=CID, session_id=SESSION, payload={"a": 1},
    )
    assert event.hash == h0
    assert event.compute_hash() == event.hash
    chained = TraceEvent.create(
        seq=1, prev_hash=event.hash, ts_monotonic=1.0, type="executed",
        correlation_id=CID, session_id=SESSION, payload={"a": 2},
    )
    assert chained.hash == h1


def test_tampered_payload_reported_by_verify() -> None:
    """篡改任一事件内容 -> verify 报断链（hash 不再可复算）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.jsonl"
        write_sample(path, 4)
        events = JsonlTraceReader(path).read()
        assert verify(events) == []
        tampered = list(events)
        tampered[2] = replace(tampered[2], payload={"step": 999})  # 内容被改，hash 未变
        issues = verify(tampered)
        assert issues and any("seq=2" in issue for issue in issues)
        # 后续事件 prev_hash 仍衔接存储值、自身 hash 正确，不会被误报。
        assert sum("seq=3" in i for i in issues) == 0


# ---------------------------------------------------------------- TRC-002


def test_truncated_tail_partial_line_returns_valid_prefix() -> None:
    """崩溃残留（最后一行残缺 JSON）-> 完好前缀 + truncated_tail=True。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.jsonl"
        expected = write_sample(path, 5)
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 5, "ts_monotonic": 5.0, "cor')  # 半行即崩溃
        reader = JsonlTraceReader(path)
        events = reader.read()
        assert [e.hash for e in events] == expected  # 完好前缀可读
        assert reader.truncated_tail is True
        assert reader.errors  # 损坏原因留痕
        assert verify(events) == []  # 前缀自身链完整


def test_broken_chain_tail_is_truncated() -> None:
    """尾行哈希断链（内容被改）-> 截断该行并标记 truncated_tail。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.jsonl"
        expected = write_sample(path, 3)
        rewrite_line(path, 2, lambda d: d.update(payload={"tampered": True}))
        reader = JsonlTraceReader(path)
        events = reader.read()
        assert [e.hash for e in events] == expected[:2]
        assert reader.truncated_tail is True
        assert verify(events) == []


def test_reader_truncates_at_tampered_middle_line() -> None:
    """中间行被篡改 -> 读取端按最长可信前缀截断（保守崩溃恢复语义）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.jsonl"
        write_sample(path, 4)
        rewrite_line(path, 1, lambda d: d.update(payload={"step": -1}))
        reader = JsonlTraceReader(path)
        events = reader.read()
        assert len(events) == 1  # 只保留篡改点之前的完好前缀
        assert reader.truncated_tail is True
        assert verify(events) == []


# ---------------------------------------------------------------- FSM-012 前奏


def test_same_input_sequence_yields_identical_hash_chain() -> None:
    """确定性：同输入序列两次生成 -> 事件哈希序列完全一致。"""
    with tempfile.TemporaryDirectory() as tmp:
        path_a = Path(tmp) / "a.jsonl"
        path_b = Path(tmp) / "b.jsonl"
        hashes_a = write_sample(path_a, 6)
        hashes_b = write_sample(path_b, 6)
        assert hashes_a == hashes_b
        read_a = JsonlTraceReader(path_a).read()
        read_b = JsonlTraceReader(path_b).read()
        assert [e.hash for e in read_a] == [e.hash for e in read_b]
        assert verify(read_a) == [] and verify(read_b) == []


def test_writer_assigns_seq_prev_hash_and_flushes() -> None:
    """writer 逐条赋 seq/prev_hash/hash；append_event 忽略外部 seq/hash。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.jsonl"
        writer = JsonlTraceWriter(path, flush_every=True)
        first = writer.append_event(
            {"seq": 999, "hash": "forged", "type": "intent_issued",
             "ts_monotonic": 0.5, "session_id": SESSION, "correlation_id": CID,
             "payload": {"kind": "click"}}
        )
        second = writer.append(
            "estop", ts_monotonic=1.5, session_id=SESSION, correlation_id=CID,
            payload={"reason": "hotkey"},
        )
        writer.close()
        assert first.seq == 0 and first.hash != "forged" and len(first.hash) == 64
        assert second.seq == 1 and second.prev_hash == first.hash
        assert writer.last_hash == second.hash
        # 逐条 flush：写完即已落盘可见。
        events = JsonlTraceReader(path).read()
        assert [e.type for e in events] == ["intent_issued", "estop"]
        assert verify(events) == []
