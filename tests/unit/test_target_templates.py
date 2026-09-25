"""目标适配模板单测（ADP-001/002/003，M5）。

覆盖：
- desktop-target-template：validate 通过、policy 保守默认、目标非受保护、
  检测器示例（color_region + template_match）、README 准入清单五项；
- protected-online-shadow-template：validate 通过且为 shadow、
  篡改 mode→real_input 必拒（protected_online_no_real_input）、
  篡改 unattended→enabled 必拒（protected_online_no_unattended）、
  新增 real_input 策略同样被拒、README 范围声明。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from domain_model.models import PolicyMode
from domain_model.validation import validate_project_dir

REPO = Path(__file__).resolve().parents[2]
DESKTOP_TEMPLATE = REPO / "examples" / "desktop-target-template"
SHADOW_TEMPLATE = REPO / "examples" / "protected-online-shadow-template"


# ---------------------------------------------------------------------------
# ADP-001：普通桌面应用目标模板
# ---------------------------------------------------------------------------


def test_desktop_template_validates():
    result = validate_project_dir(DESKTOP_TEMPLATE)
    assert result.ok, [f"{i.file}{i.pointer}: {i.rule} {i.message}" for i in result.issues]
    assert result.objects_checked >= 8  # project + target + policy + calibration + 2 detectors + machine + asset


def test_desktop_policy_conservative_defaults():
    """real_input 允许但必须人工启动 + 保守限额（POL-002）。"""
    data = yaml.safe_load((DESKTOP_TEMPLATE / "policies" / "default.yaml").read_text(encoding="utf-8"))
    assert data["mode"] == "real_input"
    assert data["require_manual_start"] is True
    assert data["unattended_schedule"] == "disabled"
    assert data["on_focus_lost"] == "stop"
    assert data["max_runtime_minutes"] <= 30
    assert data["max_actions_per_minute"] <= 120
    assert isinstance(data["max_total_actions"], int) and data["max_total_actions"] <= 500


def test_desktop_target_not_protected():
    data = yaml.safe_load((DESKTOP_TEMPLATE / "targets" / "desktop-app.yaml").read_text(encoding="utf-8"))
    assert data["protected_online"] is False
    assert data["executable"] == Path(data["executable"]).name  # 不含路径分隔符
    assert "/" not in data["executable"] and "\\" not in data["executable"]


def test_desktop_detectors_cover_color_region_and_template():
    kinds: set[str] = set()
    fields: set[str] = set()
    for f in sorted((DESKTOP_TEMPLATE / "detectors").glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        kinds.add(data["type"])
        fields.add(data["field_name"])
    assert {"color_region", "template_match"} <= kinds
    # 状态机迁移引用的字段都由这些检测器输出
    machine = yaml.safe_load((DESKTOP_TEMPLATE / "machines" / "main.machine.yaml").read_text(encoding="utf-8"))
    referenced: set[str] = set()
    for state in machine["states"].values():
        for tr in state.get("transitions", []):
            for token in ("ready_button", "progress_ratio"):
                if token in tr["when"]:
                    referenced.add(token)
    assert referenced <= fields


def test_desktop_readme_has_admission_checklist():
    """ADP-003 准入清单：文件存在且含五项勾选表。"""
    readme = DESKTOP_TEMPLATE / "README.md"
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    for item in ("授权依据", "风险等级", "可测试环境", "回滚", "数据处理"):
        assert f"- [ ] **{item}**" in text, f"准入清单缺少 {item} 勾选项"
    assert "mode: real_input" in text or "real_input" in text


# ---------------------------------------------------------------------------
# ADP-002：受保护在线目标 Shadow 模板
# ---------------------------------------------------------------------------


def test_shadow_template_validates_in_shadow_mode():
    result = validate_project_dir(SHADOW_TEMPLATE)
    assert result.ok, [f"{i.file}{i.pointer}: {i.rule} {i.message}" for i in result.issues]
    assert result.bundle is not None
    assert result.bundle.has_protected_target
    policy = next(iter(result.bundle.policies.values()))
    assert policy.mode_value is PolicyMode.SHADOW
    assert policy.unattended_value.value == "disabled"
    target = next(iter(result.bundle.targets.values()))
    assert target.protected_online and target.real_input_allowed is False


def _copy_and_tamper_policy(tmp_path: Path, replacements: dict[str, str], name: str = "proj") -> Path:
    proj = tmp_path / name
    shutil.copytree(SHADOW_TEMPLATE, proj)
    policy_file = proj / "policies" / "default.yaml"
    text = policy_file.read_text(encoding="utf-8")
    for old, new in replacements.items():
        assert old in text, f"模板缺少预期内容 {old!r}"
        text = text.replace(old, new)
    policy_file.write_text(text, encoding="utf-8")
    return proj


def test_shadow_template_rejects_real_input_tamper(tmp_path):
    """手工把 mode 改成 real_input → validate 拒绝，含硬锁规则 ID。"""
    proj = _copy_and_tamper_policy(tmp_path, {"mode: shadow": "mode: real_input"})
    result = validate_project_dir(proj)
    assert not result.ok
    rules = {i.rule for i in result.issues}
    assert "protected_online_no_real_input" in rules


def test_shadow_template_rejects_unattended_tamper(tmp_path):
    """手工把 unattended_schedule 改成 enabled → validate 拒绝。"""
    proj = _copy_and_tamper_policy(tmp_path, {"unattended_schedule: disabled": "unattended_schedule: enabled"})
    result = validate_project_dir(proj)
    assert not result.ok
    rules = {i.rule for i in result.issues}
    assert "protected_online_no_unattended" in rules


def test_shadow_template_rejects_extra_real_input_policy(tmp_path):
    """新增一个 real_input 策略文件同样被硬锁（规则与文件名无关）。"""
    proj = tmp_path / "extra-policy"
    shutil.copytree(SHADOW_TEMPLATE, proj)
    (proj / "policies" / "sneaky.yaml").write_text(
        "schema_version: 1\n"
        "policy_id: sneaky\n"
        "mode: real_input\n"
        "require_manual_start: true\n"
        "max_runtime_minutes: 5\n"
        "max_actions_per_minute: 10\n"
        "on_focus_lost: stop\n"
        "unattended_schedule: disabled\n",
        encoding="utf-8",
    )
    result = validate_project_dir(proj)
    assert not result.ok
    assert "protected_online_no_real_input" in {i.rule for i in result.issues}


def test_shadow_readme_declares_scope():
    """README 明确 仅观察/标注/回放/Shadow 与 auto_reconnect=false 语义。"""
    text = (SHADOW_TEMPLATE / "README.md").read_text(encoding="utf-8")
    for keyword in ("观察", "标注", "回放", "Shadow"):
        assert keyword in text
    assert "auto_reconnect" in text
    assert "protected_online_no_real_input" in text
    assert "protected_online_no_unattended" in text
