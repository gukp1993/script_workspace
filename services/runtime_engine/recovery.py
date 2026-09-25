"""会话收尾与恢复：异常安全的资源释放顺序（E07/TRC-002 配套）。

收尾顺序固定为 **轨迹 flush -> 采集 stop -> Broker stop**：

1. 先 flush 轨迹：即使后续清理失败，已发生的证据必须先落盘；
2. 再停采集：停止产生新帧，主循环自然失去输入；
3. 最后停 Broker：释放所有仍按下的键（补偿 key_up 不经策略评估）。

每一步都用 try/finally 串接：任一步抛出异常都不会阻断其余步骤，
异常在全部步骤完成后原样上抛。
"""

from __future__ import annotations

from runtime_engine.engine import SessionEngine

__all__ = ["close_and_finalize"]


def close_and_finalize(engine: SessionEngine) -> None:
    """收尾一个会话；可安全重复调用（各步骤均幂等）。

    典型用法（异常路径也保证收尾）::

        engine = build_session(...)
        try:
            engine.setup()
            engine.run()
        finally:
            close_and_finalize(engine)

    Raises:
        底层步骤的异常（flush/stop 失败等）——在全部收尾步骤执行完后上抛。
    """
    try:
        engine.trace.flush()
    finally:
        try:
            engine.capture.stop()
        finally:
            # stop() 幂等：释放按键、拒绝后续批次，并把引擎置为停止态。
            engine.stop()
