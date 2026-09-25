"""ArenaLab 确定性自检 CLI（LAB-001 / LAB-004 验收入口）。

用法（python -m 直跑需要注入包路径，见 README）::

    export PYTHONPATH="packages;services;apps"   # Git Bash 写法
    python -m arena_lab.selfcheck [--frames 300]

检查内容：

1. 同种子跑 happy_path 两遍，逐帧 sha256 对比 -> 全部一致；
2. 换种子跑 -> 与基线存在帧差异；
3. 三档支持分辨率各跑 20 帧 -> 同分辨率内确定、渲染尺寸 (H, W, 3) 正确。

全部通过打印 ``SELFCHECK PASS: <n> frames verified`` 并退出码 0；
任一失败打印 ``SELFCHECK FAIL: ...`` 并退出码 1。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import numpy as np

from arena_lab.render import SUPPORTED_RESOLUTIONS, Renderer, SceneConfig, SceneState
from arena_lab.scenario import run_scenario

__all__ = ["run_selfcheck", "main"]

# 自检基线种子（固定值，保证跨机器跨时间输出一致）。
_BASE_SEED = 20260925
_FPS = 30.0
_SHORT_RUN_FRAMES = 20


def _frame_hashes(name: str, seed: int, resolution: tuple[int, int], frames: int) -> tuple[str, ...]:
    """跑一个场景并返回逐帧 sha256（时长由帧数与 fps 反推）。"""
    duration = frames / _FPS
    return run_scenario(name, seed, resolution, _FPS, duration).frame_hashes


def run_selfcheck(frame_count: int = 300) -> tuple[bool, list[str], int]:
    """执行三项确定性检查，返回 (是否全部通过, 日志行, 校验帧数)。"""
    lines: list[str] = []
    verified = 0
    if frame_count < 1:
        return False, [f"invalid --frames {frame_count}: must be >= 1"], 0

    # [1] 同种子两遍：逐帧哈希完全一致。
    hashes_a = _frame_hashes("happy_path", _BASE_SEED, (1280, 720), frame_count)
    hashes_b = _frame_hashes("happy_path", _BASE_SEED, (1280, 720), frame_count)
    verified += 2 * frame_count
    same_seed_ok = hashes_a == hashes_b
    lines.append(
        f"[1] same-seed happy_path x2, {frame_count} frames: "
        + ("identical" if same_seed_ok else "MISMATCH")
    )
    if not same_seed_ok:
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(hashes_a, hashes_b)) if a != b), -1
        )
        lines.append(f"    first differing frame index: {first_diff}")

    # [2] 换种子：与基线存在帧差异（渲染含种子色调，差异应是全帧级的）。
    hashes_c = _frame_hashes("happy_path", _BASE_SEED + 1, (1280, 720), frame_count)
    verified += frame_count
    diff_count = sum(1 for a, b in zip(hashes_a, hashes_c) if a != b)
    diff_seed_ok = diff_count >= 1
    lines.append(
        f"[2] other-seed happy_path: differing frames {diff_count}/{frame_count}"
        + ("" if diff_seed_ok else "  -> FAIL (expected at least 1)")
    )

    # [3] 三档分辨率：同分辨率内确定，渲染尺寸正确。
    resolutions_ok = True
    for resolution in SUPPORTED_RESOLUTIONS:
        run_a = _frame_hashes("happy_path", _BASE_SEED, resolution, _SHORT_RUN_FRAMES)
        run_b = _frame_hashes("happy_path", _BASE_SEED, resolution, _SHORT_RUN_FRAMES)
        verified += 2 * _SHORT_RUN_FRAMES
        deterministic = run_a == run_b
        probe = Renderer(SceneConfig(seed=_BASE_SEED, resolution=resolution)).render(SceneState())
        shape_ok = probe.shape == (resolution[1], resolution[0], 3) and probe.dtype == np.uint8
        resolutions_ok = resolutions_ok and deterministic and shape_ok
        lines.append(
            f"[3] {resolution[0]}x{resolution[1]}: deterministic={deterministic} "
            f"shape={probe.shape} dtype={probe.dtype}"
        )

    return (same_seed_ok and diff_seed_ok and resolutions_ok), lines, verified


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arena_lab.selfcheck", description="ArenaLab 确定性自检（LAB-001/004）"
    )
    parser.add_argument(
        "--frames", type=int, default=300, help="步骤 1/2 的帧数（默认 300，步骤 3 固定 20 帧）"
    )
    args = parser.parse_args(argv)

    ok, lines, verified = run_selfcheck(args.frames)
    for line in lines:
        print(line)
    if ok:
        print(f"SELFCHECK PASS: {verified} frames verified")
        return 0
    print("SELFCHECK FAIL: determinism or resolution check failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
