"""全局急停控制器（INP-006 / SAFE-008 / SAFE-018）。

两步语义（ADR-0004 决策 4）：
- 第一步（立即）：cancel InputBroker —— 拒绝一切新批次并 release_all
  释放所有仍按下的键；输入安全优先于一切，绝不等待策略评估；
- 第二步（清理）：结束会话占用 + 审计入链（estop + keys_released 事件）。

保证：
- 幂等（SAFE-018）：重复 trigger 只执行一次，返回 False；
- 与 UI 完全解耦：来源（热键/看门狗/失焦/操作员按钮）只是字符串，
  控制器不依赖任何 UI 组件，UI 卡死不影响急停；
- 记录 source/reason/单调时间戳，全部进入审计轨迹。
"""

from __future__ import annotations

from dataclasses import dataclass

from common.clock import Clock, MonotonicClock

from input_broker.audit import AuditLogger
from input_broker.broker import InputBroker


@dataclass(frozen=True)
class EstopRecord:
    """一次急停触发的审计记录（控制器内存视图，落盘在轨迹链里）。"""

    source: str
    reason: str
    ts_monotonic: float
    released_keys: tuple[str, ...]


class EstopController:
    """急停总入口：热键 / 看门狗 / 失焦 / UI 按钮统一调用 trigger(source)。

    broker 与 audit 均可缺省（分层组合）：只传 broker 时仅做停止与释放；
    只传 audit 时仅落审计；都传时执行完整两步。
    """

    def __init__(
        self,
        broker: InputBroker | None = None,
        audit: AuditLogger | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.broker = broker
        self.audit = audit
        self.clock: Clock = clock if clock is not None else MonotonicClock()
        self.records: list[EstopRecord] = []
        self._triggered = False

    # ------------------------------------------------------------------ 状态
    @property
    def triggered(self) -> bool:
        """急停是否已触发（闩存：触发后必须走人工恢复流程）。"""
        return self._triggered

    def trigger(self, source: str, reason: str = "") -> bool:
        """触发急停；返回 True 表示本次调用执行了急停，False 表示已触发过。

        执行顺序可预测（INP-009 取消屏障语义）：
        1. 立即 cancel broker（停止新批次 + release_all 已按下键）；
        2. stop broker（结束会话占用；幂等，双重保险）；
        3. 审计：estop 事件 + 按键释放结果。
        """
        if self._triggered:
            return False
        self._triggered = True
        ts = float(self.clock.now())

        # 第一步：立即停止输入并释放全部按键（安全优先，不经策略评估）。
        released = self.broker.cancel(f"estop:{source}") if self.broker is not None else []
        # 第二步：清理会话占用（幂等；stop 后任何提交路径都被拒绝）。
        if self.broker is not None:
            self.broker.stop()
        # 审计：先 estop 主事件，再按键释放结果（无卡键证据）。
        if self.audit is not None:
            session_id = self.broker.last_session_id if self.broker is not None else ""
            self.audit.log_estop(source, reason, released_count=len(released), session_id=session_id)
            if released:
                self.audit.log_release(released, cause=f"estop:{source}", session_id=session_id)

        self.records.append(
            EstopRecord(
                source=source,
                reason=reason,
                ts_monotonic=ts,
                released_keys=tuple(
                    sorted(str(i.payload.get("key", "")) for i in released)
                ),
            )
        )
        return True

    def reset(self) -> None:
        """人工恢复流程专用：清除触发闩存（新会话需重新绑定与确认）。"""
        self._triggered = False
