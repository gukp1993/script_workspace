"""ArenaLab Windows E2E 冒烟（LAB-008 基础 + CAP/TGT 集成验证）。

流程（:func:`run_e2e_smoke`，供 CLI 与 pytest 复用）：

1. 前置守卫：window_service（pywin32）/ mss / tkinter 可用，否则直接
   FAIL 并说明原因（调用方也可据此 skip）；
2. 子进程启动 ``python -m arena_lab.view happy_path --hold``：窗口标题
   固定为 ``ArenaLab - Training``、置顶、位置/尺寸固定、循环播放；
3. ``window_service.list_windows()`` 按"标题 + 子进程 PID + 可见"找到
   窗口，取得 hwnd；
4. ``win32gui.GetClientRect`` + ``ClientToScreen`` 取窗口客户区矩形
   （只读查询，不做任何窗口/输入操作）；
5. ``capture_api.MssAdapter`` 抓取整屏帧后按窗口客户区 rect 裁剪
   （MssAdapter 是整屏采集器，窗口 rect 裁剪在帧上完成）；
6. 断言：血条低血红特征色像素占比峰值 > 阈值（happy_path 受击段血量
   0.35 的确定插值色，该色不受种子色调影响）；裁剪帧尺寸 == 客户区
   尺寸（±容差）；
7. 结束 terminate 子进程并断言：进程退出、按标题再枚举无残留窗口。

任一失败都会在结果的诊断行里输出枚举到的窗口列表，便于排查。

安全边界：全程只"看"（枚举/只读查询/抓屏），绝不注入真实键鼠输入；
子进程结束后由本模块负责回收，不残留进程。

用法（python -m 直跑需要注入包路径，见 README）::

    export PYTHONPATH="packages;services;apps"   # Git Bash 写法
    python -m arena_lab.e2e_smoke                # 打印 PASS/FAIL，exit 0/1
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from arena_lab.render import health_color
from arena_lab.view import E2E_WINDOW_TITLE

__all__ = [
    "SmokeResult",
    "run_e2e_smoke",
    "main",
    "E2E_WINDOW_TITLE",
    "RED_RATIO_MIN",
    "SIZE_TOLERANCE_PX",
]

# 血条特征色（happy_path 受击段 health_ratio=0.35 的确定性插值色）判定阈值：
# 960x540 窗口内约 0.24% 像素为该色，取 0.1% 留足余量。
RED_RATIO_MIN = 0.001
# 特征色逐通道匹配容差（抗截图/缩放噪声；种子色调不影响血条填充色）。
COLOR_TOLERANCE = 12
# 裁剪帧尺寸与客户区尺寸的一致性容差（像素；窗口边框/DPI 舍入）。
SIZE_TOLERANCE_PX = 16

# 轮询与等待参数。
_POLL_INTERVAL_S = 0.25
_WINDOW_WAIT_S = 12.0
# 抓帧采样间隔（约 8 Hz）与总时长下限：需覆盖 happy_path 受击红血窗口。
_GRAB_INTERVAL_S = 0.12
_MIN_GRAB_SECONDS = 12.0
# 诊断输出里最多列出的窗口行数。
_MAX_DIAG_WINDOWS = 25


@dataclass(frozen=True)
class SmokeResult:
    """一次 E2E 冒烟的完整结果（含逐项检查与诊断行）。"""

    ok: bool
    window_found: bool = False
    child_pid: int = 0
    pid_match: bool = False
    grabs: int = 0
    red_ratio_max: float = 0.0
    size_ok: bool = False
    client_size: tuple[int, int] | None = None
    captured_size: tuple[int, int] | None = None
    process_exited: bool = False
    exit_code: int | None = None
    residual_windows: tuple[str, ...] = field(default_factory=tuple)
    lines: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        """多行人类可读摘要（失败诊断 / pytest 断言消息复用）。"""
        return "\n".join(self.lines)


def _repo_root() -> str:
    """仓库根目录（apps/arena_lab/e2e_smoke.py 向上三级）。"""
    return str(Path(__file__).resolve().parents[2])


def _diagnose_windows() -> tuple[str, ...]:
    """枚举当前顶层窗口标题（诊断用；服务不可用时返回提示行）。"""
    try:
        from window_service import WINDOW_SERVICE_AVAILABLE, list_windows

        if not WINDOW_SERVICE_AVAILABLE:
            return ("window_service 不可用（缺 pywin32），无法枚举窗口",)
        return tuple(w.describe() for w in list_windows()[:_MAX_DIAG_WINDOWS])
    except Exception as exc:  # noqa: BLE001 - 诊断本身不允许把流程打断
        return (f"枚举窗口失败: {exc!r}",)


def _terminate(proc: subprocess.Popen[str]) -> tuple[bool, int | None]:
    """结束子进程并等待退出：返回 (是否正常退出, 退出码)。"""
    if proc.poll() is not None:
        return True, proc.returncode
    proc.terminate()
    try:
        return True, proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - 极端挂死兜底
        proc.kill()
        return False, proc.wait(timeout=5)


def run_e2e_smoke(
    *,
    duration_s: float = 6.0,
    resolution: tuple[int, int] = (960, 540),
    fps: float = 30.0,
    red_ratio_min: float = RED_RATIO_MIN,
    size_tolerance_px: int = SIZE_TOLERANCE_PX,
) -> SmokeResult:
    """执行完整 E2E 冒烟流程，返回结构化结果（不抛异常）。"""
    lines: list[str] = []

    # ---- [0] 前置守卫 -----------------------------------------------------
    reasons: list[str] = []
    if sys.platform != "win32":
        reasons.append(f"非 Windows 平台（{sys.platform}）")
    try:
        from window_service import WINDOW_SERVICE_AVAILABLE

        if not WINDOW_SERVICE_AVAILABLE:
            reasons.append("window_service 不可用（缺 pywin32）")
        window_service_ok = WINDOW_SERVICE_AVAILABLE
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"window_service 导入失败: {exc!r}")
        window_service_ok = False
    try:
        from capture_api import MSS_AVAILABLE

        if not MSS_AVAILABLE:
            reasons.append("capture_api.mss 不可用")
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"capture_api 导入失败: {exc!r}")
    try:
        import tkinter  # noqa: F401

        from PIL import Image, ImageTk  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"tkinter/Pillow 不可用: {exc!r}")
    if reasons:
        lines.append(f"[0] 前置守卫失败：{'；'.join(reasons)}")
        return SmokeResult(ok=False, lines=tuple(lines))
    lines.append("[0] 前置守卫通过（window_service / mss / tkinter 可用）")

    # ---- [1] 子进程启动驻留窗口 -------------------------------------------
    width, height = int(resolution[0]), int(resolution[1])
    cmd = [
        sys.executable,
        "-m",
        "arena_lab.view",
        "happy_path",
        "--hold",
        "--duration",
        repr(float(duration_s)),
        "--fps",
        repr(float(fps)),
        "--resolution",
        f"{width}x{height}",
    ]
    env = dict(os.environ)
    injected = os.pathsep.join(["packages", "services", "apps"])
    env["PYTHONPATH"] = f"{injected}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else injected
    proc = subprocess.Popen(  # noqa: S603 - 命令为固定参数列表的本地解释器
        cmd,
        cwd=_repo_root(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines.append(f"[1] 子进程已启动 pid={proc.pid}: {' '.join(cmd)}")

    def _finish(
        result: SmokeResult, proc_ref: subprocess.Popen[str] | None = proc
    ) -> SmokeResult:
        """失败路径兜底：确保子进程被回收，并附窗口诊断。"""
        exited, code = (True, None)
        if proc_ref is not None and proc_ref.poll() is None:
            exited, code = _terminate(proc_ref)
        diag = (f"子进程退出码={code}" if exited else "子进程未被终止（kill 后仍存活）",)
        return SmokeResult(
            ok=False,
            window_found=result.window_found,
            child_pid=result.child_pid,
            pid_match=result.pid_match,
            grabs=result.grabs,
            red_ratio_max=result.red_ratio_max,
            size_ok=result.size_ok,
            client_size=result.client_size,
            captured_size=result.captured_size,
            process_exited=exited,
            exit_code=code,
            residual_windows=result.residual_windows,
            lines=tuple(result.lines) + diag + tuple(_diagnose_windows()),
        )

    # ---- [2] 按标题 + PID 找窗口 ------------------------------------------
    info = None
    deadline = time.monotonic() + _WINDOW_WAIT_S
    if window_service_ok:
        from window_service import list_windows

        while info is None and time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # 子进程提前退出（启动失败等）
            for candidate in list_windows():
                if (
                    candidate.title == E2E_WINDOW_TITLE
                    and candidate.visible
                    and candidate.pid == proc.pid
                ):
                    info = candidate
                    break
            if info is None:
                time.sleep(_POLL_INTERVAL_S)
    if info is None:
        stderr_text = ""
        if proc.poll() is not None:
            try:
                stderr_text = (proc.stderr.read() or "")[-800:] if proc.stderr else ""
            except Exception:  # noqa: BLE001
                stderr_text = "<读取失败>"
        lines.append(
            f"[2] 未找到标题为 {E2E_WINDOW_TITLE!r} 的窗口"
            + (f"；子进程已退出，stderr 末尾：{stderr_text}" if stderr_text else "")
        )
        return _finish(SmokeResult(ok=False, child_pid=proc.pid, lines=tuple(lines)))
    lines.append(
        f"[2] 窗口已找到 hwnd=0x{info.hwnd:08X} pid={info.pid} title={info.title!r}"
    )

    # ---- [3] 客户区矩形（只读查询，走 window_service 统一入口） -------------
    from window_service.enumerate_win import client_rect_screen

    rect = client_rect_screen(info.hwnd)
    if rect is None:
        return _finish(SmokeResult(ok=False, child_pid=proc.pid, lines=tuple(lines + ["[3] 客户区查询失败"])))
    screen_x, screen_y, right, bottom = rect
    client_w, client_h = right - screen_x, bottom - screen_y
    lines.append(f"[3] 客户区 {client_w}x{client_h} @ screen ({screen_x},{screen_y})")

    # ---- [4] MssAdapter 抓帧 + 客户区裁剪 + 血条特征色检测 -------------------
    from capture_api import MssAdapter

    expected_red = np.array(health_color(0.35), dtype=np.int16)  # 受击段低血红
    adapter = MssAdapter(monitor_index=0)
    adapter.start()
    grabs = 0
    red_ratio_max = 0.0
    captured_size: tuple[int, int] | None = None
    size_ok = False
    grab_deadline = time.monotonic() + max(_MIN_GRAB_SECONDS, duration_s + 4.0)
    try:
        while time.monotonic() < grab_deadline:
            if proc.poll() is not None:
                lines.append("[4] 子进程在抓帧期间提前退出")
                break
            frame = adapter.grab()
            if frame is None:
                time.sleep(_GRAB_INTERVAL_S)
                continue
            grabs += 1
            mono_l, mono_t = frame.meta.client_rect[0], frame.meta.client_rect[1]
            rx = int(screen_x) - int(mono_l)
            ry = int(screen_y) - int(mono_t)
            fh, fw = frame.pixels.shape[:2]
            x0, y0 = max(0, rx), max(0, ry)
            x1, y1 = min(fw, rx + client_w), min(fh, ry + client_h)
            if x1 > x0 and y1 > y0:
                crop = frame.pixels[y0:y1, x0:x1]
                captured_size = (int(crop.shape[1]), int(crop.shape[0]))
                size_ok = (
                    abs(captured_size[0] - client_w) <= size_tolerance_px
                    and abs(captured_size[1] - client_h) <= size_tolerance_px
                )
                diff = np.abs(crop.astype(np.int16) - expected_red)
                matched = int(np.count_nonzero(np.all(diff <= COLOR_TOLERANCE, axis=-1)))
                red_ratio_max = max(red_ratio_max, matched / float(crop.shape[0] * crop.shape[1]))
            # 早退：已确认特征色与尺寸后无需继续采样。
            if grabs >= 10 and size_ok and red_ratio_max >= red_ratio_min:
                break
            time.sleep(_GRAB_INTERVAL_S)
    finally:
        adapter.stop()
    lines.append(
        f"[4] 抓帧 {grabs} 次，血条特征色占比峰值={red_ratio_max:.5f}"
        f"（阈值 {red_ratio_min}），裁剪尺寸={captured_size} vs 客户区={client_w}x{client_h}"
    )
    if grabs == 0:
        lines.append("[4] 未获取到任何帧 -> FAIL")
        return _finish(
            SmokeResult(
                ok=False,
                window_found=True,
                child_pid=proc.pid,
                pid_match=True,
                client_size=(client_w, client_h),
                captured_size=captured_size,
                size_ok=size_ok,
                red_ratio_max=red_ratio_max,
                lines=tuple(lines),
            )
        )

    # ---- [5] 结束子进程并检查残留 ------------------------------------------
    process_exited, exit_code = _terminate(proc)
    time.sleep(0.4)  # 等 WM 销毁消息走完再复查窗口
    from window_service import list_windows as _list_windows

    residual = tuple(
        w.describe() for w in _list_windows() if w.title == E2E_WINDOW_TITLE
    )
    lines.append(
        f"[5] 子进程退出码={exit_code}（正常退出={process_exited}），"
        f"残留窗口={len(residual)} 个"
    )

    ok = (
        red_ratio_max >= red_ratio_min
        and size_ok
        and process_exited
        and exit_code is not None
        and not residual
    )
    lines.append(
        "[6] 检查结论："
        + "血条特征色=" + ("OK" if red_ratio_max >= red_ratio_min else "FAIL")
        + "，尺寸一致=" + ("OK" if size_ok else "FAIL")
        + "，进程退出=" + ("OK" if process_exited and exit_code is not None else "FAIL")
        + "，无残留=" + ("OK" if not residual else "FAIL")
    )
    return SmokeResult(
        ok=ok,
        window_found=True,
        child_pid=proc.pid,
        pid_match=True,
        grabs=grabs,
        red_ratio_max=red_ratio_max,
        size_ok=size_ok,
        client_size=(client_w, client_h),
        captured_size=captured_size,
        process_exited=process_exited,
        exit_code=exit_code,
        residual_windows=residual,
        lines=tuple(lines) + (tuple(_diagnose_windows()) if not ok else ()),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：同流程，打印 PASS/FAIL，退出码 0/1。"""
    parser = argparse.ArgumentParser(
        prog="arena_lab.e2e_smoke", description="ArenaLab Windows E2E 冒烟（LAB-008）"
    )
    parser.add_argument("--duration", type=float, default=6.0, help="场景时长（秒）")
    parser.add_argument(
        "--resolution", type=lambda s: tuple(int(x) for x in s.lower().split("x", 1)),
        default=(960, 540), help="分辨率 宽x高",
    )
    parser.add_argument("--fps", type=float, default=30.0, help="回放帧率")
    args = parser.parse_args(argv)

    result = run_e2e_smoke(
        duration_s=args.duration, resolution=args.resolution, fps=args.fps
    )
    for line in result.lines:
        print(line)
    if result.ok:
        print("E2E SMOKE PASS")
        return 0
    print("E2E SMOKE FAIL: 见上方诊断行（含窗口枚举）", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
