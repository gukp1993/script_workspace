"""ArenaLab M1 扩展单测（LAB-005/006/007）。

覆盖：

- 故障注入（LAB-006）：默认 faults=None 与 M0 行为逐字节一致；遮挡只改
  窗口内帧且事件成对；fps_drop 只改 Oracle 时间戳不改内容；input_delay
  经 record_input 记录；focus_lost / focus_restored 事件；非法计划与越界
  窗口报错；apply 与直接带 faults 重跑等价；meta 记录并可 JSON 往返；
- 分辨率/DPI 矩阵（LAB-005 基础）：三档分辨率 × 两档 ui_scale 的尺寸
  正确、同参数确定、元素尺寸按比例缩放；
- Oracle 导出（LAB-007）：save/load 文件往返确定性、export_summary 计数。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from arena_lab.faults import (
    EVENT_FPS_DROP_ENDED,
    EVENT_FPS_DROP_STARTED,
    EVENT_FOCUS_LOST,
    EVENT_FOCUS_RESTORED,
    EVENT_OCCLUSION_ENDED,
    EVENT_OCCLUSION_STARTED,
    FaultPlan,
    apply,
    default_occlusion_rect,
    occlusion_rects_at,
    record_input,
)
from arena_lab.oracle import OracleTrace
from arena_lab.render import (
    COLOR_OCCLUSION,
    SUPPORTED_RESOLUTIONS,
    Renderer,
    SceneConfig,
    SceneState,
    health_color,
)
from arena_lab.scenario import ScenarioRun, run_scenario

BASE_SEED = 5
RES = (640, 360)
FPS = 20.0
DURATION = 3.0


# ---------------------------------------------------------------- 助手


def _base_run(**kwargs: object) -> ScenarioRun:
    """共用基线运行（无故障）。"""
    return run_scenario("happy_path", BASE_SEED, RES, FPS, DURATION, **kwargs)  # type: ignore[arg-type]


def _event_names(trace: OracleTrace) -> list[str]:
    return [event.name for event in trace.events]


def _diff_mask(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.any(a != b, axis=-1)


def _fill_ratio_on_middle_row(
    renderer: Renderer, frame: np.ndarray, inner: tuple[int, int, int, int], color: tuple[int, int, int]
) -> float:
    """在矩形内中间行统计等于 color 的像素占比（量出条形填充宽度）。"""
    x0, y0, x1, y1 = inner
    row = frame[(y0 + y1) // 2, x0:x1]
    expected = np.array(color, dtype=np.uint8)
    filled = int(np.count_nonzero(np.all(row == expected, axis=-1)))
    return filled / (x1 - x0)


# ---------------------------------------------------------------- LAB-006：向后兼容


def test_default_and_none_faults_keep_m0_behavior() -> None:
    """faults 缺省 / None / 空 FaultPlan 三者与 M0 基线完全一致。"""
    base = _base_run()
    assert run_scenario("happy_path", BASE_SEED, RES, FPS, DURATION).frame_hashes == base.frame_hashes
    noop = run_scenario(
        "happy_path", BASE_SEED, RES, FPS, DURATION, faults=FaultPlan()
    )
    assert noop.frame_hashes == base.frame_hashes
    assert noop.states == base.states
    assert noop.trace.events == base.trace.events
    assert noop.trace.frames == base.trace.frames
    assert not FaultPlan().enabled


# ---------------------------------------------------------------- LAB-006：遮挡


def test_occlusion_changes_frames_only_within_window() -> None:
    """遮挡：窗口内帧哈希与无故障不同，窗口外逐帧相同，状态序列不变。"""
    base = _base_run()
    faulted = run_scenario(
        "happy_path",
        BASE_SEED,
        RES,
        FPS,
        DURATION,
        faults=FaultPlan(occlusion_from=1.0, occlusion_until=2.0),
    )
    assert faulted.states == base.states
    differing = [
        i
        for i, (a, b) in enumerate(zip(base.frame_hashes, faulted.frame_hashes))
        if a != b
    ]
    expected = [i for i in range(faulted.frame_count) if 1.0 <= i / FPS < 2.0]
    assert differing == expected


def test_occlusion_events_paired_and_bounded() -> None:
    """遮挡事件成对（started/ended 各一次）且时刻精确等于窗口边界。"""
    plan = FaultPlan(occlusion_from=1.0, occlusion_until=2.0)
    run = run_scenario("happy_path", BASE_SEED, RES, FPS, DURATION, faults=plan)
    started = [e for e in run.trace.events if e.name == EVENT_OCCLUSION_STARTED]
    ended = [e for e in run.trace.events if e.name == EVENT_OCCLUSION_ENDED]
    assert [(e.t_monotonic) for e in started] == [1.0]
    assert [(e.t_monotonic) for e in ended] == [2.0]
    # 场景事件不受故障影响（过滤掉故障事件后与基线一致）。
    kept = [
        e
        for e in run.trace.events
        if e.name not in (EVENT_OCCLUSION_STARTED, EVENT_OCCLUSION_ENDED)
    ]
    assert kept == list(run_scenario("happy_path", BASE_SEED, RES, FPS, DURATION).trace.events)


def test_occlusion_renders_solid_fill_inside_rect() -> None:
    """渲染入口：occlusion 传入后矩形内为确定性纯色遮挡，其余不受影响。"""
    renderer = Renderer(SceneConfig(seed=1, resolution=(320, 200)))
    rect = (10, 10, 60, 50)
    plain = renderer.render(SceneState(frame_index=2))
    occluded = renderer.render(SceneState(frame_index=2), occlusion=[rect])
    x0, y0, x1, y1 = rect
    assert np.all(occluded[y0:y1, x0:x1] == COLOR_OCCLUSION)
    mask = np.zeros(plain.shape[:2], dtype=bool)
    mask[y0:y1, x0:x1] = True
    assert np.array_equal(occluded[~mask], plain[~mask])
    assert not np.array_equal(occluded[mask], plain[mask])


def test_occlusion_custom_rect_scoped_to_region() -> None:
    """自定义遮挡矩形：帧差异只出现在该矩形内且覆盖绝大部分区域。"""
    rect = (20, 20, 120, 90)
    plan = FaultPlan(occlusion_from=0.0, occlusion_until=0.2, occlusion_rect=rect)
    base = run_scenario("happy_path", BASE_SEED, RES, FPS, DURATION, with_frames=True)
    faulted = run_scenario(
        "happy_path", BASE_SEED, RES, FPS, DURATION, with_frames=True, faults=plan
    )
    base_frame = base.frames[0]  # type: ignore[index]
    faulted_frame = faulted.frames[0]  # type: ignore[index]
    diff = _diff_mask(base_frame, faulted_frame)
    x0, y0, x1, y1 = rect
    outside = diff.copy()
    outside[y0:y1, x0:x1] = False
    assert not outside.any()
    inside_ratio = float(diff[y0:y1, x0:x1].mean())
    assert inside_ratio > 0.9


def test_occlusion_rects_at_helper_and_default_rect() -> None:
    """occlusion_rects_at：窗口外为空；窗口内为自定义或按分辨率派生矩形。"""
    resolution = (800, 600)
    plan = FaultPlan(
        occlusion_from=1.0, occlusion_until=2.0, occlusion_rect=(1, 2, 3, 4)
    )
    assert occlusion_rects_at(None, 1.5, resolution) == ()
    assert occlusion_rects_at(plan, 0.5, resolution) == ()
    assert occlusion_rects_at(plan, 1.5, resolution) == ((1, 2, 3, 4),)
    bare = FaultPlan(occlusion_from=0.0, occlusion_until=1.0)
    assert occlusion_rects_at(bare, 0.5, resolution) == (default_occlusion_rect(resolution),)
    w, h = resolution
    x0, y0, x1, y1 = default_occlusion_rect(resolution)
    assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h  # 派生矩形落在画面内


# ---------------------------------------------------------------- LAB-006：掉帧


def test_fps_drop_changes_only_timestamps() -> None:
    """掉帧：状态/帧内容/场景事件不变，窗口内 Oracle 帧间隔拉大到 1/fps_drop_to。"""
    base = _base_run()
    plan = FaultPlan(fps_drop_from=0.5, fps_drop_until=1.5, fps_drop_to=10.0)
    faulted = run_scenario(
        "happy_path", BASE_SEED, RES, FPS, DURATION, faults=plan
    )
    assert faulted.states == base.states
    assert faulted.frame_hashes == base.frame_hashes
    fault_only = (EVENT_FPS_DROP_STARTED, EVENT_FPS_DROP_ENDED)
    assert [e for e in faulted.trace.events if e.name not in fault_only] == list(
        base.trace.events
    )
    # 时间戳：窗口内间隔 0.1s（=1/10），窗口外 0.05s（=1/20），帧数不变。
    ts = [record.t_monotonic for record in faulted.trace.frames]
    assert len(ts) == base.frame_count and ts[0] == 0.0
    for index in range(len(ts) - 1):
        expected = 0.1 if 0.5 <= index / FPS < 1.5 else 0.05
        assert abs(ts[index + 1] - ts[index] - expected) < 1e-6


def test_fps_drop_events_recorded() -> None:
    """掉帧起止写入离散事件，各一次且时刻精确。"""
    run = run_scenario(
        "happy_path",
        BASE_SEED,
        RES,
        FPS,
        DURATION,
        faults=FaultPlan(fps_drop_from=0.5, fps_drop_until=1.5, fps_drop_to=10.0),
    )
    started = [e.t_monotonic for e in run.trace.events if e.name == EVENT_FPS_DROP_STARTED]
    ended = [e.t_monotonic for e in run.trace.events if e.name == EVENT_FPS_DROP_ENDED]
    assert started == [0.5]
    assert ended == [1.5]


def test_fps_drop_deterministic() -> None:
    """掉帧运行同样确定：同参数两遍帧哈希与时间戳完全一致。"""
    plan = FaultPlan(fps_drop_from=0.5, fps_drop_until=1.5, fps_drop_to=10.0)
    a = run_scenario("happy_path", BASE_SEED, RES, FPS, DURATION, faults=plan)
    b = run_scenario("happy_path", BASE_SEED, RES, FPS, DURATION, faults=plan)
    assert a.frame_hashes == b.frame_hashes
    assert a.trace.frames == b.trace.frames
    assert a.trace.events == b.trace.events


# ---------------------------------------------------------------- LAB-006：输入延迟 / 失焦


def test_input_delay_recorded_by_helper() -> None:
    """record_input：计划带 input_delay_ms 时参数中记录 delay_ms。"""
    trace = OracleTrace()
    plan = FaultPlan(input_delay_ms=250)
    record_input(plan, trace, 1.0, "click", button="left", x=3, y=4)
    record = trace.inputs[0]
    assert record.t_monotonic == 1.0 and record.action == "click"
    assert record.params["delay_ms"] == 250.0
    assert record.params["button"] == "left" and record.params["x"] == 3


def test_input_delay_absent_without_fault() -> None:
    """无延迟计划（None 或 0ms）时记录内容与 OracleTrace.record_input 一致。"""
    for plan in (None, FaultPlan()):
        trace = OracleTrace()
        record_input(plan, trace, 0.5, "key_down", key="a")
        assert trace.inputs[0].params == {"key": "a"}
        assert "delay_ms" not in trace.inputs[0].params


def test_focus_loss_event_only() -> None:
    """focus_loss_at：写入唯一 focus_lost 事件，无恢复事件。"""
    run = run_scenario(
        "happy_path", BASE_SEED, RES, FPS, DURATION, faults=FaultPlan(focus_loss_at=1.25)
    )
    lost = [e for e in run.trace.events if e.name == EVENT_FOCUS_LOST]
    restored = [e for e in run.trace.events if e.name == EVENT_FOCUS_RESTORED]
    assert [(e.t_monotonic) for e in lost] == [1.25]
    assert restored == []


def test_focus_loss_and_restore_events_paired() -> None:
    """focus_loss 窗口：focus_lost 与 focus_restored 成对且时刻精确。"""
    plan = FaultPlan(focus_loss_at=1.0, focus_loss_until=2.25)
    run = run_scenario("happy_path", BASE_SEED, RES, FPS, DURATION, faults=plan)
    lost = [e.t_monotonic for e in run.trace.events if e.name == EVENT_FOCUS_LOST]
    restored = [e.t_monotonic for e in run.trace.events if e.name == EVENT_FOCUS_RESTORED]
    assert lost == [1.0]
    assert restored == [2.25]


# ---------------------------------------------------------------- LAB-006：校验与组合


def test_invalid_fault_plan_rejected() -> None:
    """非法 FaultPlan：窗口不成对 / 起点不小于终点 / 负延迟 / 非正目标帧率。"""
    with pytest.raises(ValueError):
        FaultPlan(occlusion_from=1.0)  # 缺终点
    with pytest.raises(ValueError):
        FaultPlan(occlusion_from=2.0, occlusion_until=1.0)
    with pytest.raises(ValueError):
        FaultPlan(fps_drop_from=1.0)  # 缺终点
    with pytest.raises(ValueError):
        FaultPlan(fps_drop_to=0.0)
    with pytest.raises(ValueError):
        FaultPlan(input_delay_ms=-1.0)
    with pytest.raises(ValueError):
        FaultPlan(focus_loss_until=2.0)  # 缺起点
    with pytest.raises(ValueError):
        FaultPlan(focus_loss_at=2.0, focus_loss_until=1.0)


def test_fault_window_outside_duration_rejected() -> None:
    """故障时刻超出场景时长 / 目标帧率不低于场景帧率：run_scenario 报错。"""
    with pytest.raises(ValueError):
        run_scenario(
            "happy_path",
            BASE_SEED,
            RES,
            FPS,
            DURATION,
            faults=FaultPlan(occlusion_from=1.0, occlusion_until=5.0),
        )
    with pytest.raises(ValueError):
        run_scenario(
            "happy_path",
            BASE_SEED,
            RES,
            FPS,
            DURATION,
            faults=FaultPlan(focus_loss_at=3.5),
        )
    for drop_to in (FPS, FPS + 10.0):  # 等于或高于场景帧率都不是"掉帧"
        with pytest.raises(ValueError):
            run_scenario(
                "happy_path",
                BASE_SEED,
                RES,
                FPS,
                DURATION,
                faults=FaultPlan(
                    fps_drop_from=0.5, fps_drop_until=1.0, fps_drop_to=drop_to
                ),
            )


def test_combined_faults_all_events_present() -> None:
    """多故障组合：遮挡 + 掉帧 + 延迟 + 失焦同时注入，事件与 meta 齐全。"""
    plan = FaultPlan(
        occlusion_from=0.5,
        occlusion_until=1.0,
        fps_drop_from=1.0,
        fps_drop_until=2.0,
        fps_drop_to=8.0,
        input_delay_ms=120,
        focus_loss_at=2.5,
        focus_loss_until=2.75,
    )
    run = run_scenario("happy_path", BASE_SEED, RES, FPS, DURATION, faults=plan)
    names = _event_names(run.trace)
    for name, count in (
        (EVENT_OCCLUSION_STARTED, 1),
        (EVENT_OCCLUSION_ENDED, 1),
        (EVENT_FPS_DROP_STARTED, 1),
        (EVENT_FPS_DROP_ENDED, 1),
        (EVENT_FOCUS_LOST, 1),
        (EVENT_FOCUS_RESTORED, 1),
    ):
        assert names.count(name) == count
    assert run.trace.meta["faults"]["input_delay_ms"] == 120.0
    assert run.frame_count == _base_run().frame_count  # 帧数不受故障影响


def test_apply_matches_direct_faulted_run() -> None:
    """apply(plan, run) 与 run_scenario(..., faults=plan) 完全等价。"""
    plan = FaultPlan(
        occlusion_from=1.0, occlusion_until=1.5, fps_drop_from=2.0, fps_drop_until=2.5
    )
    base = _base_run()
    applied = apply(plan, base)
    direct = run_scenario(
        "happy_path", BASE_SEED, RES, FPS, DURATION, faults=plan
    )
    assert applied.frame_hashes == direct.frame_hashes
    assert applied.states == direct.states
    assert applied.trace.frames == direct.trace.frames
    assert applied.trace.events == direct.trace.events


def test_faults_meta_recorded_and_json_roundtrip() -> None:
    """故障计划写入 meta 且经 to_json/from_json 往返保持不变。"""
    plan = FaultPlan(
        occlusion_from=1.0,
        occlusion_until=2.0,
        input_delay_ms=250,
        focus_loss_at=0.5,
    )
    run = run_scenario("happy_path", BASE_SEED, RES, FPS, DURATION, faults=plan)
    meta_faults = run.trace.meta["faults"]
    assert meta_faults["occlusion_from_s"] == 1.0
    assert meta_faults["input_delay_ms"] == 250.0
    restored = OracleTrace.from_json(run.trace.to_json())
    assert restored.meta["faults"] == meta_faults
    assert json.loads(run.trace.to_json())["meta"]["faults"] == meta_faults


def test_iter_frames_reproduces_occluded_frames() -> None:
    """未保留帧的故障运行：iter_frames 重渲染与 frame_hashes 一致（含遮挡）。"""
    run = run_scenario(
        "happy_path",
        BASE_SEED,
        RES,
        FPS,
        DURATION,
        faults=FaultPlan(occlusion_from=1.0, occlusion_until=2.0),
    )
    assert run.frames is None
    rerendered = [
        hashlib.sha256(frame.tobytes()).hexdigest() for frame in run.iter_frames()
    ]
    assert rerendered == list(run.frame_hashes)


# ---------------------------------------------------------------- LAB-005：分辨率 × ui_scale 矩阵


@pytest.mark.parametrize("resolution", SUPPORTED_RESOLUTIONS)
@pytest.mark.parametrize("ui_scale", [1.0, 2.0])
def test_ui_scale_matrix_shape_and_determinism(
    resolution: tuple[int, int], ui_scale: float
) -> None:
    """三档分辨率 × 两档 ui_scale：尺寸正确、同参数确定、输出 dtype 正确。"""
    a = run_scenario(
        "happy_path", 21, resolution, 20.0, 0.5, ui_scale=ui_scale
    )
    b = run_scenario(
        "happy_path", 21, resolution, 20.0, 0.5, ui_scale=ui_scale
    )
    assert a.frame_hashes == b.frame_hashes
    assert a.frame_count == 10
    assert a.config == SceneConfig(seed=21, resolution=resolution, ui_scale=ui_scale)
    probe = Renderer(a.config).render(a.states[0])
    assert probe.shape == (resolution[1], resolution[0], 3)
    assert probe.dtype == np.uint8


def test_ui_scale_scales_element_sizes() -> None:
    """ui_scale 放大受控元素：血条/图标尺寸近似翻倍，填充测量仍精确。"""
    r1 = Renderer(SceneConfig(seed=7, resolution=(1280, 720), ui_scale=1.0))
    r2 = Renderer(SceneConfig(seed=7, resolution=(1280, 720), ui_scale=2.0))
    bar_h1 = r1.health_bar_rect()[3] - r1.health_bar_rect()[1]
    bar_h2 = r2.health_bar_rect()[3] - r2.health_bar_rect()[1]
    icon_w1 = r1.cooldown_icon_rects()[0][2] - r1.cooldown_icon_rects()[0][0]
    icon_w2 = r2.cooldown_icon_rects()[0][2] - r2.cooldown_icon_rects()[0][0]
    assert 1.8 <= bar_h2 / bar_h1 <= 2.2
    assert 1.8 <= icon_w2 / icon_w1 <= 2.2
    # 缩放不影响填充比例测量（0.5 仍是半条），且帧内容随 scale 可区分。
    frame = r2.render(SceneState(health_ratio=0.5, frame_index=1))
    measured = _fill_ratio_on_middle_row(
        r2, frame, r2.health_bar_inner_rect(), health_color(0.5)
    )
    assert abs(measured - 0.5) <= 0.02
    assert hashlib.sha256(frame.tobytes()).hexdigest() != hashlib.sha256(
        r1.render(SceneState(health_ratio=0.5, frame_index=1)).tobytes()
    ).hexdigest()


# ---------------------------------------------------------------- LAB-007：Oracle 导出


def test_oracle_save_load_roundtrip(tmp_path: Path) -> None:
    """save/load：文件往返后 to_json 与原轨迹逐字节一致；缺文件报错。"""
    run = run_scenario(
        "happy_path",
        BASE_SEED,
        RES,
        FPS,
        DURATION,
        faults=FaultPlan(occlusion_from=1.0, occlusion_until=2.0),
    )
    target = tmp_path / "nested" / "oracle.json"
    written = run.trace.save(target)
    assert written == target and target.is_file()
    json.loads(target.read_text(encoding="utf-8"))  # 合法 JSON
    loaded = OracleTrace.load(target)
    assert loaded.to_json() == run.trace.to_json()
    assert loaded.events == run.trace.events
    assert loaded.frames == run.trace.frames
    with pytest.raises(FileNotFoundError):
        OracleTrace.load(tmp_path / "missing.json")


def test_export_summary_counts() -> None:
    """export_summary：事件计数 / 时长 / 帧数 / 输入数正确且可 JSON 化。"""
    trace = OracleTrace()
    trace.record_event("popup_opened", 0.0)
    trace.record_event("popup_closed", 1.0)
    trace.record_event("popup_opened", 2.0)
    trace.record_frame(0.0, SceneState(frame_index=0))
    trace.record_frame(0.5, SceneState(frame_index=1))
    trace.record_frame(1.0, SceneState(frame_index=2))
    trace.record_input(0.25, "click", x=1, y=2)
    summary = trace.export_summary()
    assert summary["event_counts"] == {"popup_closed": 1, "popup_opened": 2}
    assert summary["event_count"] == 3
    assert summary["frame_count"] == 3
    assert summary["input_count"] == 1
    assert summary["duration_s"] == 2.0
    json.dumps(summary)  # 必须可序列化

    run = _base_run()
    run_summary = run.trace.export_summary()
    assert run_summary["frame_count"] == run.frame_count
    assert run_summary["event_counts"]["scenario_started"] == 1
    assert run_summary["event_counts"]["scenario_ended"] == 1
    assert abs(run_summary["duration_s"] - DURATION) < 1e-9
