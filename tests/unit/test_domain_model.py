"""domain_model 领域对象与解析层单测（DOM-001/005 正反样例）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from domain_model import (
    CAPABILITY_REGISTRY,
    CalibrationProfile,
    Detector,
    DomainValidationError,
    FieldObservation,
    PerceptionSnapshot,
    PolicyProfile,
    StateMachineDef,
    TargetProfile,
    VisualAsset,
    authorize,
    authorize_input,
    load_project,
    parse_asset,
    parse_calibration,
    parse_detector,
    parse_machine,
    parse_perception_snapshot,
    parse_policy,
    parse_target,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "arena_lab_demo"


def rules_of(exc: DomainValidationError) -> set[str]:
    return {i.rule for i in exc.issues}


def pointers_of(exc: DomainValidationError, rule: str) -> list[str]:
    return [i.pointer for i in exc.issues if i.rule == rule]


# ---------------------------------------------------------------------------
# TargetProfile
# ---------------------------------------------------------------------------


def test_target_real_input_lock() -> None:
    """受保护在线目标恒不允许真实输入（real_input_allowed 恒 False）。"""
    protected = TargetProfile(
        target_id="online",
        executable="Game.exe",
        title_regex="^Game",
        protected_online=True,
        allowed_display_modes=["windowed"],
    )
    local = TargetProfile(
        target_id="arena-lab",
        executable="ArenaLab.exe",
        title_regex="^ArenaLab",
        protected_online=False,
        allowed_display_modes=["windowed"],
    )
    assert protected.real_input_allowed is False
    assert local.real_input_allowed is True


def test_parse_target_valid() -> None:
    data = {
        "schema_version": 1,
        "target_id": "arena-lab",
        "executable": "ArenaLab.exe",
        "title_regex": "^ArenaLab - Training$",
        "protected_online": False,
        "allowed_display_modes": ["windowed", "borderless"],
        "notes": "本地模拟器",
    }
    target = parse_target(data, file="targets/arena-lab.yaml")
    assert target.target_id == "arena-lab"
    assert target.executable == "ArenaLab.exe"
    assert target.source_file == "targets/arena-lab.yaml"


def test_parse_target_missing_required_field() -> None:
    data = {
        "schema_version": 1,
        "target_id": "arena-lab",
        "title_regex": "^ArenaLab",
        "protected_online": False,
        "allowed_display_modes": ["windowed"],
    }
    with pytest.raises(DomainValidationError) as exc:
        parse_target(data)
    assert "missing_field" in rules_of(exc.value)
    assert "/executable" in pointers_of(exc.value, "missing_field")


def test_parse_target_invalid_display_mode() -> None:
    data = {
        "schema_version": 1,
        "target_id": "arena-lab",
        "executable": "ArenaLab.exe",
        "title_regex": "^ArenaLab",
        "protected_online": False,
        "allowed_display_modes": ["fullscreen_borderless_magic"],
    }
    with pytest.raises(DomainValidationError) as exc:
        parse_target(data)
    assert "invalid_enum" in rules_of(exc.value)


def test_parse_target_rejects_unknown_field() -> None:
    data = {
        "schema_version": 1,
        "target_id": "arena-lab",
        "executable": "ArenaLab.exe",
        "title_regex": "^ArenaLab",
        "protected_online": False,
        "allowed_display_modes": ["windowed"],
        "protect_online": True,  # 拼写错误的字段必须拒绝而不是静默忽略
    }
    with pytest.raises(DomainValidationError) as exc:
        parse_target(data)
    assert "unknown_field" in rules_of(exc.value)


def test_parse_target_invalid_title_regex() -> None:
    data = {
        "schema_version": 1,
        "target_id": "arena-lab",
        "executable": "ArenaLab.exe",
        "title_regex": "^ArenaLab[",  # 非法正则
        "protected_online": False,
        "allowed_display_modes": ["windowed"],
    }
    with pytest.raises(DomainValidationError) as exc:
        parse_target(data)
    assert "invalid_regex" in rules_of(exc.value)


# ---------------------------------------------------------------------------
# CalibrationProfile
# ---------------------------------------------------------------------------


def test_parse_calibration_valid_and_derived_size() -> None:
    calibration = parse_calibration(
        {
            "schema_version": 1,
            "calibration_id": "1920x1080-100",
            "resolution": "1920x1080",
            "dpi_percent": 100,
            "ui_scale": 1.0,
            "anchors": {"player_panel": [0.04, 0.92]},
        }
    )
    assert (calibration.width_px, calibration.height_px) == (1920, 1080)
    assert calibration.anchors["player_panel"] == (0.04, 0.92)


def test_parse_calibration_bad_resolution_and_anchor() -> None:
    with pytest.raises(DomainValidationError) as exc:
        parse_calibration(
            {
                "schema_version": 1,
                "calibration_id": "c1",
                "resolution": "1920*1080",
                "dpi_percent": 100,
                "ui_scale": 1.0,
            }
        )
    assert "invalid_resolution" in rules_of(exc.value)

    with pytest.raises(DomainValidationError) as exc2:
        parse_calibration(
            {
                "schema_version": 1,
                "calibration_id": "c1",
                "resolution": "1920x1080",
                "dpi_percent": 100,
                "ui_scale": 1.0,
                "anchors": {"bar": [0.5, 1.7]},  # 超出归一化范围
            }
        )
    assert "anchor_out_of_range" in rules_of(exc2.value)
    assert "/anchors/bar" in pointers_of(exc2.value, "anchor_out_of_range")


def test_calibration_dataclass_rejects_bad_dpi() -> None:
    with pytest.raises(DomainValidationError):
        CalibrationProfile(
            calibration_id="c1", resolution="1920x1080", dpi_percent=20, ui_scale=1.0
        )


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def test_parse_detector_valid_template_match() -> None:
    detector = parse_detector(
        {
            "schema_version": 1,
            "detector_id": "ready-button",
            "type": "template_match",
            "roi": [0.4, 0.78, 0.2, 0.12],
            "threshold": 0.91,
            "stable_frames": 3,
            "field_name": "ready",
            "template": "assets/templates/ready.png",
        }
    )
    assert detector.field_name == "ready"
    assert detector.threshold == pytest.approx(0.91)


@pytest.mark.parametrize(
    "roi",
    [
        [0.9, 0.2, 0.3, 0.4],  # x+w > 1
        [0.2, 0.9, 0.3, 0.4],  # y+h > 1
    ],
)
def test_detector_roi_out_of_bounds(roi: list[float]) -> None:
    with pytest.raises(DomainValidationError) as exc:
        Detector(
            detector_id="d", type="color_region", roi=roi,
            threshold=0.5, stable_frames=1, field_name="f",
        )
    assert "roi_out_of_bounds" in rules_of(exc.value)


@pytest.mark.parametrize(
    "roi",
    [
        [-0.1, 0.2, 0.3, 0.4],  # 负 x
        [0.1, 0.2, -0.3, 0.4],  # 负 w
        [0.1, 0.2, 0.3, 0.0],   # 零宽
    ],
)
def test_detector_roi_negative_or_zero(roi: list[float]) -> None:
    with pytest.raises(DomainValidationError) as exc:
        Detector(
            detector_id="d", type="color_region", roi=roi,
            threshold=0.5, stable_frames=1, field_name="f",
        )
    assert rules_of(exc.value) & {"roi_negative", "roi_out_of_bounds"}


@pytest.mark.parametrize("threshold", [1.2, -0.1])
def test_detector_threshold_out_of_range(threshold: float) -> None:
    with pytest.raises(DomainValidationError) as exc:
        Detector(
            detector_id="d", type="color_region", roi=[0.1, 0.1, 0.3, 0.3],
            threshold=threshold, stable_frames=1, field_name="f",
        )
    assert "threshold_out_of_range" in rules_of(exc.value)
    assert "/threshold" in pointers_of(exc.value, "threshold_out_of_range")


def test_detector_stable_frames_must_be_positive() -> None:
    with pytest.raises(DomainValidationError) as exc:
        Detector(
            detector_id="d", type="ocr_roi", roi=[0.1, 0.1, 0.3, 0.3],
            threshold=0.5, stable_frames=0, field_name="f",
        )
    assert "stable_frames_invalid" in rules_of(exc.value)


def test_detector_template_rules() -> None:
    """template_match 必须引用模板；其他类型不允许引用模板。"""
    with pytest.raises(DomainValidationError) as exc:
        Detector(
            detector_id="d", type="template_match", roi=[0.1, 0.1, 0.3, 0.3],
            threshold=0.5, stable_frames=1, field_name="f",
        )
    assert "template_required" in rules_of(exc.value)

    with pytest.raises(DomainValidationError) as exc2:
        Detector(
            detector_id="d", type="color_region", roi=[0.1, 0.1, 0.3, 0.3],
            threshold=0.5, stable_frames=1, field_name="f",
            template="assets/templates/x.png",
        )
    assert "template_not_allowed" in rules_of(exc2.value)


def test_detector_invalid_type_enum() -> None:
    with pytest.raises(DomainValidationError) as exc:
        Detector(
            detector_id="d", type="magic_detection", roi=[0.1, 0.1, 0.3, 0.3],
            threshold=0.5, stable_frames=1, field_name="f",
        )
    assert "invalid_enum" in rules_of(exc.value)


# ---------------------------------------------------------------------------
# PolicyProfile
# ---------------------------------------------------------------------------


def test_parse_policy_valid_and_defaults() -> None:
    policy = parse_policy(
        {
            "schema_version": 1,
            "policy_id": "default",
            "mode": "shadow",
            "require_manual_start": True,
            "max_runtime_minutes": 20,
            "max_actions_per_minute": 120,
        }
    )
    assert policy.unattended_value.value == "disabled"  # 默认关闭无人值守
    assert policy.on_focus_lost == "stop"
    assert policy.mode_value.value == "shadow"


def test_parse_policy_invalid_mode_and_focus_lost() -> None:
    with pytest.raises(DomainValidationError) as exc:
        parse_policy(
            {
                "schema_version": 1,
                "policy_id": "default",
                "mode": "god_mode",
                "require_manual_start": True,
                "max_runtime_minutes": 20,
                "max_actions_per_minute": 120,
            }
        )
    assert "invalid_enum" in rules_of(exc.value)
    assert "/mode" in pointers_of(exc.value, "invalid_enum")

    with pytest.raises(DomainValidationError) as exc2:
        parse_policy(
            {
                "schema_version": 1,
                "policy_id": "default",
                "mode": "shadow",
                "require_manual_start": True,
                "max_runtime_minutes": 20,
                "max_actions_per_minute": 120,
                "on_focus_lost": "ignore",
            }
        )
    assert "invalid_enum" in rules_of(exc2.value)


def test_parse_policy_budget_must_be_positive() -> None:
    with pytest.raises(DomainValidationError) as exc:
        parse_policy(
            {
                "schema_version": 1,
                "policy_id": "default",
                "mode": "shadow",
                "require_manual_start": True,
                "max_runtime_minutes": 0,
                "max_actions_per_minute": 120,
            }
        )
    assert "out_of_range" in rules_of(exc.value)


# ---------------------------------------------------------------------------
# StateMachineDef
# ---------------------------------------------------------------------------


def _machine_data() -> dict:
    return {
        "schema_version": 1,
        "machine_id": "main",
        "initial": "idle",
        "states": {
            "idle": {"transitions": [{"when": "ready.present", "to": "stopped"}]},
            "stopped": {"terminal": True},
        },
    }


def test_parse_machine_valid() -> None:
    machine = parse_machine(_machine_data())
    assert machine.initial == "idle"
    assert machine.states["stopped"].terminal is True


def test_parse_machine_initial_not_defined() -> None:
    data = _machine_data()
    data["initial"] = "ghost"
    with pytest.raises(DomainValidationError) as exc:
        parse_machine(data)
    assert "initial_state_missing" in rules_of(exc.value)
    assert "/initial" in pointers_of(exc.value, "initial_state_missing")


def test_parse_machine_rejects_unknown_state_field() -> None:
    data = _machine_data()
    data["states"]["idle"]["actions"] = [{"kind": "press_key"}]  # 不允许的字段
    with pytest.raises(DomainValidationError) as exc:
        parse_machine(data)
    assert "unknown_field" in rules_of(exc.value)


def test_machine_requires_states() -> None:
    with pytest.raises(DomainValidationError):
        StateMachineDef(machine_id="main", initial="idle", states={})


# ---------------------------------------------------------------------------
# VisualAsset / PerceptionSnapshot
# ---------------------------------------------------------------------------


def test_parse_asset_valid_and_bad_hash() -> None:
    asset = parse_asset(
        {
            "asset_id": "asset-ready",
            "path": "assets/templates/ready.png",
            "sha256": "a" * 64,
            "version": 1,
            "kind": "template",
        }
    )
    assert asset.path == "assets/templates/ready.png"

    with pytest.raises(DomainValidationError) as exc:
        parse_asset(
            {
                "asset_id": "asset-ready",
                "path": "assets/templates/ready.png",
                "sha256": "not-a-hash",
                "version": 1,
                "kind": "template",
            }
        )
    assert "invalid_hash" in rules_of(exc.value)

    with pytest.raises(DomainValidationError) as exc2:
        parse_asset(
            {
                "asset_id": "asset-evil",
                "path": "../../outside.png",  # 越出项目根
                "sha256": "a" * 64,
                "version": 1,
                "kind": "mask",
            }
        )
    assert "path_escape" in rules_of(exc2.value)


def test_visual_asset_dataclass_valid() -> None:
    asset = VisualAsset(
        asset_id="asset-mask", path="assets/masks/hud.png",
        sha256="b" * 64, version=2, kind="mask",
    )
    assert asset.kind_value.value == "mask"


def test_parse_perception_snapshot_contains_no_action_fields() -> None:
    snapshot = parse_perception_snapshot(
        {
            "frame_seq": 7,
            "ts_monotonic": 12.5,
            "values": {
                "ready": {"present": True, "confidence": 0.97, "value": True},
                "health_ratio": {"present": True, "confidence": 0.9, "value": 0.83},
            },
        }
    )
    assert snapshot.values["ready"].value is True
    # 感知快照只含感知事实：不包含任何动作/意图字段
    data_vars = set(vars(snapshot))
    assert data_vars == {"frame_seq", "ts_monotonic", "values"}

    with pytest.raises(DomainValidationError) as exc:
        FieldObservation(name="x", present=True, confidence=1.5)
    assert "confidence_out_of_range" in rules_of(exc.value)


# ---------------------------------------------------------------------------
# load_project 聚合根
# ---------------------------------------------------------------------------


def test_load_project_arena_lab_demo() -> None:
    bundle = load_project(EXAMPLE)
    assert bundle.project["name"] == "arena_lab_demo"
    assert set(bundle.targets) == {"arena-lab"}
    assert set(bundle.policies) == {"default"}
    assert set(bundle.calibrations) == {"1920x1080-100"}
    assert set(bundle.detectors) == {"ready_button", "gate_button", "health_bar"}
    assert set(bundle.machines) == {"main"}
    assert set(bundle.assets) == {"asset-ready-button", "asset-gate-button"}
    assert bundle.perception_field_names == {"ready", "manual_gate", "health_ratio"}
    assert bundle.has_protected_target is False
    assert bundle.object_count() == 10


def test_load_project_missing_project_yaml(tmp_path: Path) -> None:
    with pytest.raises(DomainValidationError) as exc:
        load_project(tmp_path)
    assert "project_file_missing" in rules_of(exc.value)


def test_load_project_reports_duplicate_id(tmp_path: Path) -> None:
    (tmp_path / "project.yaml").write_text("schema_version: 1\nname: dup\n", encoding="utf-8")
    body = (
        "schema_version: 1\npolicy_id: same\nmode: observe\nrequire_manual_start: true\n"
        "max_runtime_minutes: 5\nmax_actions_per_minute: 10\n"
    )
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "a.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "policies" / "b.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(DomainValidationError) as exc:
        load_project(tmp_path)
    assert "duplicate_id" in rules_of(exc.value)


# ---------------------------------------------------------------------------
# 能力清单与授权模型（DOM-005）：一切能力默认拒绝
# ---------------------------------------------------------------------------


def test_capabilities_default_deny() -> None:
    assert "input.key" in CAPABILITY_REGISTRY
    # 未授予 -> 拒绝
    assert authorize("input.key") is False
    # 已注册且显式授予 -> 允许
    assert authorize("input.key", {"input.key"}) is True
    # 未注册能力即使出现在授予列表中也必须拒绝
    assert authorize("driver.hide", {"driver.hide"}) is False


def test_input_capability_requires_target_permission() -> None:
    protected = TargetProfile(
        target_id="online", executable="Game.exe", title_regex="^Game",
        protected_online=True, allowed_display_modes=["windowed"],
    )
    local = TargetProfile(
        target_id="arena-lab", executable="ArenaLab.exe", title_regex="^ArenaLab",
        protected_online=False, allowed_display_modes=["windowed"],
    )
    granted = {"input.key"}
    assert authorize_input("input.key", granted, protected) is False
    assert authorize_input("input.key", granted, local) is True
    assert authorize_input("perception.read", granted, local) is False  # 非输入能力不适用
