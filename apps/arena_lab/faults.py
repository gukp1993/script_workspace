"""故障注入（LAB-006）。

以声明式的 :class:`FaultPlan` 描述一次运行要注入的故障，配合
:func:`arena_lab.scenario.run_scenario` 的可选 ``faults`` 参数使用
（默认 ``None`` 时行为与 M0 完全一致，向后兼容）：

- ``occlusion_from`` / ``occlusion_until``（场景时间窗，秒）：窗口内每帧
  在渲染最上层叠加确定性纯色遮挡块（默认位置由分辨率派生，也可用
  ``occlusion_rect`` 指定）；渲染侧入口是
  ``Renderer.render(state, occlusion=[rect, ...])``；
- ``fps_drop_from`` / ``fps_drop_until`` + ``fps_drop_to``：窗口内把
  Oracle 记录的帧时间戳间隔拉大到 ``1 / fps_drop_to``，**帧内容（状态、
  像素哈希）与无故障运行逐字节一致**——只改时间不改内容；
- ``input_delay_ms``：通过 :func:`record_input` 记录注入输入时，
  在参数中写入 ``delay_ms``（ArenaLab 只记录输入，绝不产生真实输入）；
- ``focus_loss_at``（可选 ``focus_loss_until``）：向 Oracle 写入
  ``focus_lost``（及可选 ``focus_restored``）事件，模拟窗口失焦。

每个故障的起止都会作为离散事件写入 Oracle（``occlusion_started`` /
``occlusion_ended`` / ``fps_drop_started`` / ``fps_drop_ended`` /
``focus_lost`` / ``focus_restored``），供测试精确断言。

:func:`apply` 可以把一个 FaultPlan 应用到已生成的
:class:`~arena_lab.scenario.ScenarioRun` 上（等价于带 faults 重跑）。
所有注入都不修改被测系统代码：故障只发生在 ArenaLab 模拟器侧。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arena_lab.render import Rect

if TYPE_CHECKING:  # 仅为类型注解（避免与 scenario 循环导入）
    from arena_lab.oracle import OracleTrace
    from arena_lab.scenario import ScenarioRun

__all__ = [
    "FaultPlan",
    "EVENT_OCCLUSION_STARTED",
    "EVENT_OCCLUSION_ENDED",
    "EVENT_FPS_DROP_STARTED",
    "EVENT_FPS_DROP_ENDED",
    "EVENT_FOCUS_LOST",
    "EVENT_FOCUS_RESTORED",
    "apply",
    "record_input",
    "default_occlusion_rect",
    "occlusion_rects_at",
    "check_fault_plan",
]

# 故障起止的离散事件名（与 Oracle 事件共用命名空间，测试据此精确断言）。
EVENT_OCCLUSION_STARTED = "occlusion_started"
EVENT_OCCLUSION_ENDED = "occlusion_ended"
EVENT_FPS_DROP_STARTED = "fps_drop_started"
EVENT_FPS_DROP_ENDED = "fps_drop_ended"
EVENT_FOCUS_LOST = "focus_lost"
EVENT_FOCUS_RESTORED = "focus_restored"

# 时间比较容差（与 Oracle 单调性校验一致）。
_EPS = 1e-9


def _validate_window(label: str, start: float | None, end: float | None) -> None:
    """校验一个 [start, end) 故障时间窗：必须成对出现且 0 <= start < end。"""
    if (start is None) != (end is None):
        raise ValueError(f"{label} 窗口必须同时提供起点与终点，收到 ({start!r}, {end!r})")
    if start is None or end is None:
        return
    if not (0.0 <= start < end):
        raise ValueError(f"{label} 窗口无效：要求 0 <= 起点 < 终点，收到 [{start!r}, {end!r})")


@dataclass(frozen=True)
class FaultPlan:
    """一次运行要注入的故障集合（全部字段可选；空计划 = 无故障）。

    时间一律为场景单调时间（秒，与 Oracle ``t_monotonic`` 同一坐标系），
    与真实墙钟无关，保证确定性。
    """

    # 遮挡窗口（秒）与可选遮挡矩形；缺省矩形由分辨率确定性派生。
    occlusion_from: float | None = None
    occlusion_until: float | None = None
    occlusion_rect: Rect | None = None

    # 掉帧窗口（秒）与窗口内目标帧率：帧时间戳间隔拉大，内容不变。
    fps_drop_from: float | None = None
    fps_drop_until: float | None = None
    fps_drop_to: float = 10.0

    # 注入输入延迟（毫秒）：经 record_input 记录到每条输入的参数里。
    input_delay_ms: float = 0.0

    # 失焦时刻（必填才生效）与可选的恢复时刻。
    focus_loss_at: float | None = None
    focus_loss_until: float | None = None

    def __post_init__(self) -> None:
        # 数值规范化：时间窗字段统一转 float，杜绝 int/float 混用差异。
        for name in (
            "occlusion_from",
            "occlusion_until",
            "fps_drop_from",
            "fps_drop_until",
            "focus_loss_at",
            "focus_loss_until",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, float(value))
        object.__setattr__(self, "fps_drop_to", float(self.fps_drop_to))
        object.__setattr__(self, "input_delay_ms", float(self.input_delay_ms))

        _validate_window("occlusion", self.occlusion_from, self.occlusion_until)
        _validate_window("fps_drop", self.fps_drop_from, self.fps_drop_until)
        if not (self.fps_drop_to > 0.0):
            raise ValueError(f"fps_drop_to 必须为正数，收到 {self.fps_drop_to!r}")
        if self.input_delay_ms < 0.0:
            raise ValueError(f"input_delay_ms 不能为负，收到 {self.input_delay_ms!r}")
        if self.focus_loss_until is not None and self.focus_loss_at is None:
            raise ValueError("focus_loss_until 必须与 focus_loss_at 成对提供")
        if self.focus_loss_at is not None:
            if self.focus_loss_at < 0.0:
                raise ValueError(f"focus_loss_at 不能为负，收到 {self.focus_loss_at!r}")
            if self.focus_loss_until is not None and not (
                self.focus_loss_at < self.focus_loss_until
            ):
                raise ValueError(
                    "focus_loss 窗口无效：要求 focus_loss_at < focus_loss_until，"
                    f"收到 ({self.focus_loss_at!r}, {self.focus_loss_until!r})"
                )

    # ---- 查询 ------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """是否注入了任何故障（空计划为 False，等价于无故障运行）。"""
        return (
            self.occlusion_from is not None
            or self.fps_drop_from is not None
            or self.input_delay_ms > 0.0
            or self.focus_loss_at is not None
        )

    def occlusion_active(self, t_monotonic: float) -> bool:
        """时刻 t 是否处于遮挡窗口内（半开区间 [from, until)）。"""
        return (
            self.occlusion_from is not None
            and self.occlusion_until is not None
            and self.occlusion_from <= t_monotonic < self.occlusion_until
        )

    def fps_drop_active(self, t_monotonic: float) -> bool:
        """时刻 t 是否处于掉帧窗口内（半开区间 [from, until)）。"""
        return (
            self.fps_drop_from is not None
            and self.fps_drop_until is not None
            and self.fps_drop_from <= t_monotonic < self.fps_drop_until
        )

    def describe(self) -> dict[str, Any]:
        """导出 JSON 安全的故障描述（写入 Oracle meta，供断言/审计）。"""
        return {
            "occlusion_from_s": self.occlusion_from,
            "occlusion_until_s": self.occlusion_until,
            "occlusion_rect": (
                list(self.occlusion_rect) if self.occlusion_rect is not None else None
            ),
            "fps_drop_from_s": self.fps_drop_from,
            "fps_drop_until_s": self.fps_drop_until,
            "fps_drop_to_fps": self.fps_drop_to,
            "input_delay_ms": self.input_delay_ms,
            "focus_loss_at_s": self.focus_loss_at,
            "focus_loss_until_s": self.focus_loss_until,
        }


def default_occlusion_rect(resolution: tuple[int, int]) -> Rect:
    """按分辨率确定性派生的默认遮挡矩形（画面中央偏上的大块）。"""
    width, height = int(resolution[0]), int(resolution[1])
    return (int(width * 0.34), int(height * 0.24), int(width * 0.66), int(height * 0.60))


def occlusion_rects_at(
    faults: FaultPlan | None, t_monotonic: float, resolution: tuple[int, int]
) -> tuple[Rect, ...]:
    """时刻 t 应叠加的遮挡矩形序列（窗口外为空）——渲染与重渲染共用。"""
    if faults is None or not faults.occlusion_active(t_monotonic):
        return ()
    rect = faults.occlusion_rect or default_occlusion_rect(resolution)
    return (rect,)


def check_fault_plan(faults: FaultPlan | None, duration_s: float, fps: float) -> None:
    """结合具体运行参数校验 FaultPlan（越界/矛盾配置尽早报错）。

    - 所有故障时刻必须落在 [0, duration_s] 内；
    - fps_drop_to 必须 < 场景 fps（"降帧"只能往下调）。
    """
    if faults is None:
        return
    times: tuple[tuple[str, float], ...] = tuple(
        (label, value)
        for label, value in (
            ("occlusion_from", faults.occlusion_from),
            ("occlusion_until", faults.occlusion_until),
            ("fps_drop_from", faults.fps_drop_from),
            ("fps_drop_until", faults.fps_drop_until),
            ("focus_loss_at", faults.focus_loss_at),
            ("focus_loss_until", faults.focus_loss_until),
        )
        if value is not None
    )
    for label, value in times:
        if not (-_EPS <= value <= duration_s + _EPS):
            raise ValueError(
                f"故障时刻 {label}={value!r} 超出场景时长 [0, {duration_s!r}]"
            )
    if faults.fps_drop_from is not None and not (faults.fps_drop_to < float(fps)):
        raise ValueError(
            f"fps_drop_to 必须小于场景帧率 {fps!r}，收到 {faults.fps_drop_to!r}"
        )


def fault_events(faults: FaultPlan | None) -> tuple[tuple[float, str], ...]:
    """把 FaultPlan 的故障起止展开为 (t_monotonic, 事件名) 序列。"""
    if faults is None:
        return ()
    events: list[tuple[float, str]] = []
    if faults.occlusion_from is not None and faults.occlusion_until is not None:
        events.append((faults.occlusion_from, EVENT_OCCLUSION_STARTED))
        events.append((faults.occlusion_until, EVENT_OCCLUSION_ENDED))
    if faults.fps_drop_from is not None and faults.fps_drop_until is not None:
        events.append((faults.fps_drop_from, EVENT_FPS_DROP_STARTED))
        events.append((faults.fps_drop_until, EVENT_FPS_DROP_ENDED))
    if faults.focus_loss_at is not None:
        events.append((faults.focus_loss_at, EVENT_FOCUS_LOST))
        if faults.focus_loss_until is not None:
            events.append((faults.focus_loss_until, EVENT_FOCUS_RESTORED))
    return tuple(events)


def record_input(
    faults: FaultPlan | None,
    trace: OracleTrace,
    t_monotonic: float,
    action: str,
    **params: Any,
) -> None:
    """故障感知的输入记录（LAB-006/007）：仅记录，绝不执行真实输入。

    ``faults.input_delay_ms > 0`` 时在参数中写入 ``delay_ms``（毫秒），
    表示该输入被延迟注入；否则与 ``OracleTrace.record_input`` 完全一致。
    """
    if faults is not None and faults.input_delay_ms > 0.0:
        params = {"delay_ms": faults.input_delay_ms, **params}
    trace.record_input(t_monotonic, action, **params)


def apply(plan: FaultPlan, run: ScenarioRun) -> ScenarioRun:
    """把故障计划应用到一次已完成的运行上（等价于带 faults 重跑）。

    复用原运行的 (name, seed, resolution, fps, duration_s, ui_scale)，
    保持帧内容真值不变，仅注入 plan 描述的故障。
    """
    from arena_lab.scenario import run_scenario  # 局部导入避免循环依赖

    return run_scenario(
        run.name,
        run.seed,
        run.config.resolution,
        run.fps,
        run.duration_s,
        ui_scale=run.config.ui_scale,
        with_frames=run.frames is not None,
        faults=plan,
    )
