"""工作区配置与安全默认值（CTL-008）。

默认值（安全优先）：
- ``default_mode`` = ``shadow``：新会话默认影子模式；
- ``trace_retention_days`` = 7：轨迹短保留期；
- ``unattended_schedule`` = ``disabled``：定时自动启动默认关闭。

安全规则：工作区存在受保护在线目标时，禁止通过 API 把 ``default_mode``
改为 ``real_input``，也禁止开启 ``unattended_schedule``——配置面永远不能
成为绕过目标级保护（``TargetProfile.real_input_allowed``）的通道。
配置存 ``<project_root>/settings.json``；文件缺失/损坏时回退安全默认值。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from policy_engine.modes import RunMode

from control_plane.errors import ControlPlaneError
from control_plane.storage import ProjectStore

#: 安全默认值（CTL-008）
DEFAULT_SETTINGS: dict[str, Any] = {
    "default_mode": "shadow",
    "trace_retention_days": 7,
    "unattended_schedule": "disabled",
}

#: 允许的键集合
_SETTING_KEYS = frozenset(DEFAULT_SETTINGS)

#: 允许的调度取值
_SCHEDULE_VALUES = frozenset({"enabled", "disabled"})


class SettingsManager:
    """工作区设置管理器（默认值合并 + 安全校验 + 持久化）。"""

    def __init__(self, project_root: Path, *, store: ProjectStore) -> None:
        self.path = Path(project_root) / "settings.json"
        self._store = store

    def get(self) -> dict[str, Any]:
        """读取当前设置（文件缺失/损坏时返回安全默认值）。"""
        stored: dict[str, Any] = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                stored = data
        merged = dict(DEFAULT_SETTINGS)
        for key in _SETTING_KEYS:
            if key in stored:
                merged[key] = stored[key]
        return merged

    def update(self, patch: Any) -> dict[str, Any]:
        """局部更新设置；逐键校验并执行安全默认值硬约束。"""
        if not isinstance(patch, dict):
            raise ControlPlaneError(422, "invalid_body", "settings 更新体必须是 JSON 对象")
        unknown = sorted(set(patch) - _SETTING_KEYS)
        if unknown:
            raise ControlPlaneError(
                422,
                "unknown_field",
                f"不支持的字段：{unknown}",
                extra={"allowed": sorted(_SETTING_KEYS)},
            )

        protected_checked = False
        has_protected = False

        def _protected_present() -> bool:
            nonlocal protected_checked, has_protected
            if not protected_checked:
                has_protected = self._store.workspace_has_protected_target()
                protected_checked = True
            return has_protected

        if "default_mode" in patch:
            mode = patch["default_mode"]
            if mode not in {m.value for m in RunMode}:
                raise ControlPlaneError(
                    422,
                    "invalid_enum",
                    f"default_mode 非法；允许值：{[m.value for m in RunMode]}",
                )
            if mode == RunMode.REAL_INPUT.value and _protected_present():
                raise ControlPlaneError(
                    409,
                    "protected_online_no_real_input",
                    "工作区存在受保护在线目标，禁止把默认模式改为 real_input（CTL-008 安全默认值）",
                )
        if "trace_retention_days" in patch:
            days = patch["trace_retention_days"]
            if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
                raise ControlPlaneError(422, "out_of_range", "trace_retention_days 必须是 1~365 的整数")
        if "unattended_schedule" in patch:
            schedule = patch["unattended_schedule"]
            if schedule not in _SCHEDULE_VALUES:
                raise ControlPlaneError(
                    422,
                    "invalid_enum",
                    f"unattended_schedule 只允许 {sorted(_SCHEDULE_VALUES)}",
                )
            if schedule == "enabled" and _protected_present():
                raise ControlPlaneError(
                    409,
                    "protected_online_unattended_forbidden",
                    "工作区存在受保护在线目标，禁止开启无人值守调度（CTL-008 安全默认值）",
                )

        merged = self.get()
        merged.update(patch)
        self._save(merged)
        return merged

    def _save(self, settings: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({k: settings[k] for k in _SETTING_KEYS}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
