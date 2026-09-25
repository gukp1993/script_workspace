"""ArenaLab Windows E2E 冒烟（LAB-008 基础 + CAP/TGT 集成验证）。

完整流程见 :mod:`arena_lab.e2e_smoke`：子进程启动
``python -m arena_lab.view happy_path --hold``（固定标题/置顶/循环播放）
-> window_service 按标题+PID 找窗 -> MssAdapter 抓帧并按客户区 rect 裁剪
-> 断言血条特征色与尺寸一致 -> 回收子进程、断言无残留。

守卫约定：非 Windows、缺 pywin32（window_service）、缺 mss 或无显示器
（tkinter 初始化失败）一律 skip，不 fail；失败路径输出枚举到的窗口列表
作为诊断。
"""

from __future__ import annotations

import sys

import pytest

# ---------------------------------------------------------------- 守卫探测

IS_WINDOWS = sys.platform == "win32"


def _window_service_available() -> bool:
    """window_service（pywin32）是否可用。"""
    try:
        from window_service import WINDOW_SERVICE_AVAILABLE

        return bool(WINDOW_SERVICE_AVAILABLE)
    except Exception:  # noqa: BLE001 - 导入失败按不可用处理
        return False


def _mss_available() -> bool:
    """capture_api 的 mss 适配器是否可用。"""
    try:
        from capture_api import MSS_AVAILABLE

        return bool(MSS_AVAILABLE)
    except Exception:  # noqa: BLE001
        return False


def _display_available() -> bool:
    """有无可用显示器（能创建并销毁一个隐藏的 Tk root 即视为有）。"""
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001 - 无显示环境/缺 tkinter
        return False


pytestmark = [
    pytest.mark.skipif(not IS_WINDOWS, reason="仅在 Windows 上运行"),
    pytest.mark.skipif(
        not _window_service_available(), reason="window_service 不可用（缺 pywin32）"
    ),
    pytest.mark.skipif(not _mss_available(), reason="capture_api mss 不可用"),
    pytest.mark.skipif(not _display_available(), reason="无可用显示器（tkinter 初始化失败）"),
]

from arena_lab.e2e_smoke import RED_RATIO_MIN, SmokeResult, run_e2e_smoke  # noqa: E402


# ---------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def smoke() -> SmokeResult:
    """整模块只跑一次完整冒烟，三个用例共享同一份结果。"""
    result = run_e2e_smoke()
    if not result.window_found:
        pytest.fail(
            "E2E：未找到标题为 'ArenaLab - Training' 的窗口\n" + result.describe()
        )
    return result


# ---------------------------------------------------------------- 用例


def test_e2e_window_found_by_title_and_pid(smoke: SmokeResult) -> None:
    """驻留窗口按标题找到且属于冒烟子进程（TGT 集成）。"""
    assert smoke.window_found and smoke.pid_match, smoke.describe()


def test_e2e_capture_shows_health_red_and_matches_client_size(
    smoke: SmokeResult,
) -> None:
    """按客户区 rect 抓帧：帧内出现血条特征色，且尺寸与客户区一致（CAP 集成）。"""
    assert smoke.grabs > 0, smoke.describe()
    assert smoke.red_ratio_max >= RED_RATIO_MIN, (
        f"血条特征色像素占比峰值 {smoke.red_ratio_max:.5f} < {RED_RATIO_MIN}\n"
        + smoke.describe()
    )
    assert smoke.size_ok and smoke.captured_size is not None, (
        f"裁剪尺寸 {smoke.captured_size} 与客户区 {smoke.client_size} 不一致（±16px）\n"
        + smoke.describe()
    )


def test_e2e_process_exits_without_residue(smoke: SmokeResult) -> None:
    """结束冒烟后：子进程已退出且按标题再枚举无残留窗口。"""
    assert smoke.process_exited and smoke.exit_code is not None, smoke.describe()
    assert smoke.residual_windows == (), (
        "发现残留窗口：\n" + "\n".join(smoke.residual_windows)
    )
