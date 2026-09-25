"""回放差异引擎与可读报告（TRC-007）。

- :func:`diff_replays`：比较两次回放（:class:`ReplayResult`），定位首个
  分歧 tick、分歧类型（perception/state/intent/hash）、受影响对象
  （状态名 / 迁移 / 意图 kind / 感知字段-检测器）与严重度：
  state 或 intent 分歧 = ``error``；仅 hash 或仅 perception 分歧 =
  ``warning``；无分歧 = ``none``；序列长度不一致视同 state 级 ``error``。
- :func:`diff_perceptions`：VIS 变更定位——逐 tick 比较快照字段，指出
  哪个字段先分歧（AC-P0-09 的"首个视觉分歧/样本/检测器"）。
- :meth:`ReplayDiff.to_report`：可读中文报告——分歧点 + 前后各 3 tick
  上下文 + 统计。

确定性约定：所有差异列表按 (tick, 字段/对象名) 排序，报告内容稳定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from domain_model.models import FieldObservation
from trace_format.replay import ReplayResult

__all__ = [
    "SEVERITY_NONE",
    "SEVERITY_WARNING",
    "SEVERITY_ERROR",
    "FieldDiff",
    "ReplayDiff",
    "diff_replays",
    "diff_perceptions",
]

#: 无分歧
SEVERITY_NONE: str = "none"
#: 告警级（仅感知或仅哈希分歧：视觉回归/哈希漂移，尚未改变行为）
SEVERITY_WARNING: str = "warning"
#: 错误级（状态或意图分歧：行为已改变）
SEVERITY_ERROR: str = "error"

#: 数值默认比较容差
VALUE_TOL: float = 1e-9

#: 报告上下文窗口：首个分歧点前后各 3 tick
CONTEXT_RADIUS: int = 3


# ---------------------------------------------------------------------------
# 感知字段差异（VIS 变更定位）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDiff:
    """一条感知字段差异。

    Attributes:
        tick:        分歧 tick（1 起）。
        field:       语义字段名。
        detector_id: 产生该字段的检测器 ID（调用方提供映射时；否则 None）。
        before:      base 侧观测 ``{"present","confidence","value"}``；字段缺失为 None。
        after:       other 侧观测（同上）。
    """

    tick: int
    field: str
    detector_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    def describe(self) -> str:
        """单行人类可读描述。"""
        detector = f"，检测器 {self.detector_id}" if self.detector_id else ""
        return (
            f"tick {self.tick} 字段 {self.field}{detector}: "
            f"{_fmt_obs(self.before)} -> {_fmt_obs(self.after)}"
        )


def _obs_dict(obs: FieldObservation | None) -> dict[str, Any] | None:
    """观测 -> 契约 payload 形状（与 perception_snapshot 字段结构一致）。"""
    if obs is None:
        return None
    return {
        "present": bool(obs.present),
        "confidence": float(obs.confidence),
        "value": obs.value,
    }


def _fmt_obs(obs: dict[str, Any] | None) -> str:
    """观测的紧凑展示（报告用）。"""
    if obs is None:
        return "<缺失>"
    return f"present={obs['present']}, value={obs['value']!r}, confidence={obs['confidence']:.4f}"


def _value_differs(a: Any, b: Any, tol: float) -> bool:
    """观测值比较：数值按容差，其余按类型严格相等（bool 与数值不等价）。"""
    if a is None and b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return not (isinstance(a, bool) and isinstance(b, bool) and a == b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) > tol
    return type(a) is not type(b) or a != b


def _entry_differs(
    a: FieldObservation | None, b: FieldObservation | None, tol: float
) -> bool:
    """两条观测是否分歧：缺失 / present / value / confidence 任一不同。"""
    if a is None or b is None:
        return a is not b
    if a.present != b.present:
        return True
    if _value_differs(a.value, b.value, tol):
        return True
    return abs(float(a.confidence) - float(b.confidence)) > tol


def diff_perceptions(
    base: ReplayResult,
    other: ReplayResult,
    *,
    detector_ids: Mapping[str, str] | None = None,
    value_tol: float = VALUE_TOL,
    confidence_tol: float = VALUE_TOL,
) -> tuple[FieldDiff, ...]:
    """逐 tick 比较两回放的感知快照，返回字段级差异（按 tick、字段名排序）。

    ``detector_ids``：字段名 -> 检测器 ID 映射（报告与影响分析指出检测器）。
    """
    field_map = dict(detector_ids) if detector_ids else {}
    out: list[FieldDiff] = []
    for idx in range(min(len(base.snapshot_seq), len(other.snapshot_seq))):
        a_snap = base.snapshot_seq[idx]
        b_snap = other.snapshot_seq[idx]
        for name in sorted(set(a_snap.values) | set(b_snap.values)):
            a_obs = a_snap.values.get(name)
            b_obs = b_snap.values.get(name)
            if _entry_differs(a_obs, b_obs, max(value_tol, confidence_tol)):
                out.append(
                    FieldDiff(
                        tick=idx + 1,
                        field=name,
                        detector_id=field_map.get(name),
                        before=_obs_dict(a_obs),
                        after=_obs_dict(b_obs),
                    )
                )
    return tuple(out)


# ---------------------------------------------------------------------------
# 回放级差异与报告
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayDiff:
    """两次回放的差异汇总（TRC-007）。

    Attributes:
        first_divergence_tick: 首个分歧 tick（1 起）；无分歧为 None。
        last_divergence_tick:  末个分歧 tick；无分歧为 None。
        kinds:                 分歧类型（perception/state/intent/hash，字典序）。
        severity:              严重度：none / warning / error。
        affected_states:       受影响状态名（字典序）。
        affected_transitions:  受影响迁移（"a→b" 形式，字典序）。
        affected_intents:      受影响意图 kind（字典序）。
        perception_diffs:      字段级感知差异（首个元素即"首个视觉分歧"）。
        state_divergent_ticks: 状态分歧的 tick 列表（升序）。
        intent_divergent_ticks: 意图分歧的 tick 列表（升序）。
        hash_divergent_ticks:  哈希分歧的 tick 列表（升序）。
        length_base/length_other: 两侧回放 tick 数。
        length_mismatch:       序列长度是否不一致（视同 state 级差异）。
        stop_base/stop_other:  两侧 (stopped, stop_reason)。
        context_lines:         首个分歧点前后各 3 tick 的逐 tick 上下文行。
        frame_refs_base:       base 侧每 tick 的帧引用（感知分歧样本标注）。
    """

    first_divergence_tick: int | None
    last_divergence_tick: int | None
    kinds: tuple[str, ...]
    severity: str
    affected_states: tuple[str, ...]
    affected_transitions: tuple[str, ...]
    affected_intents: tuple[str, ...]
    perception_diffs: tuple[FieldDiff, ...]
    state_divergent_ticks: tuple[int, ...]
    intent_divergent_ticks: tuple[int, ...]
    hash_divergent_ticks: tuple[int, ...]
    length_base: int
    length_other: int
    length_mismatch: bool
    stop_base: tuple[bool, str | None]
    stop_other: tuple[bool, str | None]
    context_lines: tuple[str, ...] = ()
    frame_refs_base: tuple[str | None, ...] = ()

    @property
    def has_divergence(self) -> bool:
        """是否存在任何分歧。"""
        return self.severity != SEVERITY_NONE

    def to_report(self) -> str:
        """生成可读中文报告：分歧点 + 前后各 3 tick 上下文 + 统计。"""
        lines: list[str] = ["=== 回放差异报告（TRC-007） ==="]
        if not self.has_divergence:
            lines.append("结论: 无分歧（严重度 none）")
        else:
            lines.append(f"结论: 检测到 {len(self.kinds)} 类分歧，严重度 {self.severity}")
            lines.append(f"分歧类型: {', '.join(self.kinds) if self.kinds else '-'}")
            lines.append(
                f"首个分歧 tick: {self.first_divergence_tick}；"
                f"末个分歧 tick: {self.last_divergence_tick}"
            )
            lines.append(f"受影响状态: {', '.join(self.affected_states) or '-'}")
            lines.append(f"受影响迁移: {', '.join(self.affected_transitions) or '-'}")
            lines.append(f"受影响意图: {', '.join(self.affected_intents) or '-'}")
        if self.perception_diffs:
            lines.append(f"感知分歧: 共 {len(self.perception_diffs)} 处；首个视觉分歧如下")
            first = self.perception_diffs[0]
            sample = self._sample_ref(first.tick)
            sample_note = f"，样本 frame_ref={sample}" if sample else ""
            lines.append(f"  - {first.describe()}{sample_note}")
            for item in self.perception_diffs[1:6]:
                lines.append(f"  - {item.describe()}")
            if len(self.perception_diffs) > 6:
                lines.append(f"  - … 其余 {len(self.perception_diffs) - 6} 处从略")
        elif self.kinds:
            lines.append("感知分歧: 无（视觉结果一致，分歧来自状态机/决策侧）")
        if self.context_lines:
            lines.append("")
            lines.extend(self.context_lines)
        lines.append("---- 统计 ----")
        lines.append(
            f"比较 tick 数: {min(self.length_base, self.length_other)}"
            f"（base={self.length_base}, other={self.length_other}，"
            f"长度一致: {'否' if self.length_mismatch else '是'}）"
        )
        lines.append(
            f"分歧 tick 数: 状态 {len(self.state_divergent_ticks)}，"
            f"意图 {len(self.intent_divergent_ticks)}，哈希 {len(self.hash_divergent_ticks)}，"
            f"感知 {len(self.perception_diffs)}"
        )
        lines.append(f"停止: base={_fmt_stop(self.stop_base)}, other={_fmt_stop(self.stop_other)}")
        return "\n".join(lines)

    def _sample_ref(self, tick: int) -> str | None:
        """base 侧某 tick 的样本帧引用（越界/缺失返回 None）。"""
        idx = tick - 1
        if 0 <= idx < len(self.frame_refs_base):
            return self.frame_refs_base[idx]
        return None


def _fmt_stop(stop: tuple[bool, str | None]) -> str:
    stopped, reason = stop
    return f"stopped({reason})" if stopped else "running"


def _build_context_lines(base: ReplayResult, other: ReplayResult, first_tick: int) -> tuple[str, ...]:
    """首个分歧点前后各 CONTEXT_RADIUS 个 tick 的逐 tick 上下文行。"""
    total = min(base.ticks, other.ticks)
    start = max(1, first_tick - CONTEXT_RADIUS)
    end = min(total, first_tick + CONTEXT_RADIUS)
    lines = [f"---- 上下文（tick {start}~{end}，base | other） ----"]
    for tick in range(start, end + 1):
        idx = tick - 1

        def _side(seq_states: tuple[str, ...], seq_intents: tuple, seq_hashes: tuple[str, ...]) -> str:
            state = seq_states[idx] if idx < len(seq_states) else "<无>"
            if idx < len(seq_intents):
                kinds = ",".join(i.kind for i in seq_intents[idx])
                intents = f"[{kinds}]"
            else:
                intents = "<无>"
            hash_text = (seq_hashes[idx][:8] + "…") if idx < len(seq_hashes) else "<无>"
            return f"state={state} intents={intents} hash={hash_text}"

        left = _side(base.state_seq, base.intent_seq, base.hash_seq)
        right = _side(other.state_seq, other.intent_seq, other.hash_seq)
        lines.append(f"tick {tick}: {left} | {right}")
    return tuple(lines)


def _affected_transitions(
    base: ReplayResult,
    other: ReplayResult,
    first_tick: int | None,
    last_tick: int | None,
) -> set[str]:
    """分歧区间内两侧实际发生的状态迁移（"a→b"），作为受影响迁移面。"""
    transitions: set[str] = set()
    if first_tick is None or last_tick is None:
        return transitions
    lo = max(1, first_tick)
    hi = min(min(base.ticks, other.ticks), last_tick)
    for result in (base, other):
        for tick in range(lo, hi + 1):
            idx = tick - 1
            if idx == 0:
                continue  # 首 tick 无前序状态可对照
            prev_state = result.state_seq[idx - 1]
            cur_state = result.state_seq[idx]
            if cur_state != prev_state:
                transitions.add(f"{prev_state}→{cur_state}")
    return transitions


def diff_replays(
    base: ReplayResult,
    other: ReplayResult,
    *,
    detector_ids: Mapping[str, str] | None = None,
    value_tol: float = VALUE_TOL,
    confidence_tol: float = VALUE_TOL,
) -> ReplayDiff:
    """比较两次回放，定位首个分歧与影响面（TRC-007）。

    分类约定：state / intent 分歧 -> ``error``；仅 hash / 仅 perception
    分歧 -> ``warning``；长度不一致视同 state 分歧（``error``）。
    """
    perception_diffs = diff_perceptions(
        base, other, detector_ids=detector_ids, value_tol=value_tol, confidence_tol=confidence_tol
    )
    perception_ticks = {d.tick for d in perception_diffs}
    state_ticks: set[int] = set()
    intent_ticks: set[int] = set()
    hash_ticks: set[int] = set()
    affected_states: set[str] = set()
    affected_intents: set[str] = set()

    n = min(base.ticks, other.ticks)
    for idx in range(n):
        if base.state_seq[idx] != other.state_seq[idx]:
            state_ticks.add(idx + 1)
            affected_states.update({base.state_seq[idx], other.state_seq[idx]})
        if base.intent_seq[idx] != other.intent_seq[idx]:
            intent_ticks.add(idx + 1)
            for intent in base.intent_seq[idx] + other.intent_seq[idx]:
                affected_intents.add(intent.kind)
        if base.hash_seq[idx] != other.hash_seq[idx]:
            hash_ticks.add(idx + 1)

    length_mismatch = base.ticks != other.ticks
    if length_mismatch:
        # 长度不一致意味着停止点/轨迹结构不同，视同状态级差异。
        state_ticks.add(n + 1)
        for seq in (base, other):
            if seq.ticks > n:
                affected_states.add(seq.state_seq[n])

    kinds: list[str] = []
    if perception_diffs:
        kinds.append("perception")
    if state_ticks:
        kinds.append("state")
    if intent_ticks:
        kinds.append("intent")
    if hash_ticks:
        kinds.append("hash")

    all_ticks = perception_ticks | state_ticks | intent_ticks | hash_ticks
    first_tick: int | None = min(all_ticks) if all_ticks else None
    last_tick: int | None = max(all_ticks) if all_ticks else None

    if state_ticks or intent_ticks:
        severity = SEVERITY_ERROR
    elif perception_diffs or hash_ticks:
        severity = SEVERITY_WARNING
    else:
        severity = SEVERITY_NONE

    context_lines: tuple[str, ...] = ()
    if first_tick is not None:
        context_lines = _build_context_lines(base, other, first_tick)

    return ReplayDiff(
        first_divergence_tick=first_tick,
        last_divergence_tick=last_tick,
        kinds=tuple(sorted(kinds)),
        severity=severity,
        affected_states=tuple(sorted(affected_states)),
        affected_transitions=tuple(sorted(_affected_transitions(base, other, first_tick, last_tick))),
        affected_intents=tuple(sorted(affected_intents)),
        perception_diffs=perception_diffs,
        state_divergent_ticks=tuple(sorted(state_ticks)),
        intent_divergent_ticks=tuple(sorted(intent_ticks)),
        hash_divergent_ticks=tuple(sorted(hash_ticks)),
        length_base=base.ticks,
        length_other=other.ticks,
        length_mismatch=length_mismatch,
        stop_base=(base.stopped, base.stop_reason),
        stop_other=(other.stopped, other.stop_reason),
        context_lines=context_lines,
        frame_refs_base=base.frame_refs,
    )
