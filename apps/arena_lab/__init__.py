"""arena_lab——本地可控测试场（LAB-001/002/004 + LAB-007 M0 子集）。

一个不依赖真实游戏、可确定性复现视觉场景的本地模拟器：

- render：确定性渲染器（同种子同状态同参数 => 逐字节相同的帧）；
- scenario：内置场景脚本与种子化随机（happy_path / loading_timeout / popup_random）；
- oracle：真值轨迹（逐帧状态 + 离散事件）与注入输入记录；
- selfcheck：``python -m arena_lab.selfcheck`` 确定性自检 CLI；
- view：tkinter 逐帧回放窗口（体验用，不进验收）。

安全边界：ArenaLab 只是模拟器，不包含任何真实键鼠输入调用，仅"记录"
它收到的注入输入（LAB-007 Oracle 的 M0 子集）。
"""

from __future__ import annotations

from arena_lab.oracle import (
    EVENT_LOADING_ENDED,
    EVENT_LOADING_STARTED,
    EVENT_LOOT_APPEARED,
    EVENT_LOOT_DISAPPEARED,
    EVENT_POPUP_CLOSED,
    EVENT_POPUP_OPENED,
    EVENT_SCENARIO_ENDED,
    EVENT_SCENARIO_STARTED,
    EVENT_TARGET_APPEARED,
    EVENT_TARGET_DISAPPEARED,
    FrameRecord,
    InputRecord,
    OracleEvent,
    OracleTrace,
)
from arena_lab.render import (
    SUPPORTED_RESOLUTIONS,
    Renderer,
    SceneConfig,
    SceneState,
    health_color,
)
from arena_lab.scenario import (
    BUILTIN_SCENARIOS,
    ScenarioPlan,
    ScenarioRun,
    list_scenarios,
    run_scenario,
)

__all__ = [
    "SUPPORTED_RESOLUTIONS",
    "BUILTIN_SCENARIOS",
    "FrameRecord",
    "InputRecord",
    "OracleEvent",
    "OracleTrace",
    "Renderer",
    "ScenarioPlan",
    "ScenarioRun",
    "SceneConfig",
    "SceneState",
    "EVENT_LOADING_ENDED",
    "EVENT_LOADING_STARTED",
    "EVENT_LOOT_APPEARED",
    "EVENT_LOOT_DISAPPEARED",
    "EVENT_POPUP_CLOSED",
    "EVENT_POPUP_OPENED",
    "EVENT_SCENARIO_ENDED",
    "EVENT_SCENARIO_STARTED",
    "EVENT_TARGET_APPEARED",
    "EVENT_TARGET_DISAPPEARED",
    "health_color",
    "list_scenarios",
    "run_scenario",
]
