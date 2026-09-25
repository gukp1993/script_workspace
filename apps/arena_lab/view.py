"""tkinter 逐帧回放窗口（体验用，不进验收）。

用法（python -m 直跑需要注入包路径，见 README）::

    export PYTHONPATH="packages;services;apps"   # Git Bash 写法
    python -m arena_lab.view happy_path --seed 20260925 --duration 10

说明：

- 展示路径与验收完全一致：同一 run_scenario 状态序列 + 同一 Renderer
  逐帧渲染（经 iter_frames 惰性产出，不预载全部帧，内存占用小）；
- 窗口展示本身不要求确定性（验收只看 numpy 帧）；
- 不使用 ctypes，不注入任何真实键鼠输入。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from arena_lab.scenario import list_scenarios, run_scenario


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
    root.title(f"ArenaLab - {run.name} (seed={run.seed}, {run.frame_count} frames)")
    label = tk.Label(root, bg="black")
    label.pack(fill="both", expand=True)
    holder: dict[str, object] = {"photo": None}
    delay_ms = max(1, int(1000 / max(args.fps, 1e-6)))

    def step() -> None:
        """惰性取下一帧并显示；播完后停留片刻自动关闭。"""
        try:
            frame = next(frames)
        except StopIteration:
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
