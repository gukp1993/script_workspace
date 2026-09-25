"""arena_lab——本地可控测试场（LAB-001/002/004 + LAB-005/006/007/008）。

一个不依赖真实游戏、可确定性复现视觉场景的本地模拟器：

- render：确定性渲染器（同种子同状态同参数 => 逐字节相同的帧；
  支持可选遮挡叠加，LAB-006 渲染入口）；
- scenario：内置场景脚本与种子化随机（happy_path / loading_timeout /
  popup_random），``run_scenario(..., faults=FaultPlan(...))`` 支持故障注入；
- faults：故障注入计划（遮挡 / 掉帧 / 输入延迟 / 失焦，LAB-006）；
- oracle：真值轨迹（逐帧状态 + 离散事件）与注入输入记录；
  支持 save/load 文件往返与 export_summary 摘要（LAB-007）；
- selfcheck：``python -m arena_lab.selfcheck`` 确定性自检 CLI；
- view：tkinter 逐帧回放窗口（体验用，不进验收；``--hold`` 驻留模式
  供 E2E 冒烟使用）；
- e2e_smoke：``python -m arena_lab.e2e_smoke`` Windows E2E 冒烟
  （LAB-008：view --hold + window_service 找窗 + MssAdapter 抓帧断言）。

安全边界：ArenaLab 只是模拟器，不包含任何真实键鼠输入调用，仅"记录"
它收到的注入输入（LAB-007 Oracle）；e2e_smoke 全程只做只读窗口查询与
屏幕抓帧，不注入任何输入，并负责回收自己启动的子进程。
"""

from __future__ import annotations

from arena_lab.faults import (
    EVENT_FPS_DROP_ENDED,
    EVENT_FPS_DROP_STARTED,
    EVENT_FOCUS_LOST,
    EVENT_FOCUS_RESTORED,
    EVENT_OCCLUSION_ENDED,
    EVENT_OCCLUSION_STARTED,
    FaultPlan,
    apply,
    check_fault_plan,
    default_occlusion_rect,
    fault_events,
    occlusion_rects_at,
    record_input,
)
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
    COLOR_OCCLUSION,
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
    "COLOR_OCCLUSION",
    "FrameRecord",
    "InputRecord",
    "OracleEvent",
    "OracleTrace",
    "Renderer",
    "ScenarioPlan",
    "ScenarioRun",
    "SceneConfig",
    "SceneState",
    "EVENT_FPS_DROP_ENDED",
    "EVENT_FPS_DROP_STARTED",
    "EVENT_FOCUS_LOST",
    "EVENT_FOCUS_RESTORED",
    "EVENT_LOADING_ENDED",
    "EVENT_LOADING_STARTED",
    "EVENT_LOOT_APPEARED",
    "EVENT_LOOT_DISAPPEARED",
    "EVENT_OCCLUSION_ENDED",
    "EVENT_OCCLUSION_STARTED",
    "EVENT_POPUP_CLOSED",
    "EVENT_POPUP_OPENED",
    "EVENT_SCENARIO_ENDED",
    "EVENT_SCENARIO_STARTED",
    "EVENT_TARGET_APPEARED",
    "EVENT_TARGET_DISAPPEARED",
    "FaultPlan",
    "apply",
    "check_fault_plan",
    "default_occlusion_rect",
    "fault_events",
    "health_color",
    "list_scenarios",
    "occlusion_rects_at",
    "record_input",
    "run_scenario",
]
