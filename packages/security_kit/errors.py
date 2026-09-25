"""security_kit 公共异常类型（SEC-002/004/005/006/007）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # 仅为类型注解引入，避免运行时循环导入
    from security_kit.path_guard import PathIssue


class SecurityKitError(Exception):
    """security_kit 全部异常的基类（便于调用方统一捕获）。"""


class PathGuardError(SecurityKitError):
    """路径防护拒绝（SEC-002）。

    Attributes:
        rule_id: 命中的规则 ID（absolute_path / parent_escape / …）。
        path:    被拒绝的原始路径字符串。
    """

    def __init__(self, rule_id: str, path: str, message: str) -> None:
        super().__init__(f"[{rule_id}] {message}（path={path!r}）")
        self.rule_id = rule_id
        self.path = path
        self.message = message


class IntegrityError(SecurityKitError):
    """资产完整性校验不通过（SEC-004）。

    ensure_untouched 在发现缺失/被改/多余资产时抛出本异常，
    ``reports`` 携带逐条 TamperReport（也以 message 汇总）。
    """

    def __init__(self, message: str, reports: Sequence[object] = ()) -> None:
        super().__init__(message)
        self.reports: list[object] = list(reports)
