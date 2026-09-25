"""领域模型的错误与可定位问题（DOM-003）。

设计约定：
- 所有解析/校验错误都携带 JSON Pointer 风格的字段路径与规则 ID，
  便于 UI 直接定位到出错字段并给出修复提示。
- 单条问题用 :class:`Issue` 表示；解析函数失败时抛出
  :class:`DomainValidationError`（聚合若干 Issue）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Issue:
    """一条可定位的校验问题。

    Attributes:
        file:    出现问题的文件（相对展示路径；解析阶段可用 ``<data>``）。
        pointer: 文件内 JSON Pointer 风格字段路径，如 ``/roi/2``；对象级问题为空串。
        rule:    规则 ID，如 ``roi_out_of_bounds``、``protected_online_no_real_input``。
        message: 人类可读的错误描述（中文）。
        hint:    修复提示（中文，可为空）。
    """

    file: str
    pointer: str
    rule: str
    message: str
    hint: str = ""

    def format(self, *, json_mode: bool = False) -> str:
        """格式化为单行输出；``json_mode`` 时输出一行 JSON。"""
        if json_mode:
            import json

            return json.dumps(
                {
                    "file": self.file,
                    "pointer": self.pointer,
                    "rule": self.rule,
                    "message": self.message,
                    "hint": self.hint,
                },
                ensure_ascii=False,
            )
        parts = [f"ERROR {self.file}", self.pointer or "/", self.rule, self.message]
        if self.hint:
            parts.append(f"提示：{self.hint}")
        return "  ".join(p for p in parts if p)


def join_pointer(*parts: str | int) -> str:
    """把若干段拼成 JSON Pointer，如 ``("states", "idle", 0, "to")`` -> ``/states/idle/0/to``。"""
    out = ""
    for part in parts:
        out += f"/{part}"
    return out


@dataclass
class DomainValidationError(Exception):
    """解析/校验失败异常，携带聚合的 :class:`Issue` 列表。"""

    issues: list[Issue] = field(default_factory=list)

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    def __str__(self) -> str:  # pragma: no cover - 仅用于人类阅读
        lines = [i.format() for i in self.issues] or ["未知领域模型错误"]
        return "；".join(lines)


class DomainModelError(Exception):
    """领域模型基础异常（未定位到具体字段时使用）。"""
