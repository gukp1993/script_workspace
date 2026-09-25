"""ArenaLab（LAB-001/002/004 + LAB-007 M0 子集）单测。

覆盖：确定性（同种子两遍/异种子/跨进程/selfcheck CLI）、渲染正确性
（血条/资源条/冷却图标/目标/掉落/弹窗/加载画面/帧号的像素级量化）、
Oracle（事件单调、loading 成对、输入记录、JSON 往返）、分辨率 shape。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from arena_lab.oracle import OracleTrace
from arena_lab.render import (
    COLOR_PROGRESS_FILL,
    COLOR_RESOURCE_FILL,
    SUPPORTED_RESOLUTIONS,
    Renderer,
    SceneConfig,
    SceneState,
    health_color,
)
from arena_lab.scenario import list_scenarios, run_scenario

ROOT = Path(__file__).resolve().parents[2]
BASE_SEED = 20260925


# ---------------------------------------------------------------- 助手


def _make_renderer() -> Renderer:
    """默认 720p 渲染器（多数像素量化测试共用）。"""
    return Renderer(SceneConfig(seed=BASE_SEED, resolution=(1280, 720)))


def _region_mask(shape: tuple[int, ...], rects: Sequence[tuple[int, int, int, int]]) -> np.ndarray:
    """把若干 (x0,y0,x1,y1) 矩形合成一张布尔区域掩码。"""
    mask = np.zeros(shape[:2], dtype=bool)
    for x0, y0, x1, y1 in rects:
        mask[y0:y1, x0:x1] = True
    return mask


def _diff_mask(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐像素是否有差异（任一通道不同即算）。"""
    return np.any(a != b, axis=-1)


