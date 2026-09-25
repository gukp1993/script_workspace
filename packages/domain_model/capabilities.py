"""脚本能力清单与授权模型（DOM-005）。

安全约定：**一切能力默认拒绝**。
- 脚本/项目只能声明 :data:`CAPABILITY_REGISTRY` 中已注册的能力；
- 授权采用显式白名单：``authorize(name, granted)`` 仅当能力已注册
  **且**出现在 granted 集合中才返回 True；
- 真实输入类能力（input.*）额外要求目标 ``real_input_allowed``，
  本模块提供 :func:`authorize_input` 封装该二次确认，调用方不得绕过。
"""

from __future__ import annotations

from typing import Iterable

from domain_model.models import TargetProfile

#: M0 已注册的能力清单（能力 ID：领域.动作）。后续里程碑只增不改语义。
CAPABILITY_REGISTRY: frozenset[str] = frozenset(
    {
        "perception.read",      # 读取感知快照
        "perception.record",    # 记录/标注感知数据
        "trace.write",          # 写入运行轨迹
        "machine.advance",      # 推进状态机（不含真实输入）
        "input.key",            # 真实键盘输入（受限）
        "input.mouse",          # 真实鼠标输入（受限）
        "input.wheel",          # 真实滚轮输入（受限）
    }
)

#: 需要目标 real_input_allowed 的受限能力
_RESTRICTED_INPUT_CAPABILITIES: frozenset[str] = frozenset({"input.key", "input.mouse", "input.wheel"})


def is_registered(capability: str) -> bool:
    """能力是否在注册清单中。"""
    return capability in CAPABILITY_REGISTRY


def authorize(capability: str, granted: Iterable[str] = ()) -> bool:
    """判定能力是否被授权：默认拒绝（未注册或未显式列入 granted 均拒绝）。"""
    if not is_registered(capability):
        return False
    return capability in frozenset(granted)


def authorize_input(capability: str, granted: Iterable[str], target: TargetProfile) -> bool:
    """受限输入能力的二次确认：能力授权 + 目标允许真实输入，二者缺一不可。"""
    if capability not in _RESTRICTED_INPUT_CAPABILITIES:
        return False
    if not authorize(capability, granted):
        return False
    return target.real_input_allowed
