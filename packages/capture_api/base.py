"""采集源协议与适配器级错误（CAP-001/002/003 共同底座）。

所有采集适配器（Dxcam/Mss/Fake）实现同一 ``CaptureSource`` 协议，
上层（环形缓冲、管线、采集链）只依赖该协议。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from capture_api.frames import Frame

__all__ = ["AdapterUnavailableError", "CaptureSource"]


class AdapterUnavailableError(RuntimeError):
    """适配器不可用（创建失败、驱动缺失、无显示器等）。

    与"单次抓帧失败"区分：前者在 ``start()`` 阶段抛出并携带结构化原因；
    后者表现为 ``grab()`` 返回 ``None``（暂无新帧）或抛出运行时异常
    （由采集链负责切换兜底）。
    """

    def __init__(self, reason: str, *, adapter: str = "unknown") -> None:
        super().__init__(f"[{adapter}] 适配器不可用: {reason}")
        self.reason = reason
        self.adapter = adapter


@runtime_checkable
class CaptureSource(Protocol):
    """采集源协议：生命周期 + 抓帧 + 诊断。

    约定：
    - ``grab()`` 返回 ``None`` 表示"暂无新帧"，**不是错误**（DXcam 常见）。
    - ``grab()`` 抛出异常表示设备级故障（丢失/失效），由上层决定降级。
    - ``latest()`` 只读最近一帧，绝不阻塞等待新帧。
    - ``diagnostics()`` 返回 JSON 可序列化 dict（CAP-008 诊断页消费）。
    """

    def start(self) -> None:
        """启动采集；资源不可用时抛 ``AdapterUnavailableError``。"""
        ...

    def stop(self) -> None:
        """停止采集并释放资源；必须幂等。"""
        ...

    def grab(self) -> Frame | None:
        """抓取一帧；暂无新帧返回 ``None``。"""
        ...

    def latest(self) -> Frame | None:
        """最近一帧（不驱动新采集），无则 ``None``。"""
        ...

    def diagnostics(self) -> dict[str, Any]:
        """诊断信息：帧数、错误、几何、色彩格式等。"""
        ...
