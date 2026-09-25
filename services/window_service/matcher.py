"""目标窗口匹配（TGT-002 / CAP-012）。

规则（与 TargetProfile 对齐，本模块 import domain_model 只取类型）：
- 标题必须命中 title_regex（re.search 部分匹配）；
- exe 名大小写不敏感精确匹配；窗口快照取不到 exe 名（空串）视为不命中；
- profile.window_class 非空时还要求窗口类名精确一致；

安全约定（CAP-012）：多个窗口同时命中时**不得自动挑选**——全部返回，
并由 ambiguous=True 明确要求人工确认实例后才能建立会话绑定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from domain_model.models import TargetProfile

from window_service.models import WindowInfo


@dataclass(frozen=True)
class MatchResult:
    """匹配结果：命中的全部窗口 + 歧义信号。

    ambiguous=True 表示命中数量 > 1：必须先人工确认目标实例，
    才允许建立 SessionBinding（不得自动挑选，CAP-012）。
    """

    matches: list[WindowInfo] = field(default_factory=list)
    ambiguous: bool = False

    @property
    def unambiguous_match(self) -> WindowInfo | None:
        """仅当恰好命中一个窗口时返回它；其余情况一律 None（不自动挑选）。"""
        return self.matches[0] if len(self.matches) == 1 else None


def match_targets_detailed(
    profile: TargetProfile, windows: list[WindowInfo]
) -> MatchResult:
    """按目标档案匹配窗口并携带歧义信号（TGT-002 / CAP-012）。"""
    try:
        title_re = re.compile(profile.title_regex)
    except re.error as exc:  # TargetProfile.__post_init__ 已校验，这里兜底防御
        raise ValueError(f"title_regex 无法编译：{exc}") from exc

    expected_exe = profile.executable.strip().lower()
    expected_class = (profile.window_class or "").strip()

    matches: list[WindowInfo] = []
    for window in windows:
        if not title_re.search(window.title or ""):
            continue
        if not window.exe_name or window.exe_name.strip().lower() != expected_exe:
            continue
        if expected_class and (window.class_name or "") != expected_class:
            continue
        matches.append(window)

    return MatchResult(matches=matches, ambiguous=len(matches) > 1)


def match_targets(
    profile: TargetProfile, windows: list[WindowInfo]
) -> list[WindowInfo]:
    """按目标档案匹配窗口，返回全部命中（多命中即歧义，不自动挑选）。

    需要歧义信号时请使用 :func:`match_targets_detailed`。
    """
    return match_targets_detailed(profile, windows).matches
