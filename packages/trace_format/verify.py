"""哈希链校验与断链报告（TRC-001/002）。"""

from __future__ import annotations

from typing import Sequence

from trace_format.events import GENESIS_HASH, TraceEvent


def verify(events: Sequence[TraceEvent]) -> list[str]:
    """校验事件序列的哈希链，返回断链报告（空列表 = 完整通过）。

    检查项（按事件顺序）：
    - seq 从 0 开始且连续递增；
    - 首事件 prev_hash == GENESIS_HASH，其后 prev_hash == 上一事件 hash；
    - 每个事件 hash 可由自身内容复算（防篡改）。
    """
    issues: list[str] = []
    prev_hash = GENESIS_HASH
    expected_seq = 0
    for event in events:
        if event.seq != expected_seq:
            issues.append(f"seq={event.seq}: 序号不连续，期望 {expected_seq}")
        if event.prev_hash != prev_hash:
            issues.append(f"seq={event.seq}: prev_hash 与前序 hash 不衔接")
        if event.compute_hash() != event.hash:
            issues.append(f"seq={event.seq}: hash 校验失败（内容被篡改或损坏）")
        prev_hash = event.hash
        expected_seq = event.seq + 1
    return issues
