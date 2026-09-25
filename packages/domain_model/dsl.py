"""DOM-006 安全条件表达式 DSL 与状态机编译入口（FSM-001/002）。

安全设计（SEC-001 / FSM-001 / ADR-0003）：
- 条件表达式只允许白名单语法：数字/布尔字面量、感知字段引用
  （``field`` / ``field.present`` / ``field.value`` / ``field.confidence`` /
  ``field.changed``）、比较运算、逻辑运算、算术运算与括号；
  其余一切语法（函数调用、字符串字面量、属性链深入、赋值、import、
  链式比较等）在解析期直接以 :class:`ParseError` 拒绝并给出行列位置；
- 求值是纯 AST 解释器：全包不出现 ``exec`` / ``eval`` / ``compile``；
  字段缺失时取安全默认（present=False / value=0 / confidence=0），
  并在 :attr:`ExprEval.missing_fields` 中记录；求值绝不会抛出异常，
  保证同一快照的求值结果确定（FSM-003/009 的基础）；
- 状态机编译（:func:`compile_machine_dict` / :func:`compile_machine_yaml` /
  :func:`compile_state_machine`）把 YAML/JSON 字典或
  :class:`~domain_model.models.StateMachineDef` 编译为
  :class:`CompiledMachine` 运行图：迁移表已按 (priority, 声明顺序) 排序、
  条件已解析为 AST、动作已分类；所有编译错误携带源位置
  （文件名 + JSON Pointer + 表达式内行列）。

动作 kind 与能力的对应（DOM-005 授权模型的静态侧）::

    press_key / key_down / key_up -> input.key
    click / move                  -> input.mouse
    wheel                         -> input.wheel
    wait / manual_gate / assert_after / wait_until -> None（运行控制，无真实输入）
    retry / on_error_to           -> None（状态配置动作，编译期消费）

未登记的 kind 在编译与静态分析中均被拒绝（规则 ``action_unauthorized``）。

状态级配置约定（受 Schema additionalProperties 限制，用动作声明表达）::

    entry:
      - kind: retry            # 有界重试策略（FSM-008）
        max_attempts: 3        # 必填正整数；缺失 -> 无限重试（infinite_retry）
        backoff: exponential   # fixed | exponential（可选，默认 fixed）
        base_ms: 200           # 基础退避毫秒（可选，默认 100）
        max_ms: 5000           # 退避上限毫秒（可选，默认 30000）
      - kind: on_error_to      # 异常路由目标（FSM-008）
        to: failed
      - kind: assert_after     # 动作后断言（FSM-007）
        when: "ready.present"
        timeout_seconds: 2
        to: recovery
      - kind: wait_until       # 阻塞式等待断言（FSM-007）
        when: "gate.present"
        timeout_seconds: 5
        to: gaveup
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from domain_model.capabilities import CAPABILITY_REGISTRY
from domain_model.errors import DomainValidationError, Issue
from domain_model.models import (
    OBSERVATION_ATTRIBUTES,
    ActionDecl,
    PerceptionSnapshot,
    StateDef,
    StateMachineDef,
)
from domain_model.parsing import load_config_file, parse_machine

__all__ = [
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "ExprAST",
    "NumberLit",
    "BoolLit",
    "FieldRef",
    "UnaryOp",
    "BinOp",
    "ParseError",
    "ExprEval",
    "parse_expression",
    "collect_field_refs",
    "parse_duration_seconds",
    "ACTION_KIND_CAPABILITY",
    "CONFIG_ACTION_KINDS",
    "ASSERTION_ACTION_KINDS",
    "RetrySpec",
    "CompiledAction",
    "CompiledAssertion",
    "CompiledTransition",
    "CompiledState",
    "CompiledMachine",
    "compile_with_diagnostics",
    "compile_state_machine",
    "compile_machine_dict",
    "compile_machine_yaml",
]


# ---------------------------------------------------------------------------
# 严重级别常量（静态分析诊断使用）
# ---------------------------------------------------------------------------

#: 阻断编译/运行的错误级别
SEVERITY_ERROR: str = "error"
#: 提示性告警（不阻断）
SEVERITY_WARNING: str = "warning"

#: 感知字段允许的子属性白名单（与 models.OBSERVATION_ATTRIBUTES 一致）
_OBS_ATTRS: frozenset[str] = frozenset(OBSERVATION_ATTRIBUTES)


# ---------------------------------------------------------------------------
# 条件表达式 AST（DOM-006 / FSM-001）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExprAST:
    """表达式 AST 节点基类：保留源位置（行/列，从 1 开始）。"""

    line: int
    col: int


@dataclass(frozen=True)
class NumberLit(ExprAST):
    """数字字面量（浮点）。"""

    value: float


@dataclass(frozen=True)
class BoolLit(ExprAST):
    """布尔字面量（true / false，大小写不敏感）。"""

    value: bool


@dataclass(frozen=True)
class FieldRef(ExprAST):
    """感知字段引用。

    Attributes:
        name: 感知字段名（即 Detector.field_name）。
        attr: 子属性（present/value/confidence/changed 之一）；
              ``None`` 表示裸字段引用，按 ``present`` 语义求值。
    """

    name: str
    attr: str | None


@dataclass(frozen=True)
class UnaryOp(ExprAST):
    """一元运算：``not``（逻辑非）或 ``neg``（算术取负）。"""

    op: str
    operand: ExprAST


@dataclass(frozen=True)
class BinOp(ExprAST):
    """二元运算：and/or、比较（< <= > >= == !=）、算术（+ - * /）。"""

    op: str
    left: ExprAST
    right: ExprAST


class ParseError(Exception):
    """条件表达式解析失败：携带表达式内的行/列位置。"""

    def __init__(self, message: str, line: int = 1, col: int = 1, src: str = "") -> None:
        super().__init__(f"第 {line} 行第 {col} 列：{message}")
        self.message = message
        self.line = line
        self.col = col
        self.src = src


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    """词法记号：kind ∈ {number, ident, op, eof}。"""

    kind: str
    value: object
    line: int
    col: int


_TWO_CHAR_OPS: tuple[str, ...] = ("<=", ">=", "==", "!=")
_ONE_CHAR_OPS: str = "+-*/<>()."
_COMPARE_OPS: frozenset[str] = frozenset({"<", "<=", ">", ">=", "==", "!="})


def _tokenize(src: str) -> list[_Token]:
    """把表达式源文本切分为记号；遇到白名单外字符立即报错。"""
    tokens: list[_Token] = []
    i, line, col = 0, 1, 1
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\n":
            i += 1
            line += 1
            col = 1
            continue
        if ch in " \t\r":
            i += 1
            col += 1
            continue
        if ch in "\"'":
            raise ParseError("不允许字符串字面量（条件 DSL 没有字符串类型）", line, col, src)
        if ch.isdigit():
            start = i
            start_col = col
            while i < n and src[i].isdigit():
                i += 1
                col += 1
            if i < n and src[i] == ".":
                i += 1
                col += 1
                if i >= n or not src[i].isdigit():
                    raise ParseError("数字字面量小数点后必须跟数字", line, col, src)
                while i < n and src[i].isdigit():
                    i += 1
                    col += 1
            tokens.append(_Token("number", float(src[start:i]), line, start_col))
            continue
        if ch.isalpha() or ch == "_":
            start = i
            start_col = col
            while i < n and (src[i].isalnum() or src[i] == "_"):
                i += 1
                col += 1
            tokens.append(_Token("ident", src[start:i], line, start_col))
            continue
        two = src[i : i + 2]
        if two in _TWO_CHAR_OPS:
            tokens.append(_Token("op", two, line, col))
            i += 2
            col += 2
            continue
        if ch in _ONE_CHAR_OPS:
            tokens.append(_Token("op", ch, line, col))
            i += 1
            col += 1
            continue
        raise ParseError(f"不允许的字符 {ch!r}", line, col, src)
    tokens.append(_Token("eof", None, line, col))
    return tokens


# ---------------------------------------------------------------------------
# 递归下降解析器（优先级从低到高：or < and < not < 比较 < 加减 < 乘除 < 一元负号）
# ---------------------------------------------------------------------------


class _Parser:
    """白名单语法的递归下降解析器（内部实现）。"""

    def __init__(self, tokens: list[_Token], src: str) -> None:
        self._toks = tokens
        self._i = 0
        self._src = src

    def _peek(self) -> _Token:
        return self._toks[self._i]

    def _next(self) -> _Token:
        tok = self._toks[self._i]
        self._i += 1
        return tok

    def _error(self, msg: str, tok: _Token) -> None:
        raise ParseError(msg, tok.line, tok.col, self._src)

    def parse(self) -> ExprAST:
        """解析完整表达式；末尾有多余内容视为错误。"""
        first = self._peek()
        if first.kind == "eof":
            self._error("表达式为空", first)
        node = self._or()
        tail = self._peek()
        if tail.kind != "eof":
            self._error("表达式末尾存在多余内容", tail)
        return node

    def _or(self) -> ExprAST:
        node = self._and()
        while True:
            tok = self._peek()
            if tok.kind == "ident" and isinstance(tok.value, str) and tok.value.lower() == "or":
                self._next()
                node = BinOp(op="or", left=node, right=self._and(), line=tok.line, col=tok.col)
            else:
                return node

    def _and(self) -> ExprAST:
        node = self._not()
        while True:
            tok = self._peek()
            if tok.kind == "ident" and isinstance(tok.value, str) and tok.value.lower() == "and":
                self._next()
                node = BinOp(op="and", left=node, right=self._not(), line=tok.line, col=tok.col)
            else:
                return node

    def _not(self) -> ExprAST:
        tok = self._peek()
        if tok.kind == "ident" and isinstance(tok.value, str) and tok.value.lower() == "not":
            self._next()
            return UnaryOp(op="not", operand=self._not(), line=tok.line, col=tok.col)
        return self._comparison()

    def _comparison(self) -> ExprAST:
        left = self._additive()
        tok = self._peek()
        if tok.kind == "op" and tok.value in _COMPARE_OPS:
            self._next()
            right = self._additive()
            nxt = self._peek()
            if nxt.kind == "op" and nxt.value in _COMPARE_OPS:
                self._error("不允许链式比较（如 a < b < c）", nxt)
            return BinOp(op=str(tok.value), left=left, right=right, line=tok.line, col=tok.col)
        return left

    def _additive(self) -> ExprAST:
        node = self._multiplicative()
        while True:
            tok = self._peek()
            if tok.kind == "op" and tok.value in ("+", "-"):
                self._next()
                node = BinOp(op=str(tok.value), left=node, right=self._multiplicative(),
                             line=tok.line, col=tok.col)
            else:
                return node

    def _multiplicative(self) -> ExprAST:
        node = self._unary()
        while True:
            tok = self._peek()
            if tok.kind == "op" and tok.value in ("*", "/"):
                self._next()
                node = BinOp(op=str(tok.value), left=node, right=self._unary(),
                             line=tok.line, col=tok.col)
            else:
                return node

    def _unary(self) -> ExprAST:
        tok = self._peek()
        if tok.kind == "op" and tok.value == "-":
            self._next()
            return UnaryOp(op="neg", operand=self._unary(), line=tok.line, col=tok.col)
        if tok.kind == "op" and tok.value == "+":
            self._next()
            return self._unary()
        return self._primary()

    def _primary(self) -> ExprAST:
        tok = self._next()
        if tok.kind == "number":
            return NumberLit(value=float(tok.value), line=tok.line, col=tok.col)  # type: ignore[arg-type]
        if tok.kind == "ident":
            text = str(tok.value)
            low = text.lower()
            if low == "true":
                return BoolLit(value=True, line=tok.line, col=tok.col)
            if low == "false":
                return BoolLit(value=False, line=tok.line, col=tok.col)
            if low in ("and", "or", "not"):
                self._error(f"逻辑关键字 {text} 不能出现在这里", tok)
            nxt = self._peek()
            if nxt.kind == "op" and nxt.value == "(":
                self._error("不允许函数调用（白名单 DSL 没有函数）", nxt)
            if nxt.kind == "op" and nxt.value == ".":
                self._next()
                attr_tok = self._next()
                if attr_tok.kind != "ident":
                    self._error("字段引用的点后必须是属性名", attr_tok)
                attr = str(attr_tok.value)
                if attr not in _OBS_ATTRS:
                    self._error(
                        f"不允许的感知属性 {attr!r}；允许的属性：{sorted(_OBS_ATTRS)}",
                        attr_tok,
                    )
                after = self._peek()
                if after.kind == "op" and after.value == ".":
                    self._error("属性链只允许一层（字段.属性）", after)
                if after.kind == "op" and after.value == "(":
                    self._error("不允许函数调用（白名单 DSL 没有函数）", after)
                return FieldRef(name=text, attr=attr, line=tok.line, col=tok.col)
            return FieldRef(name=text, attr=None, line=tok.line, col=tok.col)
        if tok.kind == "op" and tok.value == "(":
            node = self._or()
            close = self._next()
            if close.kind != "op" or close.value != ")":
                self._error("括号不匹配：缺少右括号", close)
            return node
        self._error(f"意外的记号 {tok.value!r}", tok)


def parse_expression(src: str) -> ExprAST:
    """解析白名单条件表达式为 AST；语法之外的一切立即 :class:`ParseError`。

    典型合法示例：``ready.present``、``health_ratio.value < 0.20``、
    ``a.present and b.value + 1 >= 2 or not c.confidence > 0.5``。
    """
    if not isinstance(src, str):
        raise ParseError("条件表达式必须是字符串", 1, 1, str(src))
    return _Parser(_tokenize(src), src).parse()


def collect_field_refs(ast: ExprAST) -> tuple[str, ...]:
    """按出现顺序收集 AST 引用的感知字段名（去重）。"""
    out: list[str] = []
    seen: set[str] = set()

    def walk(node: ExprAST) -> None:
        if isinstance(node, FieldRef):
            if node.name not in seen:
                seen.add(node.name)
                out.append(node.name)
        elif isinstance(node, UnaryOp):
            walk(node.operand)
        elif isinstance(node, BinOp):
            walk(node.left)
            walk(node.right)

    walk(ast)
    return tuple(out)


# ---------------------------------------------------------------------------
# 求值器（纯 AST 解释，无 exec/eval；对缺失字段取安全默认）
# ---------------------------------------------------------------------------


def _truthy(value: bool | float) -> bool:
    """白名单 DSL 的真值语义：布尔取本身，数值非零为真。"""
    return value if isinstance(value, bool) else value != 0.0


def _as_number(value: bool | float) -> float:
    """把求值结果当作数值：布尔按 0/1，其余（含异常观测值）安全归零。"""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    value_f = float(value)
    return value_f if math.isfinite(value_f) else 0.0


class ExprEval:
    """表达式求值器：同一快照恒产生同一结果（确定性，FSM-003/009）。

    Attributes:
        missing_fields: 最近一次 :meth:`evaluate` 中缺失（取了安全默认）的
                        字段引用列表，如 ``("ghost.present", "hp.value")``。
    """

    def __init__(self) -> None:
        self.missing_fields: tuple[str, ...] = ()

    def evaluate(self, ast: ExprAST, snapshot: PerceptionSnapshot) -> bool | float:
        """对 AST 求值；绝不抛出异常，缺失字段取安全默认并记录。"""
        missing: list[str] = []
        result = self._eval(ast, snapshot, missing)
        self.missing_fields = tuple(missing)
        return result

    def _eval(self, node: ExprAST, snapshot: PerceptionSnapshot, missing: list[str]) -> bool | float:
        if isinstance(node, NumberLit):
            return float(node.value)
        if isinstance(node, BoolLit):
            return node.value
        if isinstance(node, FieldRef):
            return self._field(node, snapshot, missing)
        if isinstance(node, UnaryOp):
            if node.op == "not":
                return not _truthy(self._eval(node.operand, snapshot, missing))
            return -_as_number(self._eval(node.operand, snapshot, missing))
        if isinstance(node, BinOp):
            return self._binop(node, snapshot, missing)
        raise TypeError(f"未知 AST 节点: {type(node).__name__}")  # pragma: no cover - 防御

    def _field(self, ref: FieldRef, snapshot: PerceptionSnapshot, missing: list[str]) -> bool | float:
        """字段引用求值：缺失 -> present=False / value=0 / confidence=0。"""
        obs = snapshot.values.get(ref.name)
        display = ref.name if ref.attr is None else f"{ref.name}.{ref.attr}"
        if obs is None:
            missing.append(display)
            return 0.0 if ref.attr in ("value", "confidence") else False
        attr = ref.attr if ref.attr is not None else "present"
        if attr == "present":
            return obs.present
        if attr == "changed":
            # 聚合层（M2 后续）才会产出 changed；当前恒 False 的安全默认。
            return False
        if not obs.present:
            # 字段本帧未观测到：按缺失处理（安全默认 + 记录）。
            missing.append(display)
            return 0.0
        if attr == "confidence":
            return float(obs.confidence)
        # attr == "value"：原始观测值（数值/布尔语义由上层运算归一）
        value = obs.value
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return float(value)
        # present 但值非数值：安全归零（不视为缺失，是数据问题）
        return 0.0

    def _binop(self, node: BinOp, snapshot: PerceptionSnapshot, missing: list[str]) -> bool | float:
        op = node.op
        left = self._eval(node.left, snapshot, missing)
        right = self._eval(node.right, snapshot, missing)
        if op == "and":
            return _truthy(left) and _truthy(right)
        if op == "or":
            return _truthy(left) or _truthy(right)
        if op in ("<", "<=", ">", ">="):
            ln, rn = _as_number(left), _as_number(right)
            if op == "<":
                return ln < rn
            if op == "<=":
                return ln <= rn
            if op == ">":
                return ln > rn
            return ln >= rn
        if op == "==":
            if isinstance(left, bool) and isinstance(right, bool):
                return left == right
            if isinstance(left, bool) or isinstance(right, bool):
                return False
            return _as_number(left) == _as_number(right)
        if op == "!=":
            if isinstance(left, bool) and isinstance(right, bool):
                return left != right
            if isinstance(left, bool) or isinstance(right, bool):
                return True
            return _as_number(left) != _as_number(right)
        # 算术：+ - * /（除零安全归零，保持求值总量性）
        ln, rn = _as_number(left), _as_number(right)
        if op == "+":
            return ln + rn
        if op == "-":
            return ln - rn
        if op == "*":
            return ln * rn
        if rn == 0.0:
            return 0.0
        return ln / rn


# ---------------------------------------------------------------------------
# 时长字面量（数值带单位：30s / 250ms / 2m；仅用于超时类字段，不进表达式）
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m)\s*$")
_DURATION_UNITS: dict[str, float] = {"ms": 0.001, "s": 1.0, "m": 60.0}


def parse_duration_seconds(value: object) -> float:
    """把时长解析为浮点秒：数字（按秒）或 ``"30s"`` / ``"250ms"`` / ``"2m"``。

    Raises:
        ValueError: 不是正数也不是合法的带单位字面量。
    """
    if isinstance(value, bool):
        raise ValueError("时长不能是布尔值")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(f"时长必须是正数，得到 {value!r}")
        return seconds
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match is None:
            raise ValueError(f"无法识别的时长字面量 {value!r}；支持如 30s / 250ms / 2m")
        return float(match.group(1)) * _DURATION_UNITS[match.group(2)]
    raise ValueError(f"无法识别的时长 {value!r}")


# ---------------------------------------------------------------------------
# 动作 kind 分类与能力映射（DOM-005 静态侧）
# ---------------------------------------------------------------------------

#: 动作 kind -> 所需能力（None 表示运行控制类，不产生真实输入）
ACTION_KIND_CAPABILITY: Mapping[str, str | None] = {
    # 真实输入类（受限能力，需目标 real_input_allowed 才能执行）
    "press_key": "input.key",
    "key_down": "input.key",
    "key_up": "input.key",
    "click": "input.mouse",
    "move": "input.mouse",
    "wheel": "input.wheel",
    # 运行控制类（不产生真实输入）
    "wait": None,
    "manual_gate": None,
    "assert_after": None,
    "wait_until": None,
    # 状态配置类（编译期消费，不进入运行期动作序列）
    "retry": None,
    "on_error_to": None,
}

#: 编译期消费的配置动作（提取为状态属性，不进入运行期动作表）
CONFIG_ACTION_KINDS: frozenset[str] = frozenset({"retry", "on_error_to"})

#: 断言类动作（编译为 CompiledAssertion，FSM-007）
ASSERTION_ACTION_KINDS: frozenset[str] = frozenset({"assert_after", "wait_until"})


# ---------------------------------------------------------------------------
# 编译产物：运行图（FSM-002）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrySpec:
    """有界重试策略（FSM-008，由 entry 的 ``retry`` 动作提取）。

    Attributes:
        max_attempts: 最大尝试次数（>=1；None 表示未配置上限——编译会被
                      ``infinite_retry`` 规则拒绝，运行时按 1 防御）。
        backoff:      ``fixed``（固定退避）或 ``exponential``（指数退避）。
        base_seconds: 基础退避秒数。
        max_seconds:  退避上限秒数（指数退避的封顶值）。
    """

    max_attempts: int | None
    backoff: str
    base_seconds: float
    max_seconds: float


#: 固定退避
BACKOFF_FIXED: str = "fixed"
#: 指数退避
BACKOFF_EXPONENTIAL: str = "exponential"


@dataclass(frozen=True)
class CompiledAction:
    """运行期动作（entry/exit 序列中的一步）。"""

    kind: str
    params: Mapping[str, Any]
    pointer: str


@dataclass(frozen=True)
class CompiledAssertion:
    """断言配置（FSM-007，由 assert_after / wait_until 动作提取）。"""

    kind: str  # assert_after | wait_until
    ast: ExprAST
    when_src: str
    timeout_seconds: float
    failure_state: str
    message: str
    pointer: str


@dataclass(frozen=True)
class CompiledTransition:
    """编译后的迁移：条件已解析，排序键为 (priority, 声明顺序)。

    Attributes:
        source_state:   源状态名。
        decl_index:     在源状态 transitions 中的声明下标（从 0 起）。
        when_src:       条件表达式原文（用于诊断与哈希）。
        ast:            解析后的条件 AST。
        target:         迁移目标状态名。
        on_timeout_to:  状态超时时的迁移目标（可选）。
        priority:       优先级，数值越小越先评估；默认等于声明下标。
        stable_frames:  迁移级稳定帧数：连续 N 个 tick 守卫为真才迁移（>=1）。
        pointer:        源位置（JSON Pointer）。
    """

    source_state: str
    decl_index: int
    when_src: str
    ast: ExprAST
    target: str
    on_timeout_to: str | None
    priority: int
    stable_frames: int
    pointer: str

    @property
    def sort_key(self) -> tuple[int, int]:
        """确定性排序键：(priority, 声明顺序)。"""
        return (self.priority, self.decl_index)


@dataclass(frozen=True)
class CompiledState:
    """编译后的状态：排好序的迁移表 + 分类后的动作。"""

    name: str
    transitions: tuple[CompiledTransition, ...]
    entry: tuple[CompiledAction | CompiledAssertion, ...]
    exit: tuple[CompiledAction | CompiledAssertion, ...]
    timeout_seconds: float | None
    terminal: bool
    retry_spec: RetrySpec | None
    error_target: str | None
    #: 首个带 on_timeout_to 的迁移（排序后）；状态超时时迁移到它的目标
    timeout_transition: CompiledTransition | None
    pointer: str


@dataclass(frozen=True)
class CompiledMachine:
    """可执行的状态机运行图（编译产物）。

    Attributes:
        machine_id:  状态机 ID。
        initial:     初始状态名。
        states:      状态名 -> 编译后状态。
        source_file: 来源文件（诊断展示用）。
        warnings:    非阻断的告警（如迁移冲突在分析视图中降级为告警）。
    """

    machine_id: str
    initial: str
    states: Mapping[str, CompiledState]
    source_file: str
    warnings: tuple[Issue, ...] = ()


# ---------------------------------------------------------------------------
# 编译实现（返回诊断列表，供编译入口与静态分析共用）
# ---------------------------------------------------------------------------


def _ptr(*parts: object) -> str:
    """拼装 JSON Pointer；已带前导 ``/`` 的部分原样拼接（可嵌套调用）。"""
    out = ""
    for part in parts:
        text = str(part)
        out += text if text.startswith("/") else f"/{text}"
    return out


def _norm_src(src: str) -> str:
    """压缩空白后的条件原文（用于重复条件检测）。"""
    return " ".join(src.split())


def _is_true_literal(ast: ExprAST) -> bool:
    """AST 是否为恒真的布尔字面量。"""
    return isinstance(ast, BoolLit) and ast.value


def _provably_conflicting(a: CompiledTransition, b: CompiledTransition) -> bool:
    """静态可判定"恒可同时为真"的两个迁移（保守：只在可证明时报）。"""
    if _norm_src(a.when_src) == _norm_src(b.when_src):
        return True
    return _is_true_literal(a.ast) and _is_true_literal(b.ast)


def _compile_retry(
    act: ActionDecl, *, file: str, pointer: str
) -> tuple[RetrySpec | None, tuple[Issue, str] | None]:
    """把 ``retry`` 配置动作编译为 :class:`RetrySpec`；无上限 -> infinite_retry。"""
    params = act.params
    attempts = params.get("max_attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        return None, (
            Issue(
                file=file,
                pointer=pointer,
                rule="infinite_retry",
                message=f"retry 动作缺少有界的正整数 max_attempts（得到 {attempts!r}），会造成无限重试",
                hint="为 retry 增加 max_attempts，例如 max_attempts: 3",
            ),
            SEVERITY_ERROR,
        )
    backoff = params.get("backoff", BACKOFF_FIXED)
    if backoff not in (BACKOFF_FIXED, BACKOFF_EXPONENTIAL):
        return None, (
            Issue(
                file=file,
                pointer=pointer,
                rule="retry_invalid",
                message=f"retry.backoff 只允许 {BACKOFF_FIXED} 或 {BACKOFF_EXPONENTIAL}，得到 {backoff!r}",
            ),
            SEVERITY_ERROR,
        )
    base_ms = params.get("base_ms", 100.0)
    max_ms = params.get("max_ms", 30000.0)
    for name, value in (("base_ms", base_ms), ("max_ms", max_ms)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            return None, (
                Issue(
                    file=file,
                    pointer=pointer,
                    rule="retry_invalid",
                    message=f"retry.{name} 必须是正数，得到 {value!r}",
                ),
                SEVERITY_ERROR,
            )
    spec = RetrySpec(
        max_attempts=attempts,
        backoff=str(backoff),
        base_seconds=float(base_ms) / 1000.0,
        max_seconds=float(max_ms) / 1000.0,
    )
    return spec, None


def _compile_assertion(
    act: ActionDecl, *, file: str, pointer: str, states: dict[str, StateDef]
) -> tuple[CompiledAssertion | None, tuple[Issue, str] | None]:
    """把 assert_after / wait_until 动作编译为 :class:`CompiledAssertion`。"""
    params = act.params
    when = params.get("when")
    if not isinstance(when, str) or not when.strip():
        return None, (
            Issue(file=file, pointer=pointer, rule="assertion_invalid",
                  message=f"断言动作 {act.kind!r} 缺少 when 条件"),
            SEVERITY_ERROR,
        )
    try:
        ast = parse_expression(when)
    except ParseError as exc:
        return None, (
            Issue(file=file, pointer=pointer, rule="assertion_invalid",
                  message=f"断言条件无法解析：{exc}",
                  hint=f"错误位于表达式第 {exc.line} 行第 {exc.col} 列"),
            SEVERITY_ERROR,
        )
    raw_timeout: object
    scale = 1.0
    if "timeout_seconds" in params:
        raw_timeout = params.get("timeout_seconds")
    elif "timeout_ms" in params:
        raw_timeout = params.get("timeout_ms")
        scale = 1000.0
    else:
        raw_timeout = None
    try:
        timeout_seconds = parse_duration_seconds(raw_timeout) / scale
    except ValueError as exc:
        return None, (
            Issue(file=file, pointer=pointer, rule="assertion_invalid",
                  message=f"断言超时配置不合法：{exc}",
                  hint="timeout_seconds 用数字或带单位字面量（如 30s），或用 timeout_ms"),
            SEVERITY_ERROR,
        )
    target = params.get("to")
    if not isinstance(target, str) or target not in states:
        return None, (
            Issue(file=file, pointer=pointer, rule="state_target_missing",
                  message=f"断言失败出口状态 {target!r} 未在 states 中定义",
                  hint="把 to 改为已定义的状态名，或补充该状态"),
            SEVERITY_ERROR,
        )
    message = params.get("message", "")
    return (
        CompiledAssertion(
            kind=act.kind,
            ast=ast,
            when_src=when,
            timeout_seconds=timeout_seconds,
            failure_state=target,
            message=message if isinstance(message, str) else str(message),
            pointer=pointer,
        ),
        None,
    )


def compile_with_diagnostics(
    defn: StateMachineDef,
    *,
    hints: Mapping[str, Mapping[int, Mapping[str, Any]]] | None = None,
    file: str | None = None,
    known_fields: set[str] | None = None,
) -> tuple[CompiledMachine | None, list[tuple[Issue, str]]]:
    """编译 :class:`StateMachineDef`，返回 (产物或 None, [(Issue, severity)])。

    结构检查（severity=error，阻断编译）：
    - 初始状态存在（由 StateMachineDef 不变式保证，此处复核）；
    - 迁移目标 / 超时目标 / 断言出口 / on_error_to 目标存在；
    - 所有 ``when`` 与断言条件可被白名单解析器解析（带表达式内行列）；
    - 非终态必须有迁移或 timeout_seconds（``state_no_exit``，AC-P0-10）；
    - 所有状态从 initial 可达（``state_unreachable``）；
    - 同状态恒可同时为真的迁移冲突（``transition_conflict``）；
    - 动作 kind 必须在能力表中（``action_unauthorized``）；
    - retry 必须有有限上限（``infinite_retry``）。
    ``known_fields`` 给出时，条件引用未定义感知字段将报
    ``reference_missing_field``（供项目级静态分析使用）。
    """
    file = file or defn.source_file
    hint_map = hints or {}
    diags: list[tuple[Issue, str]] = []
    states = defn.states
    compiled_states: dict[str, CompiledState] = {}

    for state_name in sorted(states):
        state = states[state_name]
        base = _ptr("states", state_name)

        # ---- 非终态必须有出口（AC-P0-10：无迁移/无超时/非终止 -> 编译失败并定位） ----
        if not state.terminal and not state.transitions and state.timeout_seconds is None:
            diags.append(
                (
                    Issue(
                        file=file,
                        pointer=base,
                        rule="state_no_exit",
                        message=f"非终态 {state_name!r} 没有任何迁移、超时迁移或 timeout_seconds，可能卡死",
                        hint="添加 transitions，或设置 timeout_seconds（配合 on_timeout_to），或标记 terminal: true",
                    ),
                    SEVERITY_ERROR,
                )
            )

        # ---- 迁移 ----
        transitions: list[CompiledTransition] = []
        state_hints = hint_map.get(state_name) or {}
        for idx, tr in enumerate(state.transitions):
            tr_base = _ptr(base, "transitions", idx)
            hint = state_hints.get(idx) or {}
            priority = hint.get("priority", idx)
            stable_frames = hint.get("stable_frames", 1)
            if not isinstance(priority, int) or isinstance(priority, bool):
                diags.append(
                    (
                        Issue(file=file, pointer=_ptr(tr_base, "priority"), rule="priority_invalid",
                              message=f"priority 必须是整数，得到 {priority!r}"),
                        SEVERITY_ERROR,
                    )
                )
                priority = idx
            if not isinstance(stable_frames, int) or isinstance(stable_frames, bool) or stable_frames < 1:
                diags.append(
                    (
                        Issue(file=file, pointer=_ptr(tr_base, "stable_frames"), rule="stable_frames_invalid",
                              message=f"stable_frames 必须是 >=1 的整数，得到 {stable_frames!r}"),
                        SEVERITY_ERROR,
                    )
                )
                stable_frames = 1
            try:
                ast = parse_expression(tr.when)
            except ParseError as exc:
                diags.append(
                    (
                        Issue(file=file, pointer=_ptr(tr_base, "when"), rule="condition_parse_error",
                              message=f"条件表达式无法解析：{exc}",
                              hint=f"错误位于表达式第 {exc.line} 行第 {exc.col} 列"),
                        SEVERITY_ERROR,
                    )
                )
                continue
            if tr.to not in states:
                diags.append(
                    (
                        Issue(file=file, pointer=_ptr(tr_base, "to"), rule="state_target_missing",
                              message=f"迁移目标状态 {tr.to!r} 未在 states 中定义",
                              hint="修正 to 为已定义状态名，或补充该状态"),
                        SEVERITY_ERROR,
                    )
                )
            if tr.on_timeout_to is not None and tr.on_timeout_to not in states:
                diags.append(
                    (
                        Issue(file=file, pointer=_ptr(tr_base, "on_timeout_to"), rule="state_target_missing",
                              message=f"超时迁移目标状态 {tr.on_timeout_to!r} 未在 states 中定义",
                              hint="修正 on_timeout_to 为已定义状态名，或补充该状态"),
                        SEVERITY_ERROR,
                    )
                )
            if known_fields is not None:
                for ref in collect_field_refs(ast):
                    if ref not in known_fields:
                        diags.append(
                            (
                                Issue(file=file, pointer=_ptr(tr_base, "when"), rule="reference_missing_field",
                                      message=f"条件 {tr.when!r} 引用了未定义的感知字段 {ref!r}",
                                      hint=f"已定义的感知字段：{sorted(known_fields)}"),
                                SEVERITY_ERROR,
                            )
                        )
            transitions.append(
                CompiledTransition(
                    source_state=state_name,
                    decl_index=idx,
                    when_src=tr.when,
                    ast=ast,
                    target=tr.to,
                    on_timeout_to=tr.on_timeout_to,
                    priority=priority,
                    stable_frames=stable_frames,
                    pointer=tr_base,
                )
            )
        transitions.sort(key=lambda t: t.sort_key)

        # ---- 迁移冲突（静态可证明恒可同时真：编译期报错，分析视图降级为告警） ----
        for i in range(len(transitions)):
            for j in range(i + 1, len(transitions)):
                a, b = transitions[i], transitions[j]
                if _provably_conflicting(a, b):
                    diags.append(
                        (
                            Issue(file=file, pointer=b.pointer, rule="transition_conflict",
                                  message=(f"状态 {state_name!r} 的迁移 {b.when_src!r} 与迁移 {a.when_src!r} "
                                           f"恒可同时为真，后者永远被前者遮蔽"),
                                  hint="合并条件、删除冗余迁移，或用 priority 明确优先级"),
                            SEVERITY_ERROR,
                        )
                    )

        # ---- 动作分类 ----
        entry_steps: list[CompiledAction | CompiledAssertion] = []
        exit_steps: list[CompiledAction | CompiledAssertion] = []
        retry_spec: RetrySpec | None = None
        error_target: str | None = None
        for role, actions, steps in (("entry", state.entry, entry_steps), ("exit", state.exit, exit_steps)):
            for a_idx, act in enumerate(actions):
                act_base = _ptr(base, role, a_idx)
                if act.kind == "retry":
                    if retry_spec is None:
                        retry_spec, issue = _compile_retry(act, file=file, pointer=act_base)
                        if issue is not None:
                            diags.append(issue)
                    continue
                if act.kind == "on_error_to":
                    if error_target is None:
                        target = act.params.get("to")
                        if not isinstance(target, str) or target not in states:
                            diags.append(
                                (
                                    Issue(file=file, pointer=act_base, rule="state_target_missing",
                                          message=f"on_error_to 的异常出口状态 {target!r} 未在 states 中定义",
                                          hint="把 to 改为已定义状态名，或补充该状态"),
                                    SEVERITY_ERROR,
                                )
                            )
                        else:
                            error_target = target
                    continue
                if act.kind in ASSERTION_ACTION_KINDS:
                    assertion, issue = _compile_assertion(act, file=file, pointer=act_base, states=states)
                    if issue is not None:
                        diags.append(issue)
                        continue
                    steps.append(assertion)
                    continue
                if act.kind not in ACTION_KIND_CAPABILITY:
                    diags.append(
                        (
                            Issue(file=file, pointer=act_base, rule="action_unauthorized",
                                  message=f"未注册的动作类型 {act.kind!r}（不在能力表中）",
                                  hint=f"允许的动作类型：{sorted(ACTION_KIND_CAPABILITY)}"),
                            SEVERITY_ERROR,
                        )
                    )
                    continue
                capability = ACTION_KIND_CAPABILITY[act.kind]
                if capability is not None and capability not in CAPABILITY_REGISTRY:
                    diags.append(
                        (
                            Issue(file=file, pointer=act_base, rule="action_unauthorized",
                                  message=f"动作 {act.kind!r} 需要的能力 {capability!r} 未注册"),
                            SEVERITY_ERROR,
                        )
                    )
                    continue
                steps.append(CompiledAction(kind=act.kind, params=dict(act.params), pointer=act_base))

        timeout_transition = next((t for t in transitions if t.on_timeout_to is not None), None)
        compiled_states[state_name] = CompiledState(
            name=state_name,
            transitions=tuple(transitions),
            entry=tuple(entry_steps),
            exit=tuple(exit_steps),
            timeout_seconds=state.timeout_seconds,
            terminal=state.terminal,
            retry_spec=retry_spec,
            error_target=error_target,
            timeout_transition=timeout_transition,
            pointer=base,
        )

    # ---- 可达性：从 initial 出发，沿 to / on_timeout_to / on_error_to / 断言出口边 ----
    reachable = {defn.initial}
    frontier = [defn.initial]
    while frontier:
        current = frontier.pop()
        compiled = compiled_states.get(current)
        if compiled is None:
            continue
        for tr in compiled.transitions:
            for target in (tr.target, tr.on_timeout_to):
                if target is not None and target in states and target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
        edges = [compiled.error_target]
        edges.extend(
            step.failure_state
            for steps in (compiled.entry, compiled.exit)
            for step in steps
            if isinstance(step, CompiledAssertion)
        )
        for target in edges:
            if target is not None and target in states and target not in reachable:
                reachable.add(target)
                frontier.append(target)
    for state_name in sorted(states):
        if state_name not in reachable:
            diags.append(
                (
                    Issue(file=file, pointer=_ptr("states", state_name), rule="state_unreachable",
                          message=f"状态 {state_name!r} 从初始状态 {defn.initial!r} 不可达",
                          hint="补一条从可达状态出发的迁移，或删除该状态"),
                    SEVERITY_ERROR,
                )
            )

    errors = [issue for issue, severity in diags if severity == SEVERITY_ERROR]
    warnings = tuple(issue for issue, severity in diags if severity == SEVERITY_WARNING)
    if errors:
        return None, diags
    machine = CompiledMachine(
        machine_id=defn.machine_id,
        initial=defn.initial,
        states=compiled_states,
        source_file=file,
        warnings=warnings,
    )
    return machine, diags


def compile_state_machine(
    defn: StateMachineDef,
    *,
    known_fields: set[str] | None = None,
    file: str | None = None,
    priorities: Mapping[str, Mapping[int, int]] | None = None,
    stable_frames: Mapping[str, Mapping[int, int]] | None = None,
) -> CompiledMachine:
    """从 :class:`StateMachineDef` 编译；失败抛 :class:`DomainValidationError`。

    ``priorities`` / ``stable_frames`` 可选提供 每状态 -> 声明下标 -> 值 的映射
    （StateMachineDef 模型本身不携带这两个字段，来自原始 YAML 时由
    :func:`compile_machine_dict` 自动提取）。
    """
    hints: dict[str, dict[int, dict[str, int]]] = {}
    if priorities:
        for name, per in priorities.items():
            hints.setdefault(name, {}).update({idx: {"priority": v} for idx, v in per.items()})
    if stable_frames:
        for name, per in stable_frames.items():
            for idx, v in per.items():
                hints.setdefault(name, {}).setdefault(idx, {}).update({"stable_frames": v})
    machine, diags = compile_with_diagnostics(
        defn, hints=hints, file=file or defn.source_file, known_fields=known_fields
    )
    if machine is None:
        raise DomainValidationError([issue for issue, _ in diags])
    return machine


def _normalize_raw_machine(data: object) -> dict[str, Any]:
    """浅拷贝原始字典并把字符串 timeout_seconds 归一为浮点秒。"""
    if not isinstance(data, dict):
        raise DomainValidationError(
            [Issue(file="<machine>", pointer="", rule="invalid_root", message="状态机定义顶层必须是键值映射")]
        )
    data = dict(data)
    states = data.get("states")
    if isinstance(states, dict):
        new_states: dict[str, Any] = {}
        for name, body in states.items():
            if not isinstance(body, dict):
                new_states[name] = body
                continue
            body = dict(body)
            if isinstance(body.get("timeout_seconds"), str):
                try:
                    body["timeout_seconds"] = parse_duration_seconds(body["timeout_seconds"])
                except ValueError:
                    pass  # 留给 StateDef 不变式报带指针的错误
            transitions = body.get("transitions")
            if isinstance(transitions, list):
                body["transitions"] = [dict(tr) if isinstance(tr, dict) else tr for tr in transitions]
            new_states[name] = body
        data["states"] = new_states
    return data


def _extract_hints(data: dict[str, Any]) -> dict[str, dict[int, dict[str, int]]]:
    """从原始字典提取迁移的 priority / stable_frames（模型层不携带）。"""
    hints: dict[str, dict[int, dict[str, int]]] = {}
    states = data.get("states")
    if not isinstance(states, dict):
        return hints
    for name, body in states.items():
        if not isinstance(body, dict):
            continue
        transitions = body.get("transitions")
        if not isinstance(transitions, list):
            continue
        per: dict[int, dict[str, int]] = {}
        for idx, tr in enumerate(transitions):
            if isinstance(tr, dict) and ("priority" in tr or "stable_frames" in tr):
                per[idx] = {
                    "priority": tr.get("priority", idx),
                    "stable_frames": tr.get("stable_frames", 1),
                }
        if per:
            hints[name] = per
    return hints


def compile_machine_dict(
    data: dict[str, Any],
    *,
    file: str = "<machine>",
    known_fields: set[str] | None = None,
    project_dir: str | Path | None = None,
) -> CompiledMachine:
    """把 YAML/JSON 字典编译为 :class:`CompiledMachine`。

    - 校验 initial 存在、迁移目标存在、``when`` 可解析（错误带源位置）；
    - ``known_fields`` 给出时额外检查感知字段引用；
    - ``project_dir`` 给出时先运行项目级静态分析（DOM-007），
      任何 error 级问题都会阻断编译（AC-P0-10 的项目侧入口）。
    """
    if project_dir is not None:
        # 延迟导入避免 dsl <-> static_analysis 循环依赖
        from domain_model.static_analysis import SEVERITY_ERROR as _ERR
        from domain_model.static_analysis import analyze

        blocking = [i for i in analyze(project_dir) if i.severity == _ERR]
        if blocking:
            raise DomainValidationError(
                [
                    Issue(file=i.file, pointer=i.pointer, rule=i.rule, message=i.message, hint=i.hint)
                    for i in blocking
                ]
            )
    normalized = _normalize_raw_machine(data)
    defn = parse_machine(normalized, file=file)
    machine, diags = compile_with_diagnostics(
        defn, hints=_extract_hints(normalized), file=file, known_fields=known_fields
    )
    if machine is None:
        raise DomainValidationError([issue for issue, _ in diags])
    return machine


def compile_machine_yaml(
    path: str | Path,
    *,
    known_fields: set[str] | None = None,
    project_dir: str | Path | None = None,
) -> CompiledMachine:
    """读取并编译一个 machines/*.yaml 文件；错误携带文件名与指针。"""
    p = Path(path)
    data = load_config_file(p)
    return compile_machine_dict(
        data, file=p.name, known_fields=known_fields, project_dir=project_dir
    )