def _fill_ratio_on_middle_row(
    renderer: Renderer, frame: np.ndarray, inner: tuple[int, int, int, int], color: tuple[int, int, int]
) -> float:
    """在矩形内中间行统计等于 color 的像素占比（用于量出条形填充宽度）。"""
    x0, y0, x1, y1 = inner
    row = frame[(y0 + y1) // 2, x0:x1]
    expected = np.array(color, dtype=np.uint8)
    filled = int(np.count_nonzero(np.all(row == expected, axis=-1)))
    return filled / (x1 - x0)


def _run_python(args: list[str], code: str | None = None) -> subprocess.CompletedProcess[str]:
    """在仓库根目录以注入 PYTHONPATH 的方式起子进程跑 Python。"""
    cmd = [sys.executable, *args]
    if code is not None:
        cmd += ["-c", code]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(["packages", "services", "apps"])
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


_CROSS_PROCESS_SNIPPET = (
    "import hashlib\n"
    "from arena_lab.scenario import run_scenario\n"
    "run = run_scenario('happy_path', 7, (1280, 720), 10.0, 0.5)\n"
    "print(hashlib.sha256('|'.join(run.frame_hashes).encode('utf-8')).hexdigest())\n"
)


# ---------------------------------------------------------------- 确定性


def test_same_seed_two_runs_frame_hashes_identical() -> None:
    """同 (name, seed, resolution, fps, duration) 两遍：逐帧哈希与状态全同。"""
    a = run_scenario("happy_path", 42, (1280, 720), 20.0, 3.0)
    b = run_scenario("happy_path", 42, (1280, 720), 20.0, 3.0)
    assert a.frame_hashes == b.frame_hashes
    assert a.states == b.states
    assert a.trace.events == b.trace.events
    assert a.frame_count == 60


def test_different_seed_changes_frames() -> None:
    """异种子：至少部分帧哈希不同（事件抖动 + 渲染种子色调）。"""
    a = run_scenario("happy_path", 42, (1280, 720), 20.0, 3.0)
    b = run_scenario("happy_path", 43, (1280, 720), 20.0, 3.0)
    differing = [i for i, (x, y) in enumerate(zip(a.frame_hashes, b.frame_hashes)) if x != y]
    assert differing
    assert a.frame_hashes != b.frame_hashes


def test_same_seed_cross_process_identical() -> None:
    """同种子跨进程两次运行：帧哈希指纹完全一致。"""
    outs = [_run_python([], code=_CROSS_PROCESS_SNIPPET).stdout.strip() for _ in range(2)]
    assert outs[0] == outs[1]
    assert len(outs[0]) == 64  # sha256 hex


def test_selfcheck_cli_passes() -> None:
    """python -m arena_lab.selfcheck：退出码 0，末行输出 SELFCHECK PASS。"""
    proc = _run_python(["-m", "arena_lab.selfcheck", "--frames", "24"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    last_line = proc.stdout.strip().splitlines()[-1]
    assert last_line.startswith("SELFCHECK PASS:")
    assert "frames verified" in last_line


def test_iter_frames_rerender_matches_hashes() -> None:
    """未保留帧时 iter_frames 重渲染：逐帧 sha256 与 frame_hashes 一致。"""
    run = run_scenario("happy_path", 15, (1280, 720), 10.0, 1.2)
    assert run.frames is None
    rerendered = [hashlib.sha256(frame.tobytes()).hexdigest() for frame in run.iter_frames()]
    assert rerendered == list(run.frame_hashes)


# ---------------------------------------------------------------- 渲染正确性


@pytest.mark.parametrize("ratio", [0.0, 0.5, 1.0])
def test_health_bar_fill_ratio_matches_state(ratio: float) -> None:
    """血条：从帧中量出的填充宽度比例与 health_ratio 偏差 <= 0.02。"""
    renderer = _make_renderer()
    frame = renderer.render(SceneState(health_ratio=ratio))
    measured = _fill_ratio_on_middle_row(
        renderer, frame, renderer.health_bar_inner_rect(), health_color(ratio)
    )
    assert abs(measured - ratio) <= 0.02


def test_health_color_interpolates_red_to_green() -> None:
    """血条颜色随比例从红到绿插值：低血红、满血红绿关系相反。"""
    low = health_color(0.0)
    high = health_color(1.0)
    assert low[0] > low[1]  # 低血量偏红
    assert high[1] > high[0]  # 满血偏绿
    mid = health_color(0.5)
    assert mid != low and mid != high


@pytest.mark.parametrize("ratio", [0.0, 0.3, 1.0])
def test_resource_bar_fill_ratio_matches_state(ratio: float) -> None:
    """资源条：量出的蓝色填充宽度比例与 resource_ratio 偏差 <= 0.02。"""
    renderer = _make_renderer()
    frame = renderer.render(SceneState(resource_ratio=ratio))
    measured = _fill_ratio_on_middle_row(
        renderer, frame, renderer.resource_bar_inner_rect(), COLOR_RESOURCE_FILL
    )
    assert abs(measured - ratio) <= 0.02


def test_cooldown_icon_region_distinguishes_ready_and_cooling() -> None:
    """冷却图标：就绪/冷却两帧差异只出现在图标区域且幅度显著。"""
    renderer = _make_renderer()
    ready = renderer.render(SceneState(cooldown_ready=True))
    cooling = renderer.render(SceneState(cooldown_ready=False))
    region = _region_mask(ready.shape, renderer.cooldown_icon_rects())
    diff = _diff_mask(ready, cooling)
    assert np.array_equal(diff, region)  # 图标区内逐像素不同，区外完全相同
    mean_abs = float(np.abs(ready[region].astype(int) - cooling[region].astype(int)).mean())
    assert mean_abs > 50.0  # 亮色块 vs 暗色块，像素级区分明显


def test_target_marker_region_distinguishes() -> None:
    """目标标记：出现/消失差异只出现在目标区域（画面中央方块）。"""
    renderer = _make_renderer()
    present = renderer.render(SceneState(target_present=True))
    absent = renderer.render(SceneState(target_present=False))
    region = _region_mask(present.shape, [renderer.target_rect()])
    diff = _diff_mask(present, absent)
    assert np.array_equal(diff, region)
    assert diff.any()


def test_loot_marker_region_distinguishes() -> None:
    """掉落标记：出现/消失差异只出现在掉落区域。"""
    renderer = _make_renderer()
    present = renderer.render(SceneState(loot_present=True))
    absent = renderer.render(SceneState(loot_present=False))
    region = _region_mask(present.shape, [renderer.loot_rect()])
    diff = _diff_mask(present, absent)
    assert np.array_equal(diff, region)
    assert diff.any()


def test_popup_dialog_region_distinguishes() -> None:
    """弹窗：打开/关闭差异只出现在弹窗矩形（上层框、含明确边框色）。"""
    renderer = _make_renderer()
    opened = renderer.render(SceneState(popup_open=True))
    closed = renderer.render(SceneState(popup_open=False))
    region = _region_mask(opened.shape, [renderer.popup_rect()])
    diff = _diff_mask(opened, closed)
    assert np.array_equal(diff, region)
    assert diff.any()


def test_loading_frame_globally_distinct() -> None:
    """加载画面：与同帧号普通帧整帧差异显著，且整体更暗。"""
    renderer = _make_renderer()
    loading = renderer.render(SceneState(loading_active=True, loading_progress=0.6, frame_index=3))
    normal = renderer.render(SceneState(frame_index=3))
    diff = _diff_mask(loading, normal)
    assert float(np.mean(diff)) > 0.9  # 几乎全部像素都不同
    mean_abs = float(np.abs(loading.astype(int) - normal.astype(int)).mean())
    assert mean_abs > 12.0
    assert float(loading.mean()) < float(normal.mean())


@pytest.mark.parametrize("progress", [0.25, 0.75])
def test_loading_progress_bar_matches_state(progress: float) -> None:
    """加载进度条：量出的填充宽度比例与 loading_progress 偏差 <= 0.02。"""
    renderer = _make_renderer()
    frame = renderer.render(SceneState(loading_active=True, loading_progress=progress))
    measured = _fill_ratio_on_middle_row(
        renderer, frame, renderer.progress_bar_inner_rect(), COLOR_PROGRESS_FILL
    )
    assert abs(measured - progress) <= 0.02


def test_frame_index_distinguishes_frames() -> None:
    """帧号：不同 frame_index 的帧在标题/帧号区域不同，其余逐字节相同。"""
    renderer = _make_renderer()
    frame0 = renderer.render(SceneState(frame_index=0))
    frame7 = renderer.render(SceneState(frame_index=7))
    diff = _diff_mask(frame0, frame7)
    header = _region_mask(frame0.shape, [renderer.header_rect()])
    assert diff.any()
    assert np.all(diff <= header)  # 差异只出现在标题栏（帧号读数所在区域）
    assert np.array_equal(frame0[~header], frame7[~header])


# ---------------------------------------------------------------- 分辨率


def test_supported_resolutions_shape_and_determinism() -> None:
    """三档分辨率：同分辨率内确定、输出 shape=(H, W, 3)、dtype uint8。"""
    for resolution in SUPPORTED_RESOLUTIONS:
        a = run_scenario("happy_path", 3, resolution, 12.0, 1.0)
        b = run_scenario("happy_path", 3, resolution, 12.0, 1.0)
        assert a.frame_hashes == b.frame_hashes
        assert len(a.states) == 12
        frame = Renderer(SceneConfig(seed=3, resolution=resolution)).render(a.states[0])
        assert frame.shape == (resolution[1], resolution[0], 3)
        assert frame.dtype == np.uint8


# ---------------------------------------------------------------- Oracle


def test_event_and_frame_timestamps_monotonic() -> None:
    """Oracle：事件与帧的 t_monotonic 均单调不减，帧时间从 0 开始。"""
    for name in ("happy_path", "loading_timeout", "popup_random"):
        run = run_scenario(name, 5, (1280, 720), 20.0, 3.0)
        event_ts = [event.t_monotonic for event in run.trace.events]
        assert event_ts == sorted(event_ts)
        frame_ts = [record.t_monotonic for record in run.trace.frames]
        assert frame_ts == sorted(frame_ts)
        assert frame_ts[0] == 0.0
        assert len(frame_ts) == run.frame_count


def test_loading_events_paired_in_happy_path_only() -> None:
    """loading_started/loading_ended 在 happy_path 成对出现；超时场景只有 started。"""
    run = run_scenario("happy_path", 9, (1280, 720), 20.0, 3.0)
    starts = [e.t_monotonic for e in run.trace.events if e.name == "loading_started"]
    ends = [e.t_monotonic for e in run.trace.events if e.name == "loading_ended"]
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0] <= ends[0]

    timeout = run_scenario("loading_timeout", 9, (1280, 720), 20.0, 3.0)
    names = [e.name for e in timeout.trace.events]
    assert names.count("loading_started") >= 1
    assert names.count("loading_ended") == 0  # 加载持续到场景结束


def test_record_input_preserves_call_order() -> None:
    """record_input：记录顺序、动作与参数和调用完全一致（仅记录不执行）。"""
    trace = OracleTrace()
    trace.record_input(0.0, "key_down", key="a")
    trace.record_input(0.5, "click", button="left", x=12, y=34)
    trace.record_input(1.0, "key_up", key="a")
    assert [item.action for item in trace.inputs] == ["key_down", "click", "key_up"]
    assert [item.t_monotonic for item in trace.inputs] == [0.0, 0.5, 1.0]
    assert trace.inputs[1].params == {"button": "left", "x": 12, "y": 34}
    assert trace.inputs[0].params == {"key": "a"}


def test_to_json_roundtrip() -> None:
    """to_json：可 json.loads 解析，from_json 完整还原事件/帧/输入。"""
    run = run_scenario("happy_path", 11, (1280, 720), 20.0, 2.0)
    text = run.trace.to_json()
    data = json.loads(text)  # 必须是合法 JSON
    assert data["meta"]["scenario"] == "happy_path"
    assert data["meta"]["seed"] == 11
    restored = OracleTrace.from_json(text)
    assert restored.events == run.trace.events
    assert restored.frames == run.trace.frames

    manual = OracleTrace()
    manual.record_input(0.25, "click", button="right", x=1, y=2)
    restored_manual = OracleTrace.from_json(manual.to_json())
    assert restored_manual.inputs == manual.inputs


# ---------------------------------------------------------------- 场景


def test_builtin_scenarios_registered() -> None:
    """内置场景注册：至少包含三个规定场景，list_scenarios 可枚举。"""
    assert {"happy_path", "loading_timeout", "popup_random"} <= set(list_scenarios())


def test_unknown_scenario_raises_with_available_names() -> None:
    """未知场景：抛 KeyError 且错误信息列出可用场景。"""
    with pytest.raises(KeyError) as excinfo:
        run_scenario("no_such_scene", 1, (1280, 720), 10.0, 1.0)
    assert "happy_path" in str(excinfo.value)


def test_popup_random_bounded_and_deterministic() -> None:
    """popup_random：弹窗/目标时刻有界且有序，同种子两次运行哈希一致。"""
    a = run_scenario("popup_random", 5, (1280, 720), 20.0, 4.0)
    b = run_scenario("popup_random", 5, (1280, 720), 20.0, 4.0)
    assert a.frame_hashes == b.frame_hashes

    opened = [e.t_monotonic for e in a.trace.events if e.name == "popup_opened"]
    closed = [e.t_monotonic for e in a.trace.events if e.name == "popup_closed"]
    assert len(opened) == 1 and len(closed) == 1
    assert 0.0 <= opened[0] < closed[0] <= 4.0  # 有界随机时刻
    appeared = [e.t_monotonic for e in a.trace.events if e.name == "target_appeared"]
    assert len(appeared) == 1 and 0.0 <= appeared[0] < 4.0


def test_ui_scale_keeps_determinism() -> None:
    """ui_scale 参与渲染但同样确定：同种子同参数两遍哈希一致。"""
    a = run_scenario("happy_path", 8, (1280, 720), 10.0, 1.0, ui_scale=1.5)
    b = run_scenario("happy_path", 8, (1280, 720), 10.0, 1.0, ui_scale=1.5)
    assert a.frame_hashes == b.frame_hashes


def test_invalid_arguments_rejected() -> None:
    """非法入参：未知场景 KeyError、非正 fps/duration、越界 ratio 报错。"""
    with pytest.raises(ValueError):
        run_scenario("happy_path", 1, (1280, 720), 0.0, 1.0)
    with pytest.raises(ValueError):
        run_scenario("happy_path", 1, (1280, 720), 30.0, -1.0)
    with pytest.raises(ValueError):
        SceneState(health_ratio=1.5)
    with pytest.raises(ValueError):
        SceneConfig(seed=1, resolution=(0, 720))
