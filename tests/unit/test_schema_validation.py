"""Schema（DOM-002）与项目校验器（DOM-003）单测，含受保护目标硬锁（SAFE-020）。

反例项目通过在 tmp_path 中复制 examples/arena_lab_demo 并改动单个字段构造，
保证每次只触发目标规则。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import yaml

from domain_model import SCHEMA_KINDS, load_schema, validate_project_dir

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def copy_example(tmp_path: Path, name: str = "arena_lab_demo") -> Path:
    """把示例项目复制到 tmp_path，作为反例项目的底板。"""
    target = tmp_path / "project"
    shutil.copytree(EXAMPLES / name, target)
    return target


def rewrite_yaml(project: Path, rel: str, mutate: Callable[[dict], None]) -> None:
    """加载项目内的 YAML 文件 -> 应用变更 -> 写回。"""
    p = project / rel
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    mutate(data)
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def rules(result) -> set[str]:
    return {i.rule for i in result.issues}


def issues_with(result, rule: str) -> list:
    return [i for i in result.issues if i.rule == rule]


# ---------------------------------------------------------------------------
# Schema 自身（DOM-002）
# ---------------------------------------------------------------------------


def test_all_schemas_load_with_draft_and_id() -> None:
    assert set(SCHEMA_KINDS) == {"project", "target", "detector", "machine", "policy", "calibration"}
    for kind in SCHEMA_KINDS:
        schema = load_schema(kind)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"] == f"https://vaw.local/schemas/{kind}.schema.json"
        assert schema["properties"]["schema_version"]["const"] == 1


def test_schema_rejects_wrong_schema_version(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "targets/arena-lab.yaml", lambda d: d.__setitem__("schema_version", 2))
    result = validate_project_dir(project)
    assert "schema_invalid" in rules(result)
    assert not result.ok


def test_schema_rejects_missing_required_field(tmp_path: Path) -> None:
    project = copy_example(tmp_path)

    def remove_required(d: dict) -> None:
        d.pop("protected_online")

    rewrite_yaml(project, "targets/arena-lab.yaml", remove_required)
    result = validate_project_dir(project)
    assert "schema_invalid" in rules(result)
    # JSON Schema 的 required 错误定位在对象根，字段名出现在消息里
    assert any("protected_online" in i.message for i in issues_with(result, "schema_invalid"))


def test_schema_rejects_invalid_enum(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "policies/default.yaml", lambda d: d.__setitem__("mode", "god_mode"))
    result = validate_project_dir(project)
    assert "schema_invalid" in rules(result)


def test_schema_rejects_unknown_property(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "targets/arena-lab.yaml", lambda d: d.__setitem__("typo_field", 1))
    result = validate_project_dir(project)
    assert "schema_invalid" in rules(result)


def test_schema_detector_roi_shape(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    # 5 个元素 / 非数字元素 / 宽度为 0 均不通过 Schema
    rewrite_yaml(project, "detectors/ready_button.yaml",
                 lambda d: d.__setitem__("roi", [0.4, 0.78, 0.2, 0.12, 0.0]))
    assert "schema_invalid" in rules(validate_project_dir(project))

    project2 = copy_example(tmp_path / "second")
    rewrite_yaml(project2, "detectors/ready_button.yaml",
                 lambda d: d.__setitem__("roi", [0.4, 0.78, "wide", 0.12]))
    assert "schema_invalid" in rules(validate_project_dir(project2))


def test_schema_detector_template_match_requires_template(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "detectors/ready_button.yaml", lambda d: d.pop("template"))
    result = validate_project_dir(project)
    assert "schema_invalid" in rules(result)


def test_schema_machine_requires_when_and_to(tmp_path: Path) -> None:
    project = copy_example(tmp_path)

    def drop_to(d: dict) -> None:
        d["states"]["idle"]["transitions"][0].pop("to")

    rewrite_yaml(project, "machines/main.machine.yaml", drop_to)
    result = validate_project_dir(project)
    assert "schema_invalid" in rules(result)
    # 错误定位在迁移对象上，字段名出现在消息里
    assert any(i.pointer == "/states/idle/transitions/0" and "'to'" in i.message
               for i in issues_with(result, "schema_invalid"))


# ---------------------------------------------------------------------------
# 项目级校验（DOM-003）：有效示例
# ---------------------------------------------------------------------------


def test_arena_lab_demo_passes_validation() -> None:
    result = validate_project_dir(EXAMPLES / "arena_lab_demo")
    assert result.issues == []
    assert result.ok
    assert result.objects_checked == 10


def test_missing_project_dir_reported(tmp_path: Path) -> None:
    result = validate_project_dir(tmp_path / "no_such_dir")
    assert "project_dir_missing" in rules(result)
    assert not result.ok


# ---------------------------------------------------------------------------
# 受保护目标硬锁（SAFE-020）
# ---------------------------------------------------------------------------


def test_protected_online_no_real_input() -> None:
    result = validate_project_dir(EXAMPLES / "protected_online_demo")
    assert not result.ok
    locked = issues_with(result, "protected_online_no_real_input")
    assert len(locked) == 1
    assert locked[0].pointer == "/mode"
    assert locked[0].file == "policies/default.yaml"


def test_protected_online_no_unattended(tmp_path: Path) -> None:
    project = copy_example(tmp_path, "protected_online_demo")
    rewrite_yaml(project, "policies/default.yaml",
                 lambda d: d.__setitem__("unattended_schedule", "enabled"))
    result = validate_project_dir(project)
    locked = issues_with(result, "protected_online_no_unattended")
    assert len(locked) == 1
    assert locked[0].pointer == "/unattended_schedule"


def test_real_input_forbids_unattended_even_without_protected_target(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "policies/default.yaml",
                 lambda d: d.update({"mode": "real_input", "unattended_schedule": "enabled"}))
    result = validate_project_dir(project)
    assert "real_input_no_unattended" in rules(result)
    # 无受保护目标时不得误报两条硬锁
    assert "protected_online_no_real_input" not in rules(result)
    assert "protected_online_no_unattended" not in rules(result)


def test_shadow_mode_with_protected_target_allowed(tmp_path: Path) -> None:
    """受保护目标 + shadow 模式 + unattended disabled 是合法组合。"""
    project = copy_example(tmp_path, "protected_online_demo")
    rewrite_yaml(project, "policies/default.yaml", lambda d: d.__setitem__("mode", "shadow"))
    result = validate_project_dir(project)
    assert result.ok


# ---------------------------------------------------------------------------
# 资产引用检查
# ---------------------------------------------------------------------------


def test_template_file_missing(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "detectors/ready_button.yaml",
                 lambda d: d.__setitem__("template", "assets/templates/missing.png"))
    result = validate_project_dir(project)
    found = issues_with(result, "template_file_missing")
    assert len(found) == 1
    assert found[0].pointer == "/template"
    assert found[0].file == "detectors/ready_button.yaml"


def test_template_not_in_manifest(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "assets/assets.yaml",
                 lambda d: d.__setitem__("assets", [a for a in d["assets"] if a["asset_id"] != "asset-ready-button"]))
    result = validate_project_dir(project)
    assert "template_not_in_manifest" in rules(result)


def test_template_hash_mismatch(tmp_path: Path) -> None:
    project = copy_example(tmp_path)

    def corrupt(d: dict) -> None:
        for asset in d["assets"]:
            if asset["asset_id"] == "asset-ready-button":
                asset["sha256"] = "0" * 64

    rewrite_yaml(project, "assets/assets.yaml", corrupt)
    result = validate_project_dir(project)
    found = issues_with(result, "template_hash_mismatch")
    assert len(found) == 1
    assert "/sha256" in found[0].pointer


# ---------------------------------------------------------------------------
# 状态机与感知字段交叉检查
# ---------------------------------------------------------------------------


def test_when_references_undefined_perception_field(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "machines/main.machine.yaml",
                 lambda d: d["states"]["idle"]["transitions"][0].__setitem__("when", "ghost.present"))
    result = validate_project_dir(project)
    found = issues_with(result, "perception_field_undefined")
    assert len(found) == 1
    assert found[0].pointer == "/states/idle/transitions/0/when"


def test_when_references_unknown_attribute(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "machines/main.machine.yaml",
                 lambda d: d["states"]["idle"]["transitions"][0].__setitem__("when", "ready.colour"))
    result = validate_project_dir(project)
    assert "perception_attribute_unknown" in rules(result)


def test_state_target_missing(tmp_path: Path) -> None:
    project = copy_example(tmp_path)

    def break_target(d: dict) -> None:
        d["states"]["exercise"]["transitions"][0]["to"] = "nowhere"
        d["states"]["awaiting_manual_gate"]["transitions"][0]["on_timeout_to"] = "limbo"

    rewrite_yaml(project, "machines/main.machine.yaml", break_target)
    result = validate_project_dir(project)
    found = issues_with(result, "state_target_missing")
    assert {i.pointer for i in found} == {
        "/states/exercise/transitions/0/to",
        "/states/awaiting_manual_gate/transitions/0/on_timeout_to",
    }


def test_state_unreachable(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "machines/main.machine.yaml",
                 lambda d: d["states"].__setitem__("orphan", {"transitions": []}))
    result = validate_project_dir(project)
    found = issues_with(result, "state_unreachable")
    assert [i.pointer for i in found] == ["/states/orphan"]


def test_state_no_exit(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "machines/main.machine.yaml",
                 lambda d: d["states"]["exercise"].__setitem__("transitions", []))
    result = validate_project_dir(project)
    found = issues_with(result, "state_no_exit")
    assert [i.pointer for i in found] == ["/states/exercise"]


def test_terminal_state_is_allowed_to_have_no_exit(tmp_path: Path) -> None:
    """stopped 本来就是无迁移的终态，不得误报 state_no_exit。"""
    result = validate_project_dir(EXAMPLES / "arena_lab_demo")
    assert "state_no_exit" not in rules(result)


# ---------------------------------------------------------------------------
# CLI（python -m domain_model.validate）
# ---------------------------------------------------------------------------


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "packages") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "domain_model.validate", *args],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        env=_cli_env(), timeout=120,
    )


def test_cli_valid_project_exits_zero() -> None:
    proc = _run_cli("examples/arena_lab_demo")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("VALID: 10 objects checked")


def test_cli_protected_demo_exits_one_with_rule_id() -> None:
    proc = _run_cli("examples/protected_online_demo")
    assert proc.returncode == 1
    assert "protected_online_no_real_input" in proc.stdout
    assert "INVALID" in proc.stdout


def test_cli_json_lines_output(tmp_path: Path) -> None:
    proc = _run_cli("examples/protected_online_demo", "--json")
    assert proc.returncode == 1
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert lines, proc.stdout
    payload = json.loads(lines[0])
    assert payload["rule"] == "protected_online_no_real_input"
    assert payload["pointer"] == "/mode"
    assert payload["file"].endswith("default.yaml")
    assert "hint" in payload


def test_cli_reports_each_issue_with_file_pointer_and_hint(tmp_path: Path) -> None:
    project = copy_example(tmp_path)
    rewrite_yaml(project, "policies/default.yaml",
                 lambda d: d.update({"mode": "real_input", "unattended_schedule": "enabled"}))
    proc = _run_cli(str(project))
    assert proc.returncode == 1
    out = proc.stdout
    assert "policies" in out and "/unattended_schedule" in out and "real_input_no_unattended" in out
    assert "提示" in out  # 修复提示必须出现
