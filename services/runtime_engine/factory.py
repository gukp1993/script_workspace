"""会话装配工厂：项目目录 -> 可运行的 SessionEngine（E07 便捷入口）。

装配顺序（编译前置门先行，AC-P0-10）::

    load_project -> ensure_compilable（静态分析 error 即拒绝）
    -> compile_project_machine -> registry.create_detector
    -> PolicyEvaluator（默认拒绝）-> InputBroker(mode)
    -> JsonlTraceWriter（默认写入项目 traces/ 目录）
    -> SessionEngine

本模块只做装配，不做任何系统输入调用；真实输入能力由注入的
InputSink 与运行模式共同决定。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from capture_api.base import CaptureSource
from common.clock import Clock
from common.ids import new_id
from domain_model import DetectorType
from domain_model.models import Detector as DetectorConfig
from domain_model.parsing import ProjectBundle, load_project
from domain_model.static_analysis import ensure_compilable
from input_broker import InputBroker
from input_broker.fake_sink import InputSink
from policy_engine import (
    ForegroundContext,
    PolicyEvaluator,
    PolicyInput,
    RunMode,
    TargetRef,
    parse_mode,
)
from state_machine.compiler import compile_project_machine
from trace_format import JsonlTraceWriter
from vision_core.registry import create_detector

from runtime_engine.engine import FrameStore, SessionEngine

__all__ = ["build_session"]


def build_session(
    project_dir: str | Path,
    *,
    mode: RunMode | str,
    clock: Clock,
    capture: CaptureSource,
    sink: InputSink,
    machine_id: str | None = None,
    session_id: str | None = None,
    foreground: ForegroundContext | None = None,
    frame_store: FrameStore | None = None,
    max_ticks: int | None = None,
    trace_path: str | Path | None = None,
    budget_ms: float = 12.0,
) -> SessionEngine:
    """从项目目录装配一次可运行会话。

    Args:
        project_dir: 项目根目录（含 project.yaml 与各子目录）。
        mode:        运行模式（observe/shadow/dry_run/real_input）。
        clock:       注入时钟。
        capture:     采集源（真实适配器或 Fake）。
        sink:        输入执行器（真实适配器或 FakeInputSink）。
        machine_id:  状态机 ID；缺省取项目中字典序第一个。
        session_id:  会话 ID；缺省自动生成。
        foreground:  前台上下文（real_input 模式建议显式提供）。
        frame_store: 帧像素留痕存储；None 表示禁用。
        max_ticks:   run() 默认 tick 上限。
        trace_path:  轨迹文件路径；缺省写入 ``<项目>/traces/<trace-id>.jsonl``。
        budget_ms:   每帧检测计算预算（毫秒）。

    Raises:
        DomainValidationError: 项目解析失败或静态分析存在 error 级问题。
        ValueError: 模式/状态机/目标/策略缺失或不合法。
    """
    bundle = load_project(Path(project_dir))
    # 编译前置门：存在 error 级静态问题直接拒绝（AC-P0-10）。
    ensure_compilable(bundle)

    run_mode = parse_mode(mode)
    if run_mode is None:
        raise ValueError(f"未知运行模式: {mode!r}")

    machine = compile_project_machine(bundle, machine_id or _default_machine_id(bundle))
    detectors = {
        detector_id: create_detector(config, **_detector_kwargs(bundle, config))
        for detector_id, config in bundle.detectors.items()
    }
    policy = PolicyEvaluator(_policy_input(bundle))
    target = _target_ref(bundle)

    broker = InputBroker(sink, clock=clock)
    broker.mode = run_mode.value

    path = (
        Path(trace_path)
        if trace_path is not None
        else bundle.root / "traces" / f"{new_id('trace')}.jsonl"
    )
    trace = JsonlTraceWriter(path)

    return SessionEngine(
        project=bundle,
        machine=machine,
        detectors=detectors,
        policy=policy,
        broker=broker,
        capture=capture,
        trace=trace,
        clock=clock,
        mode=run_mode,
        target=target,
        foreground=foreground,
        frame_store=frame_store,
        max_ticks=max_ticks,
        session_id=session_id,
        budget_ms=budget_ms,
    )


# --------------------------------------------------------------------------- 内部


def _default_machine_id(bundle: ProjectBundle) -> str:
    """缺省状态机：项目中字典序第一个；不存在则报错。"""
    if not bundle.machines:
        raise ValueError(f"项目中未定义状态机：{bundle.root}")
    return sorted(bundle.machines)[0]


def _detector_kwargs(bundle: ProjectBundle, config: DetectorConfig) -> dict[str, Any]:
    """按检测器类型补齐构造参数：template_match 需要模板文件路径。"""
    if config.type_value is DetectorType.TEMPLATE_MATCH and config.template:
        return {"template_path": bundle.root / config.template}
    return {}


def _policy_input(bundle: ProjectBundle) -> PolicyInput:
    """项目策略 -> PolicyInput（缺省取字典序第一个；无策略用保守默认）。"""
    if not bundle.policies:
        return PolicyInput()
    profile = bundle.policies[sorted(bundle.policies)[0]]
    return PolicyInput(
        mode=str(profile.mode_value.value),
        require_manual_start=bool(profile.require_manual_start),
        max_runtime_minutes=float(profile.max_runtime_minutes),
        max_actions_per_minute=int(profile.max_actions_per_minute),
        max_total_actions=(
            int(profile.max_total_actions)
            if profile.max_total_actions is not None
            else PolicyInput().max_total_actions
        ),
        unattended_schedule=str(profile.unattended_value.value),
    )


def _target_ref(bundle: ProjectBundle) -> TargetRef:
    """项目目标 -> TargetRef（取字典序第一个；项目必须定义目标）。"""
    if not bundle.targets:
        raise ValueError(f"项目中未定义目标档案：{bundle.root}")
    profile = bundle.targets[sorted(bundle.targets)[0]]
    return TargetRef(
        target_id=profile.target_id,
        protected_online=bool(profile.protected_online),
        title_regex=profile.title_regex,
    )
