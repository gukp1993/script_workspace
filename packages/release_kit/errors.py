"""release_kit 的异常与失败步骤定义（VER-001~010 共用）。

设计约定：
- :class:`ReleaseError` 聚合"阻断原因"：静态分析问题（Issue 列表）或
  质量闸门/完整性校验的原因字符串列表，调用方（UI/CLI）可直接展示；
- :class:`CompatError` 是运行时不兼容的专用错误，消息内含迁移建议文案；
- :class:`MigrationStepError` 是 Schema 迁移 callable 的约定异常：
  携带失败步骤名，升级失败诊断（AC-P0-14）据此报告失败步骤。
"""

from __future__ import annotations

from domain_model.errors import Issue


class ReleaseError(Exception):
    """发布/回滚/导入流程被阻断。

    Attributes:
        issues:  静态分析/解析层面的可定位问题（可空）。
        reasons: 闸门或完整性校验失败原因（可空）。
    """

    def __init__(
        self,
        message: str,
        *,
        issues: list[Issue] | None = None,
        reasons: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.issues: list[Issue] = list(issues or [])
        self.reasons: list[str] = list(reasons or [])

    def __str__(self) -> str:
        parts = [self.message]
        for issue in self.issues:
            parts.append(f"  ERROR {issue.file} {issue.pointer or '/'} {issue.rule}: {issue.message}")
        for reason in self.reasons:
            parts.append(f"  - {reason}")
        return "\n".join(parts)


class CompatError(ReleaseError):
    """运行时兼容检查失败（VER-010）：消息中包含迁移建议文案。"""


class MigrationStepError(RuntimeError):
    """Schema 迁移 callable 的失败约定：携带失败步骤名（AC-P0-14 诊断用）。

    迁移函数（:func:`release_kit.rollback.ReleaseRollback.upgrade_with_backup`
    的 ``migrate_fn`` 参数）在任一步骤失败时应抛出本异常；抛出其他异常时
    失败步骤名记为 ``unspecified``。
    """

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step
