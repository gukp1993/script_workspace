"""按键状态账本与幂等释放（INP-004 雏形）。

任何停止路径（急停 / cancel / stop / 崩溃清理）都必须能通过本账本
生成必要的 key_up 补偿序列，释放所有仍处于按下状态的键：
- 记录 key_down / key_up 配对，维护 pressed 集合；
- release_all(now) 生成补偿 key_up（幂等：重复调用无额外效果）；
- 补偿意图永不过期（ttl=None），保证停止路径不被 TTL 拦截。
"""

from __future__ import annotations

from typing import Any

from input_broker.intents import IntentKind, InputIntent, make_intent, normalize_key


class KeyLedger:
    """按键按下/释放配对账本。

    只依赖意图的 duck-typing 接口（kind / payload["key"]），
    不依赖具体实现包；M0 中由 InputBroker 在执行成功后喂入。
    """

    def __init__(self) -> None:
        self._pressed: dict[str, float] = {}  # key -> 最近一次按下的单调时刻

    @property
    def pressed(self) -> frozenset[str]:
        """当前仍被按下的键（归一化小写）。"""
        return frozenset(self._pressed)

    def is_pressed(self, key: str) -> bool:
        """查询某个键是否处于按下状态。"""
        normalized = normalize_key(key)
        return normalized is not None and normalized in self._pressed

    def observe(self, intent: Any) -> bool:
        """消费一个意图并更新配对状态；返回状态是否发生变化。

        - key_down：加入 pressed（重复按下无额外效果）；
        - key_up：移出 pressed（释放未按下键为无副作用 no-op）；
        - 其他 kind 一律忽略。
        """
        kind = getattr(intent, "kind", "")
        key = normalize_key(getattr(intent, "payload", {}).get("key"))
        if key is None:
            return False
        if kind == IntentKind.KEY_DOWN.value:
            if key in self._pressed:
                return False
            self._pressed[key] = float(getattr(intent, "created_monotonic", 0.0))
            return True
        if kind == IntentKind.KEY_UP.value:
            return self._pressed.pop(key, None) is not None
        return False

    def release_all(
        self,
        now: float,
        *,
        session_id: str = "",
        target_id: str = "",
        cause: str = "release_all",
    ) -> list[InputIntent]:
        """为所有仍按下的键生成 key_up 补偿意图（幂等）。

        - 返回的补偿意图按按键名排序，保证释放序列确定、可回放；
        - 补偿意图 ttl=None（永不过期），cause 标注来源停止路径；
        - 调用后 pressed 清空，重复调用返回空列表、无额外副作用。
        """
        keys = sorted(self._pressed)
        ups = [
            make_intent(
                session_id,
                target_id,
                IntentKind.KEY_UP.value,
                {"key": key},
                now=now,
                ttl_ms=None,
                cause=cause,
            )
            for key in keys
        ]
        self._pressed.clear()
        return ups
