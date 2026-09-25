"""tkinter 逐帧回放窗口（体验用，不进验收）。

用法（python -m 直跑需要注入包路径，见 README）::

    export PYTHONPATH="packages;services;apps"   # Git Bash 写法
    python -m arena_lab.view happy_path --seed 20260925 --duration 10

说明：

- 展示路径与验收完全一致：同一 run_scenario 状态序列 + 同一 Renderer
  逐帧渲染（经 iter_frames 惰性产出，不预载全部帧，内存占用小）；
- 窗口展示本身不要求确定性（验收只看 numpy 帧）；
- 不使用 ctypes，不注入任何真实键鼠输入。

``--hold`` 驻留模式（LAB-008 E2E 冒烟用）：窗口标题固定为
``ArenaLab - Training``、置顶、位置/尺寸固定，帧序列循环播放，
直到进程被外部结束——供 e2e_smoke 按标题找到窗口并抓帧断言。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from arena_lab.scenario import list_scenarios, run_scenario

# --hold 模式的固定窗口标题（e2e_smoke 按此查找窗口）。
E2E_WINDOW_TITLE = "ArenaLab - Training"


def _parse_resolution(text: str) -> tuple[int, int]:
    """解析 '宽x高' 形式的分辨率字符串（如 1280x720）。"""
    try:
        width_text, height_text = text.lower().split("x", maxsplit=1)
        resolution = (int(width_text), int(height_text))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"分辨率格式应为 宽x高（如 1280x720），收到 {text!r}"
        ) from exc
    if resolution[0] <= 0 or resolution[1] <= 0:
        raise argparse.ArgumentTypeError(f"分辨率必须为正宽高，收到 {text!r}")
    return resolution


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arena_lab.view", description="ArenaLab 场景回放窗口（体验用）"
    )
    parser.add_argument("scenario", nargs="?", default="happy_path", help="场景名")
    parser.add_argument("--seed", type=int, default=20260925, help="随机种子")
    parser.add_argument("--fps", type=float, default=30.0, help="回放帧率")
    parser.add_argument("--duration", type=float, default=10.0, help="场景时长（秒）")
    parser.add_argument("--resolution", type=_parse_resolution, default=(1280, 720), help="分辨率 宽x高")
    parser.add_argument("--ui-scale", type=float, default=1.0, help="UI 缩放")
    parser.add_argument(
        "--hold",
        action="store_true",
        help="驻留模式：固定标题/位置、置顶、循环播放（LAB-008 E2E 冒烟用）",
    )
    args = parser.parse_args(argv)
    if args.scenario not in list_scenarios():
        parser.error(f"未知场景 {args.scenario!r}；可用场景：{', '.join(list_scenarios())}")

    # 延迟导入：让 --help 等不依赖 tkinter/Pillow 也能工作。
    try:
        import tkinter as tk

        from PIL import Image, ImageTk
    except Exception as exc:  # pragma: no cover - 无显示环境/缺依赖时才触发
        print(f"无法启动回放窗口（缺少 tkinter 或 Pillow）：{exc}")
        return 1

    run = run_scenario(
        args.scenario,
        args.seed,
        args.resolution,
        args.fps,
        args.duration,
        ui_scale=args.ui_scale,
    )
    frames = run.iter_frames()

    root = tk.Tk()
    if args.hold:
        # 驻留模式：标题固定（供按标题查找）、置顶 + 固定几何，
        # 减小被其他窗口遮挡/位置漂移导致的抓帧污染。
        root.title(E2E_WINDOW_TITLE)
        width, height = run.config.resolution
        root.geometry(f"{width}x{height}+40+40")
        root.resizable(False, False)
        root.attributes("-topmost", True)
    else:
        root.title(f"ArenaLab - {run.name} (seed={run.seed}, {run.frame_count} frames)")
    label = tk.Label(root, bg="black")
    label.pack(fill="both", expand=True)
    holder: dict[str, object] = {"photo": None}
    delay_ms = max(1, int(1000 / max(args.fps, 1e-6)))

    def step() -> None:
        """惰性取下一帧并显示；--hold 播完后循环重放，否则停留后关闭。"""
        nonlocal frames
        try:
            frame = next(frames)
        except StopIteration:
            if args.hold:
                frames = run.iter_frames()  # 循环播放，直到进程被外部结束
                root.after(delay_ms, step)
                return
            root.after(1500, root.destroy)
            return
        photo = ImageTk.PhotoImage(Image.fromarray(frame))
        holder["photo"] = photo  # 防止 PhotoImage 被垃圾回收
        label.configure(image=photo)
        root.after(delay_ms, step)

    root.after(0, step)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
