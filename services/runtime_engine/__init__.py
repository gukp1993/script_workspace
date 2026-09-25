"""runtime_engine——运行时引擎编排服务（E07/E10，M2）。

把一次会话编排成 感知 -> 状态机 -> 意图 -> 策略 -> 输入代理 -> 轨迹
的逐帧闭环：

- :mod:`runtime_engine.engine`：:class:`SessionEngine` 主循环、
  :class:`FrameStore` 帧留痕、:class:`TickOutcome` / :class:`RunSummary`；
- :mod:`runtime_engine.factory`：:func:`build_session` 从项目目录一条龙装配；
- :mod:`runtime_engine.recovery`：:func:`close_and_finalize` 异常安全的收尾。

安全边界：本服务不调用任何系统输入 API；真实输入只能经
``services/input_broker`` 的模式闸与前台复核后发生。
"""

from runtime_engine.engine import (
    FRAME_SEQ_NONE,
    FrameStore,
    RunSummary,
    SessionEngine,
    TickOutcome,
)
from runtime_engine.factory import build_session
from runtime_engine.recovery import close_and_finalize

__all__ = [
    "FRAME_SEQ_NONE",
    "FrameStore",
    "RunSummary",
    "SessionEngine",
    "TickOutcome",
    "build_session",
    "close_and_finalize",
]
