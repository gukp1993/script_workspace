"""内置场景脚本与种子化随机（LAB-004）。

内置 3 个场景（通过 :func:`run_scenario` 执行）：

- ``happy_path``：加载 -> 就绪 -> 目标出现 -> 血条变化（受击/治疗）->
  掉落 -> 弹窗 -> 结束，覆盖全部场景元素；
- ``loading_timeout``：加载持续到场景结束（进度到 0.8 后停滞），
  用于验证"加载永不结束"的负向路径；
- ``popup_random``：由种子驱动的有界随机时刻弹窗/目标出现。

确定性约定：

- 所有随机性只来自 ``random.Random(seed)``，且抖动幅度有上限
  （cap = min(0.5s, 4% 场景时长)），事件时刻只会小幅偏移；
- 场景时间是确定性的（frame_index / fps），刻意不接真实时钟——
  帧序列与 Oracle 轨迹都是 (name, seed, resolution, fps, duration) 的
  纯函数：同参数两次运行逐帧哈希完全一致，异种子至少部分帧不同。
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import numpy as np

from arena_lab.oracle import OracleTrace
from arena_lab.render import Renderer, SceneConfig, SceneState

__all__ = [
    "ScenarioPlan",
    "ScenarioRun",
    "BUILTIN_SCENARIOS",
    "list_scenarios",
    "run_scenario",
]


@dataclass(frozen=True)
class ScenarioPlan:
    """一个场景在给定种子下的确定性执行计划。

    ``events`` 为 (t_monotonic, 事件名) 序列（run_scenario 会按时间排序后
    写入 Oracle）；``state_at(t, frame_index)`` 返回该时刻的场景真值。
    """

    events: tuple[tuple[float, str], ...]
    state_at: Callable[[float, int], SceneState]


def _plan_happy_path(rng: random.Random, duration: float) -> ScenarioPlan:
    """happy_path：完整正向流程，事件时刻带种子化的有界抖动。"""
    cap = min(0.5, 0.04 * duration)

    def jit(base: float) -> float:
        """事件时刻 = 基准 + U(-cap, cap)，并夹到 [0, duration]。"""
        return min(max(base + rng.uniform(-cap, cap), 0.0), duration)

    # 依赖链上做次序保护（后者不早于前者 + epsilon），抖动不会破坏流程。
    t_load_end = max(jit(0.20 * duration), 0.01)
    t_target_on = max(jit(0.35 * duration), t_load_end + 0.01)
    t_cast = t_target_on
    t_hit1 = max(jit(0.42 * duration), t_target_on + 0.005)
    t_hit2 = max(jit(0.52 * duration), t_hit1 + 0.005)
    t_target_off = max(jit(0.62 * duration), t_hit2 + 0.005)
    t_ready = max(jit(0.55 * duration), t_hit2)
    t_loot_on = max(jit(0.66 * duration), t_target_off + 0.005)
    t_heal = max(jit(0.68 * duration), t_loot_on)
    t_loot_off = max(jit(0.74 * duration), t_loot_on + 0.005)
    t_popup_on = max(jit(0.78 * duration), t_loot_off)
    t_popup_off = min(max(jit(0.90 * duration), t_popup_on + 0.005), duration)

    events: tuple[tuple[float, str], ...] = (
        (0.0, "scenario_started"),
        (0.0, "loading_started"),
        (t_load_end, "loading_ended"),
        (t_cast, "skill_cast"),
        (t_target_on, "target_appeared"),
        (t_hit1, "hit_taken"),
        (t_hit2, "hit_taken"),
        (t_target_off, "target_disappeared"),
        (t_ready, "cooldown_finished"),
        (t_loot_on, "loot_appeared"),
        (t_heal, "healed"),
        (t_loot_off, "loot_disappeared"),
        (t_popup_on, "popup_opened"),
        (t_popup_off, "popup_closed"),
        (float(duration), "scenario_ended"),
    )

    def state_at(t: float, frame_index: int) -> SceneState:
        if t < t_load_end:
            progress = 1.0 if t_load_end <= 0 else min(t / t_load_end, 1.0)
            return SceneState(
                loading_active=True, loading_progress=progress, frame_index=frame_index
            )
        health = 1.0
        if t >= t_hit1:
            health = 0.6
        if t >= t_hit2:
            health = 0.35
        if t >= t_heal:
            health = 0.8
        resource = 1.0
        if t >= t_cast:
            resource = 0.7
        if t >= t_heal:
            resource = 0.85
        return SceneState(
            health_ratio=health,
            resource_ratio=resource,
            cooldown_ready=not (t_cast <= t < t_ready),
            target_present=t_target_on <= t < t_target_off,
            loot_present=t_loot_on <= t < t_loot_off,
            loading_active=False,
            loading_progress=0.0,
            popup_open=t_popup_on <= t < t_popup_off,
            frame_index=frame_index,
        )

    return ScenarioPlan(events=events, state_at=state_at)


def _plan_loading_timeout(rng: random.Random, duration: float) -> ScenarioPlan:
    """loading_timeout：加载持续到场景结束（负向路径，永不 loading_ended）。"""
    cap = min(0.5, 0.04 * duration)
    peak = 0.8
    t_stall = min(max(0.60 * duration + rng.uniform(-cap, cap), 0.01), duration)

    events: tuple[tuple[float, str], ...] = (
        (0.0, "scenario_started"),
        (0.0, "loading_started"),
        (t_stall, "loading_stalled"),
        (float(duration), "scenario_ended"),
    )

    def state_at(t: float, frame_index: int) -> SceneState:
        progress = peak if t_stall <= 0 else peak * min(t / t_stall, 1.0)
        return SceneState(
            loading_active=True, loading_progress=progress, frame_index=frame_index
        )

    return ScenarioPlan(events=events, state_at=state_at)


def _plan_popup_random(rng: random.Random, duration: float) -> ScenarioPlan:
    """popup_random：种子驱动的有界随机时刻弹窗 / 目标 / 掉落。"""
    t_popup_on = rng.uniform(0.20 * duration, 0.50 * duration)
    t_popup_off = min(t_popup_on + rng.uniform(0.12 * duration, 0.30 * duration), duration)
    t_target_on = rng.uniform(0.30 * duration, 0.60 * duration)
    t_target_off = min(t_target_on + rng.uniform(0.10 * duration, 0.25 * duration), duration)
    has_loot = rng.random() < 0.5
    if has_loot:
        loot_hi = min(0.85 * duration, duration)
        loot_lo = 0.55 * duration
        t_loot_on = rng.uniform(loot_lo, loot_hi) if loot_hi > loot_lo else duration
        t_loot_off = min(t_loot_on + rng.uniform(0.05 * duration, 0.15 * duration), duration)
    else:
        t_loot_on = duration
        t_loot_off = duration

    events: list[tuple[float, str]] = [
        (0.0, "scenario_started"),
        (t_target_on, "target_appeared"),
        (t_target_off, "target_disappeared"),
        (t_popup_on, "popup_opened"),
        (t_popup_off, "popup_closed"),
    ]
    if has_loot:
        events.append((t_loot_on, "loot_appeared"))
        events.append((t_loot_off, "loot_disappeared"))
    events.append((float(duration), "scenario_ended"))

    def state_at(t: float, frame_index: int) -> SceneState:
        return SceneState(
            health_ratio=1.0,
            resource_ratio=1.0,
            cooldown_ready=True,
            target_present=t_target_on <= t < t_target_off,
            loot_present=has_loot and (t_loot_on <= t < t_loot_off),
            loading_active=False,
            loading_progress=0.0,
            popup_open=t_popup_on <= t < t_popup_off,
            frame_index=frame_index,
        )

    return ScenarioPlan(events=tuple(events), state_at=state_at)


# 内置场景注册表：场景名 -> 计划构造器（rng, duration）-> ScenarioPlan。
BUILTIN_SCENARIOS: dict[str, Callable[[random.Random, float], ScenarioPlan]] = {
    "happy_path": _plan_happy_path,
    "loading_timeout": _plan_loading_timeout,
    "popup_random": _plan_popup_random,
}


def list_scenarios() -> tuple[str, ...]:
    """列出全部内置场景名（字典序）。"""
    return tuple(sorted(BUILTIN_SCENARIOS))


@dataclass(frozen=True)
class ScenarioRun:
    """一次场景运行的产物：状态序列、逐帧哈希、Oracle 轨迹。

    - ``frame_hashes[i]`` = 第 i 帧 RGB 字节的 sha256（hex）；
    - ``frames`` 仅在 with_frames=True 时保留（大分辨率长场景很占内存），
      否则可用 :meth:`iter_frames` 按需重渲染（确定性保证结果一致）。
    """

    name: str
    seed: int
    fps: float
    duration_s: float
    config: SceneConfig
    states: tuple[SceneState, ...]
    frame_hashes: tuple[str, ...]
    trace: OracleTrace
    frames: tuple[np.ndarray, ...] | None = field(default=None, compare=False)

    @property
    def frame_count(self) -> int:
        return len(self.states)

    def iter_frames(self) -> Iterator[np.ndarray]:
        """逐帧产出画面（优先返回已保留帧，否则用 Renderer 确定性重渲染）。"""
        if self.frames is not None:
            yield from self.frames
            return
        renderer = Renderer(self.config)
        for state in self.states:
            yield renderer.render(state)


def run_scenario(
    name: str,
    seed: int,
    resolution: tuple[int, int] = (1280, 720),
    fps: float = 30.0,
    duration_s: float = 10.0,
    *,
    ui_scale: float = 1.0,
    with_frames: bool = False,
) -> ScenarioRun:
    """执行内置场景：按 fps 生成帧序列 + OracleTrace（完全确定性）。

    同 (name, seed, resolution, fps, duration_s) 两次调用 -> 逐帧哈希一致；
    不同 seed -> 至少部分帧哈希不同（事件抖动 + 渲染种子色调）。
    """
    builder = BUILTIN_SCENARIOS.get(name)
    if builder is None:
        raise KeyError(f"未知场景 {name!r}；可用场景：{', '.join(list_scenarios())}")
    fps = float(fps)
    duration = float(duration_s)
    if not (fps > 0.0):
        raise ValueError(f"fps 必须为正数，收到 {fps!r}")
    if not (duration > 0.0):
        raise ValueError(f"duration_s 必须为正数，收到 {duration!r}")

    config = SceneConfig(seed=seed, resolution=resolution, ui_scale=ui_scale)
    plan = builder(random.Random(int(seed)), duration)
    renderer = Renderer(config)
    frame_count = max(1, int(round(duration * fps)))
    trace = OracleTrace(
        meta={
            "scenario": name,
            "seed": int(seed),
            "fps": fps,
            "duration_s": duration,
            "resolution": [config.resolution[0], config.resolution[1]],
            "ui_scale": config.ui_scale,
            "frame_count": frame_count,
        }
    )

    states: list[SceneState] = []
    frame_hashes: list[str] = []
    frames: list[np.ndarray] | None = [] if with_frames else None
    for index in range(frame_count):
        t = index / fps
        state = plan.state_at(t, index)
        trace.record_frame(t, state)
        frame = renderer.render(state)
        frame_hashes.append(hashlib.sha256(frame.tobytes()).hexdigest())
        states.append(state)
        if frames is not None:
            frames.append(frame)

    for t_event, event_name in sorted(plan.events, key=lambda item: item[0]):
        trace.record_event(event_name, t_event)

    return ScenarioRun(
        name=name,
        seed=int(seed),
        fps=fps,
        duration_s=duration,
        config=config,
        states=tuple(states),
        frame_hashes=tuple(frame_hashes),
        trace=trace,
        frames=tuple(frames) if frames is not None else None,
    )
