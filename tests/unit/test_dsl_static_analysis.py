"""DOM-006 安全条件表达式 DSL 与 DOM-007 静态分析单测。

覆盖：
- 白名单表达式的解析与求值（逻辑/比较/算术/优先级/括号/字段语义）；
- 字段缺失的安全默认与 missing_fields 记录；
- 一切白名单外语法（函数调用/import/dunder/字符串/赋值/链式比较）一律
  ParseError；纯 AST 解释、全仓无 eval/exec 途径；
- compile_machine_* 的结构检查（initial/目标/条件/无出口状态/冲突）；
- analyze 的全部规则与 AC-P0-10（无出口状态 -> 编译失败且定位到状态）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from domain_model import (
    DomainValidationError,
    FieldObservation,
    PerceptionSnapshot,
    analyze,
    compile_machine_dict,
    compile_machine_yaml,
    ensure_compilable,
    parse_duration_seconds,
    parse_expression,
    ExprEval,
)
from domain_model.dsl import BinOp, FieldRef, ParseError
from domain_model.static_analysis import SEVERITY_ERROR, SEVERITY_WARNING

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "arena_lab_demo"
PROTECTED = REPO / "examples" / "protected_online_demo"


# ---------------------------------------------------------------------------
# 夹具辅助
# ---------------------------------------------------------------------------


def snap(fields: dict[str, tuple] | None = None, *, frame: int = 0, ts: float = 0.0) -> PerceptionSnapshot:
    """构造感知快照：字段值元组为 (present, value[, confidence])。"""
    values = {
        name: FieldObservation(
            name=name,
            present=spec[0],
            value=spec[1],
            confidence=spec[2] if len(spec) > 2 else 0.9,
        )
        for name, spec in (fields or {}).items()
    }
    return PerceptionSnapshot(frame_seq=frame, ts_monotonic=ts, values=values)


def ev(src: str, fields: dict[str, tuple] | None = None) -> bool | float:
    """解析并求值便捷函数。"""
    return ExprEval().evaluate(parse_expression(src), snap(fields))


def rules_of(issues) -> set[str]:
    return {i.rule for i in issues}


def errors_of(issues) -> list:
    return [i for i in issues if i.severity == SEVERITY_ERROR]


def write_project(root: Path, *, machines: dict | None = None, detectors: list[dict] | None = None,
                  targets: list[dict] | None = None, policies: list[dict] | None = None) -> Path:
    """在临时目录装配一个最小项目。"""
    (root / "project.yaml").write_text(
        "schema_version: 1\nname: t\ndescription: 静态分析测试项目\n", encoding="utf-8"
    )
    if machines:
        (root / "machines").mkdir(parents=True, exist_ok=True)
        for name, data in machines.items():
            (root / "machines" / f"{name}.machine.yaml").write_text(
                yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
            )
    if detectors:
        (root / "detectors").mkdir(parents=True, exist_ok=True)
        for idx, data in enumerate(detectors):
            (root / "detectors" / f"d{idx}.yaml").write_text(
                yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
            )
    if targets:
        (root / "targets").mkdir(parents=True, exist_ok=True)
        for idx, data in enumerate(targets):
            (root / "targets" / f"t{idx}.yaml").write_text(
                yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
            )
    if policies:
        (root / "policies").mkdir(parents=True, exist_ok=True)
        for idx, data in enumerate(policies):
            (root / "policies" / f"p{idx}.yaml").write_text(
                yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
            )
    return root


def minimal_machine(**overrides) -> dict:
    """结构合法的最小状态机（a --go.present--> b，b 终态）。"""
    data: dict = {
        "schema_version": 1,
        "machine_id": "m1",
        "initial": "a",
        "states": {
            "a": {"transitions": [{"when": "go.present", "to": "b"}]},
            "b": {"terminal": True},
        },
    }
    data.update(overrides)
    return data


BASE_POLICY = {
    "schema_version": 1,
    "policy_id": "default",
    "mode": "shadow",
    "require_manual_start": True,
    "max_runtime_minutes": 10,
    "max_actions_per_minute": 60,
    "unattended_schedule": "disabled",
}


# ---------------------------------------------------------------------------
# DSL：解析与求值
# ---------------------------------------------------------------------------


class TestExpressionEval:
    def test_basic_comparison(self) -> None:
        """比较运算与字段引用求值。"""
        assert ev("1 < 2") is True
        assert ev("2 <= 2") is True
        assert ev("3 > 4") is False
        assert ev("5 >= 6") is False
        assert ev("health_ratio.value < 0.2", {"health_ratio": (True, 0.1)}) is True
        assert ev("health_ratio.value < 0.2", {"health_ratio": (True, 0.9)}) is False

    def test_logic_operators(self) -> None:
        """and / or / not 组合。"""
        fields = {"a": (True, None), "b": (False, None)}
        assert ev("a.present and not b.present", fields) is True
        assert ev("a.present and b.present", fields) is False
        assert ev("a.present or b.present", fields) is True
        assert ev("not a.present or b.present", fields) is False

    def test_precedence_and_parentheses(self) -> None:
        """优先级（乘除 > 加减 > 比较 > not > and > or）与括号覆盖。"""
        assert ev("1 + 2 * 3 == 7") is True
        assert ev("(1 + 2) * 3 == 9") is True
        assert ev("10 - 4 / 2 == 8") is True
        assert ev("not 1 > 2 and 3 > 2") is True
        ast = parse_expression("a.value + 1 < 2")
        assert isinstance(ast, BinOp) and ast.op == "<"
        assert isinstance(ast.left, BinOp) and ast.left.op == "+"

    def test_arithmetic_and_unary_minus(self) -> None:
        """算术与一元负号返回数值。"""
        assert ev("10 / 4") == 2.5
        assert ev("-3 + 1") == -2.0
        assert ev("-go.value + 1", {"go": (True, 4)}) == -3.0
        result = ev("go.value * 2 + 1", {"go": (True, 4)})
        assert result == 9.0

    def test_division_by_zero_safe_default(self) -> None:
        """除零安全归零（求值总量性：绝不抛异常）。"""
        assert ev("1 / 0") == 0.0
        assert ev("1 / (go.value - go.value)", {"go": (True, 5)}) == 0.0

    def test_field_attribute_semantics(self) -> None:
        """present / value / confidence / changed 的观测语义。"""
        fields = {"f": (True, 0.7, 0.95)}
        assert ev("f.present", fields) is True
        assert ev("f.value == 0.7", fields) is True
        assert ev("f.confidence > 0.9", fields) is True
        # changed 由聚合层产出，当前安全默认恒 False
        assert ev("f.changed", fields) is False

    def test_bare_field_means_present(self) -> None:
        """裸字段引用按 present 语义求值。"""
        assert ev("ready", {"ready": (True, None)}) is True
        assert ev("ready", {"ready": (False, None)}) is False

    def test_missing_field_safe_defaults_and_recording(self) -> None:
        """字段缺失：安全默认 + missing_fields 记录（or 两支都求值）。"""
        evaluator = ExprEval()
        result = evaluator.evaluate(
            parse_expression("ghost.present or go.value > 1"), snap()
        )
        assert result is False
        assert set(evaluator.missing_fields) == {"ghost.present", "go.value"}
        # 缺失 value/confidence 的默认是 0.0
        assert ev("go.value == 0") is True
        assert ev("go.confidence == 0") is True
        # 未缺失时不记录
        evaluator2 = ExprEval()
        evaluator2.evaluate(parse_expression("go.present"), snap({"go": (True, None)}))
        assert evaluator2.missing_fields == ()

    def test_present_but_absent_value_counts_missing(self) -> None:
        """字段本帧未观测到（present=False）时 .value 按缺失处理。"""
        evaluator = ExprEval()
        assert evaluator.evaluate(parse_expression("go.value"), snap({"go": (False, 9)})) == 0.0
        assert evaluator.missing_fields == ("go.value",)

    def test_equality_bool_vs_number(self) -> None:
        """==/!= 的布尔与数值语义：布尔不与数值相等（避免 True==1）。"""
        assert ev("ready.present == true", {"ready": (True, None)}) is True
        assert ev("ready.present != false", {"ready": (True, None)}) is True
        assert ev("ready.present == 1", {"ready": (True, None)}) is False
        assert ev("ready.present != 1", {"ready": (True, None)}) is True
        assert ev("1 == 1.0") is True

    def test_ast_keeps_source_position(self) -> None:
        """AST 节点保留源位置。"""
        ast = parse_expression("health_ratio.value < 0.2")
        assert isinstance(ast, BinOp)
        assert ast.line == 1 and ast.col == 20
        assert isinstance(ast.left, FieldRef) and ast.left.name == "health_ratio"
        assert ast.left.col == 1


# ---------------------------------------------------------------------------
# DSL：白名单之外的一切语法必须 ParseError
# ---------------------------------------------------------------------------


class TestParseRejected:
    def test_function_call_rejected(self) -> None:
        from domain_model.dsl import ParseError

        for src in ("ready(1)", "ready.present(1)", "eval(x)", "__import__(x)", "exec(x)"):
            with pytest.raises(ParseError) as excinfo:
                parse_expression(src)
            assert "函数调用" in excinfo.value.message

    def test_deep_attribute_chain_rejected(self) -> None:
        from domain_model.dsl import ParseError

        # 第二个点在 col 10：a.present.c
        with pytest.raises(ParseError) as excinfo:
            parse_expression("a.present.c")
        assert "属性链" in excinfo.value.message
        assert excinfo.value.col == 10

    def test_dunder_attribute_rejected(self) -> None:
        from domain_model.dsl import ParseError

        with pytest.raises(ParseError) as excinfo:
            parse_expression("a.__class__")
        assert "不允许的感知属性" in excinfo.value.message

    def test_string_literal_rejected(self) -> None:
        from domain_model.dsl import ParseError

        for src in ("'x'", '"abc"', "a.value + 'x'", "__import__('os')", "eval('1')",
                    "exec('1+1')"):
            with pytest.raises(ParseError) as excinfo:
                parse_expression(src)
            assert "字符串" in excinfo.value.message

    def test_import_keyword_rejected(self) -> None:
        from domain_model.dsl import ParseError

        # import 不是字段引用能消化掉的语法：多余内容 / 非法记号
        with pytest.raises(ParseError):
            parse_expression("import os")

    def test_assignment_rejected(self) -> None:
        from domain_model.dsl import ParseError

        with pytest.raises(ParseError):
            parse_expression("x = 1")

    def test_chained_comparison_rejected(self) -> None:
        from domain_model.dsl import ParseError

        with pytest.raises(ParseError) as excinfo:
            parse_expression("1 < 2 < 3")
        assert "链式比较" in excinfo.value.message

    def test_trailing_tokens_rejected(self) -> None:
        from domain_model.dsl import ParseError

        with pytest.raises(ParseError):
            parse_expression("1 2")

    def test_empty_expression_rejected(self) -> None:
        from domain_model.dsl import ParseError

        with pytest.raises(ParseError):
            parse_expression("   ")

    def test_parse_error_carries_position(self) -> None:
        """ParseError 携带表达式内行列。"""
        with pytest.raises(ParseError) as excinfo:
            parse_expression("ready.present(\n")
        assert excinfo.value.line == 1 and excinfo.value.col == 14


# ---------------------------------------------------------------------------
# DSL：时长字面量（仅用于超时类字段）
# ---------------------------------------------------------------------------


class TestDuration:
    def test_units_parse(self) -> None:
        assert parse_duration_seconds("30s") == 30.0
        assert parse_duration_seconds("250ms") == 0.25
        assert parse_duration_seconds("2m") == 120.0
        assert parse_duration_seconds(45) == 45.0
        assert parse_duration_seconds(1.5) == 1.5

    def test_invalid_units_rejected(self) -> None:
        for bad in ("abc", "0", -1, "10x", True, None, ""):
            with pytest.raises(ValueError):
                parse_duration_seconds(bad)

    def test_timeout_string_normalized_in_compile(self) -> None:
        """字符串 timeout_seconds 在编译时归一为浮点秒。"""
        data = minimal_machine()
        data["states"]["a"]["timeout_seconds"] = "45s"
        data["states"]["a"]["transitions"][0]["on_timeout_to"] = "b"
        machine = compile_machine_dict(data)
        assert machine.states["a"].timeout_seconds == 45.0


# ---------------------------------------------------------------------------
# 编译（FSM-002）：结构检查与错误定位
# ---------------------------------------------------------------------------


class TestCompile:
    def test_compile_example_machine(self) -> None:
        """示例状态机编译：初始状态、迁移排序、超时迁移、闸门动作。"""
        machine = compile_machine_yaml(EXAMPLE / "machines" / "main.machine.yaml")
        assert machine.machine_id == "main"
        assert machine.initial == "idle"
        assert set(machine.states) == {"idle", "awaiting_manual_gate", "exercise", "stopped"}
        awaiting = machine.states["awaiting_manual_gate"]
        assert awaiting.timeout_seconds == 30.0
        assert awaiting.timeout_transition is awaiting.transitions[0]
        assert awaiting.transitions[0].target == "exercise"
        assert awaiting.transitions[0].on_timeout_to == "stopped"
        # manual_gate 是运行控制类动作（kind 保留在 entry 序列）
        assert awaiting.entry[0].kind == "manual_gate"
        assert machine.states["stopped"].terminal is True
        assert machine.warnings == ()

    def test_compile_initial_missing(self) -> None:
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict({"schema_version": 1, "machine_id": "m", "states": {"a": {"terminal": True}}})
        assert "missing_field" in rules_of(excinfo.value.issues)

    def test_compile_initial_not_defined(self) -> None:
        data = minimal_machine(initial="ghost")
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict(data)
        assert "initial_state_missing" in rules_of(excinfo.value.issues)

    def test_compile_target_missing_pointer(self) -> None:
        data = minimal_machine()
        data["states"]["a"]["transitions"][0]["to"] = "ghost"
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict(data)
        issue = next(i for i in excinfo.value.issues if i.rule == "state_target_missing")
        assert issue.pointer == "/states/a/transitions/0/to"

    def test_compile_when_parse_error_located(self) -> None:
        data = minimal_machine()
        data["states"]["a"]["transitions"][0]["when"] = "go.present("
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict(data)
        issue = next(i for i in excinfo.value.issues if i.rule == "condition_parse_error")
        assert issue.pointer == "/states/a/transitions/0/when"
        assert "第 1 行第 11 列" in issue.hint

    def test_compile_state_no_exit_blocked_and_located(self) -> None:
        """AC-P0-10：无迁移/无超时/非终止的状态 -> 编译失败且定位到该状态。"""
        data = minimal_machine()
        data["states"]["stuck"] = {}  # 非终态、无迁移、无超时
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict(data)
        issue = next(i for i in excinfo.value.issues if i.rule == "state_no_exit")
        assert issue.pointer == "/states/stuck"
        assert "stuck" in issue.message

    def test_compile_unreachable_state(self) -> None:
        data = minimal_machine()
        data["states"]["orphan"] = {"terminal": True}
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict(data)
        issue = next(i for i in excinfo.value.issues if i.rule == "state_unreachable")
        assert issue.pointer == "/states/orphan"

    def test_compile_conflicting_transitions_error(self) -> None:
        """同状态恒可同时为真的迁移 -> 编译报错（transition_conflict）。"""
        data = {
            "schema_version": 1,
            "machine_id": "conflict",
            "initial": "a",
            "states": {
                "a": {"transitions": [{"when": "true", "to": "b"}, {"when": "true", "to": "c"}]},
                "b": {"terminal": True},
                "c": {"terminal": True},
            },
        }
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict(data)
        issue = next(i for i in excinfo.value.issues if i.rule == "transition_conflict")
        assert issue.pointer == "/states/a/transitions/1"

    def test_compile_known_fields_check(self) -> None:
        """known_fields 给出时检查感知字段引用。"""
        data = minimal_machine()
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict(data, known_fields={"ready"})
        assert "reference_missing_field" in rules_of(excinfo.value.issues)
        # 引用已定义字段则通过
        compile_machine_dict(data, known_fields={"go"})

    def test_compile_unknown_action_kind_rejected(self) -> None:
        data = minimal_machine()
        data["states"]["a"]["entry"] = [{"kind": "exec_shell", "cmd": "rm"}]
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict(data)
        assert "action_unauthorized" in rules_of(excinfo.value.issues)

    def test_compile_infinite_retry_rejected(self) -> None:
        data = minimal_machine()
        data["states"]["a"]["entry"] = [{"kind": "retry"}]  # 缺 max_attempts
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict(data)
        assert "infinite_retry" in rules_of(excinfo.value.issues)

    def test_compile_on_error_to_target_missing(self) -> None:
        data = minimal_machine()
        data["states"]["a"]["entry"] = [{"kind": "on_error_to", "to": "ghost"}]
        with pytest.raises(DomainValidationError) as excinfo:
            compile_machine_dict(data)
        assert "state_target_missing" in rules_of(excinfo.value.issues)

    def test_compile_with_project_dir_gate(self, tmp_path: Path) -> None:
        """compile 前置调用 analyze：项目存在 error 级问题时阻断编译。"""
        compile_machine_yaml(EXAMPLE / "machines" / "main.machine.yaml", project_dir=EXAMPLE)
        broken = write_project(tmp_path, machines={"m1": minimal_machine(initial="ghost")})
        with pytest.raises(DomainValidationError):
            compile_machine_dict(minimal_machine(), file="m1.machine.yaml", project_dir=broken)


# ---------------------------------------------------------------------------
# 静态分析（DOM-007）
# ---------------------------------------------------------------------------


class TestStaticAnalysis:
    def test_arena_demo_zero_errors(self) -> None:
        """examples/arena_lab_demo -> 0 个 error 级问题。"""
        issues = analyze(EXAMPLE)
        assert errors_of(issues) == []

    def test_protected_demo_has_hard_lock(self) -> None:
        """examples/protected_online_demo -> 含硬锁规则且为 error。"""
        issues = analyze(PROTECTED)
        assert "protected_online_no_real_input" in rules_of(issues)
        assert any(
            i.rule == "protected_online_no_real_input" and i.severity == SEVERITY_ERROR
            for i in issues
        )

    def test_state_unreachable(self, tmp_path: Path) -> None:
        data = minimal_machine()
        data["states"]["orphan"] = {"terminal": True}  # 不可达的终态
        root = write_project(tmp_path, machines={"m1": data})
        issues = analyze(root)
        issue = next(i for i in issues if i.rule == "state_unreachable")
        assert issue.pointer == "/states/orphan"
        assert issue.severity == SEVERITY_ERROR

    def test_state_no_exit(self, tmp_path: Path) -> None:
        data = minimal_machine()
        data["states"]["stuck"] = {}
        root = write_project(tmp_path, machines={"m1": data})
        issues = analyze(root)
        issue = next(i for i in issues if i.rule == "state_no_exit")
        assert issue.pointer == "/states/stuck"

    def test_reference_missing_field(self, tmp_path: Path) -> None:
        """when 引用未定义感知字段（项目没有任何 detectors）。"""
        root = write_project(tmp_path, machines={"m1": minimal_machine()})
        issues = analyze(root)
        issue = next(i for i in issues if i.rule == "reference_missing_field")
        assert "go" in issue.message

    def test_reference_field_defined_ok(self, tmp_path: Path) -> None:
        """detectors 定义了字段 -> 无 reference_missing_field。"""
        detector = {
            "schema_version": 1, "detector_id": "d1", "type": "color_bar_ratio",
            "roi": [0.0, 0.0, 0.2, 0.1], "threshold": 0.5, "stable_frames": 1,
            "field_name": "go",
        }
        root = write_project(tmp_path, machines={"m1": minimal_machine()}, detectors=[detector])
        assert all(i.rule != "reference_missing_field" for i in analyze(root))

    def test_reference_missing_asset(self, tmp_path: Path) -> None:
        """模板文件不存在 -> reference_missing_asset。"""
        detector = {
            "schema_version": 1, "detector_id": "d1", "type": "template_match",
            "roi": [0.0, 0.0, 0.2, 0.1], "threshold": 0.5, "stable_frames": 1,
            "field_name": "go", "template": "assets/templates/missing.png",
        }
        root = write_project(tmp_path, detectors=[detector])
        issues = analyze(root)
        issue = next(i for i in issues if i.rule == "reference_missing_asset")
        assert issue.pointer == "/template"

    def test_action_unauthorized(self, tmp_path: Path) -> None:
        """动作类型不在注册能力表 -> action_unauthorized。"""
        data = minimal_machine()
        data["states"]["a"]["entry"] = [{"kind": "exec_shell", "cmd": "rm"}]
        root = write_project(tmp_path, machines={"m1": data})
        issues = analyze(root)
        issue = next(i for i in issues if i.rule == "action_unauthorized")
        assert "exec_shell" in issue.message

    def test_protected_online_unattended(self, tmp_path: Path) -> None:
        """受保护目标 + unattended enabled -> 硬锁。"""
        target = {
            "schema_version": 1, "target_id": "online", "executable": "Game.exe",
            "title_regex": "^Game", "protected_online": True,
            "allowed_display_modes": ["windowed"],
        }
        policy = dict(BASE_POLICY, unattended_schedule="enabled")
        root = write_project(tmp_path, targets=[target], policies=[policy])
        issues = analyze(root)
        rules = rules_of(issues)
        assert "protected_online_unattended" in rules
        # shadow 模式不触发 real_input 硬锁
        assert "protected_online_no_real_input" not in rules

    def test_real_input_no_unattended(self, tmp_path: Path) -> None:
        """非受保护目标：real_input + 无人值守 -> 默认拒绝。"""
        target = {
            "schema_version": 1, "target_id": "local", "executable": "App.exe",
            "title_regex": "^App", "protected_online": False,
            "allowed_display_modes": ["windowed"],
        }
        policy = dict(BASE_POLICY, mode="real_input", unattended_schedule="enabled")
        root = write_project(tmp_path, targets=[target], policies=[policy])
        assert "real_input_no_unattended" in rules_of(analyze(root))

    def test_infinite_retry(self, tmp_path: Path) -> None:
        """retry 缺少 max_attempts -> infinite_retry。"""
        data = minimal_machine()
        data["states"]["a"]["entry"] = [{"kind": "retry", "backoff": "exponential"}]
        root = write_project(tmp_path, machines={"m1": data})
        assert "infinite_retry" in rules_of(analyze(root))

    def test_transition_conflict_is_warning(self, tmp_path: Path) -> None:
        """迁移冲突在分析视图中降级为告警（不阻断 analyze）。"""
        detector = {
            "schema_version": 1, "detector_id": "d1", "type": "color_bar_ratio",
            "roi": [0.0, 0.0, 0.2, 0.1], "threshold": 0.5, "stable_frames": 1,
            "field_name": "go",
        }
        data = {
            "schema_version": 1, "machine_id": "m1", "initial": "a",
            "states": {
                "a": {"transitions": [{"when": "go.present", "to": "b"},
                                       {"when": "go.present", "to": "c"}]},
                "b": {"terminal": True},
                "c": {"terminal": True},
            },
        }
        root = write_project(tmp_path, machines={"m1": data}, detectors=[detector])
        issues = analyze(root)
        issue = next(i for i in issues if i.rule == "transition_conflict")
        assert issue.severity == SEVERITY_WARNING
        assert errors_of(issues) == []

    def test_structural_error_from_compile_surfaces(self, tmp_path: Path) -> None:
        """编译期诊断（目标缺失）经 analyze 原样呈现为 error。"""
        data = minimal_machine()
        data["states"]["a"]["transitions"][0]["to"] = "ghost"
        root = write_project(tmp_path, machines={"m1": data})
        assert "state_target_missing" in rules_of(analyze(root))

    def test_ensure_compilable_blocks_and_passes(self, tmp_path: Path) -> None:
        """ensure_compilable：error 级问题抛 DomainValidationError（AC-P0-10 门）。"""
        assert ensure_compilable(EXAMPLE) is not None  # 有效项目不抛
        data = minimal_machine()
        data["states"]["stuck"] = {}
        root = write_project(tmp_path, machines={"m1": data})
        with pytest.raises(DomainValidationError) as excinfo:
            ensure_compilable(root)
        assert "state_no_exit" in rules_of(excinfo.value.issues)

    def test_analyze_rejects_non_directory(self, tmp_path: Path) -> None:
        issues = analyze(tmp_path / "nope")
        assert issues[0].rule == "invalid_target"
        assert issues[0].severity == SEVERITY_ERROR

    def test_analysis_issue_is_issue_subclass(self) -> None:
        """AnalysisIssue 是 Issue 的子类（severity 附加字段）。"""
        from domain_model import Issue

        issues = analyze(EXAMPLE)
        assert all(isinstance(i, Issue) for i in issues)
