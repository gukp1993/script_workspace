"""release_kit 单测（E12：VER-001~010，AC-P0-11/12/14 的单测版证据）。

夹具策略：把 ``examples/arena_lab_demo`` 复制到 ``tmp_path`` 作为项目工作区，
注入 :class:`FakeClock` 保证确定性；发布/快照/备份目录均放在项目外。
"""

from __future__ import annotations

import json
import os
import random
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml
from common.clock import FakeClock
from domain_model.parsing import load_project
from domain_model.static_analysis import analyze

from release_kit import (
    CheckOutcome,
    CompatError,
    HMACSigner,
    ImportReport,
    ImpactGraph,
    MigrationStepError,
    NullSigner,
    ObjectDiff,
    ReleaseError,
    ReleaseManifest,
    ReleasePublisher,
    ReleaseRollback,
    RuntimeCompat,
    SnapshotManager,
    build_object_catalog,
    build_release_manifest,
    compat_check,
    compat_message,
    content_digest,
    content_hash,
    export_release,
    freeze_directory,
    hash_domain_object,
    import_release,
    verify_directory,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "arena_lab_demo"
#: 示例项目自带的标定文件（第五类对象，JSON 格式）
CALIBRATION_FILE = "calibrations/1920x1080-100.json"


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """复制示例项目到临时目录（含五类对象：标定为 JSON 文件）。"""
    dest = tmp_path / "project"
    shutil.copytree(EXAMPLE, dest)
    return dest


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(start=1000.0)


def make_publisher(project_dir: Path, clock: FakeClock) -> ReleasePublisher:
    return ReleasePublisher(project_dir, project_dir.parent / "releases", clock)


def edit_yaml(path: Path, mutate) -> None:
    """读取 YAML -> 修改 -> 写回（测试内修改临时项目用）。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def edit_json(path: Path, mutate) -> None:
    """读取 JSON -> 修改 -> 写回（标定等 JSON 配置用）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def bump_template(project_dir: Path) -> None:
    """替换 gate 模板内容并同步资产清单（模拟资产变更）。"""
    template = project_dir / "assets" / "templates" / "gate.png"
    template.write_bytes(b"\x89PNG\r\n\x1a\nv2-template-payload")

    def mutate(data: dict) -> None:
        for entry in data["assets"]:
            if entry["asset_id"] == "asset-gate-button":
                entry["sha256"] = content_hash(template)
                entry["version"] = 2

    edit_yaml(project_dir / "assets" / "assets.yaml", mutate)


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    """构造任意条目的 zip（恶意包测试用）。"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def rules_of(report: ImportReport) -> set[str]:
    return report.rule_ids


# ---------------------------------------------------------------------------
# hashing（VER-002 / SEC-004）
# ---------------------------------------------------------------------------


class TestHashing:
    def test_content_hash_bytes_and_file(self, project_dir: Path) -> None:
        """bytes 与文件路径两种入参得到相同 sha256；内容不同哈希不同。"""
        payload = b"arena-lab-payload"
        target = project_dir / "payload.bin"
        target.write_bytes(payload)
        assert content_hash(payload) == content_hash(target)
        assert content_hash(payload) != content_hash(payload + b"x")
        assert len(content_hash(payload)) == 64

    def test_freeze_directory_recursive_and_excludes(self, project_dir: Path) -> None:
        """冻结覆盖全部受管文件；traces/releases/snapshots/backups/.git/__pycache__ 被排除。"""
        for stray in ("traces/a/x.bin", "releases/v1/f", "snapshots/s/f", "backups/b/f", ".git/HEAD", "__pycache__/m.pyc"):
            file_path = project_dir / stray
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(b"x")
        files = freeze_directory(project_dir)
        assert "project.yaml" in files
        assert "assets/templates/gate.png" in files
        assert "detectors/health_bar.yaml" in files
        assert CALIBRATION_FILE in files
        for stray in ("traces/a/x.bin", "releases/v1/f", "snapshots/s/f", "backups/b/f", ".git/HEAD", "__pycache__/m.pyc"):
            assert stray not in files
        assert all(value and len(value) == 64 for value in files.values())

    def test_verify_directory_reports_modified_missing_extra(self, project_dir: Path) -> None:
        """篡改/缺失/多余分别报告为 modified/missing/unexpected。"""
        expected = freeze_directory(project_dir)
        assert verify_directory(project_dir, expected) == []
        (project_dir / "detectors" / "health_bar.yaml").write_text("tampered: true\n", encoding="utf-8")
        (project_dir / "policies" / "default.yaml").unlink()
        (project_dir / "stray.txt").write_text("extra", encoding="utf-8")
        problems = verify_directory(project_dir, expected)
        assert "modified: detectors/health_bar.yaml" in problems
        assert "missing: policies/default.yaml" in problems
        assert "unexpected: stray.txt" in problems

    def test_content_digest_stable_and_sensitive(self, project_dir: Path) -> None:
        """同一内容清单指纹稳定；任一文件哈希变化指纹变化。"""
        files = freeze_directory(project_dir)
        assert content_digest(files) == content_digest(dict(reversed(sorted(files.items()))))
        changed = dict(files)
        changed["detectors/health_bar.yaml"] = "0" * 64
        assert content_digest(changed) != content_digest(files)


# ---------------------------------------------------------------------------
# manifest（VER-001）
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_roundtrip_dict_and_save_load(self, project_dir: Path, tmp_path: Path) -> None:
        """to_dict/from_dict 与 save/load 双向往返一致。"""
        bundle = load_project(project_dir)
        files = freeze_directory(project_dir)
        manifest = build_release_manifest(bundle=bundle, files=files, release_id="v1-abcd1234", created_at=42.0)
        data = manifest.to_dict()
        assert ReleaseManifest.from_dict(data).to_dict() == data
        target = tmp_path / "manifest.json"
        manifest.save(target)
        assert ReleaseManifest.load(target).to_dict() == data
        assert data["schema_version"] == "1"
        assert "manifest.json" not in manifest.files  # 冻结先于清单，清单不含自身

    def test_manifest_hash_stable_across_builds(self, project_dir: Path) -> None:
        """同一内容两次构建的清单哈希/对象哈希完全一致（确定性）。"""
        files = freeze_directory(project_dir)
        first = build_release_manifest(bundle=load_project(project_dir), files=files, release_id="v1-aaaaaaaa", created_at=1.0)
        second = build_release_manifest(bundle=load_project(project_dir), files=files, release_id="v2-bbbbbbbb", created_at=2.0)
        assert first.content_sha == second.content_sha
        assert first.objects == second.objects
        assert first.files == second.files

    def test_manifest_covers_five_categories_policy_and_assets(self, project_dir: Path) -> None:
        """清单覆盖五类对象 + 策略摘要 + 资产清单。"""
        manifest = build_release_manifest(
            bundle=load_project(project_dir), files=freeze_directory(project_dir), release_id="v1-abcd1234", created_at=1.0
        )
        assert manifest.object_ids("targets") == ["arena-lab"]
        assert manifest.object_ids("policies") == ["default"]
        assert manifest.object_ids("detectors") == ["gate_button", "health_bar", "ready_button"]
        assert manifest.object_ids("machines") == ["main"]
        assert manifest.object_ids("calibrations") == ["1920x1080-100"]
        summary = manifest.policy_summary[0]
        assert summary["policy_id"] == "default"
        assert summary["mode"] == "shadow"
        assert summary["unattended_schedule"] == "disabled"
        assert manifest.assets["assets/templates/gate.png"] == content_hash(project_dir / "assets" / "templates" / "gate.png")
        assert manifest.project["name"] == "arena_lab_demo"

    def test_object_hash_semantic_and_version_increment(self, project_dir: Path) -> None:
        """对象哈希只反映语义内容；相对上一发布，变更对象版本 +1。"""
        bundle = load_project(project_dir)
        first = build_release_manifest(bundle=bundle, files=freeze_directory(project_dir), release_id="v1-aabbccdd", created_at=1.0)
        assert first.find_object("detectors", "health_bar") is not None
        assert first.find_object("detectors", "health_bar").version == 1

        edit_yaml(
            project_dir / "detectors" / "health_bar.yaml",
            lambda data: data.update(threshold=0.7),
        )
        second = build_release_manifest(
            bundle=load_project(project_dir),
            files=freeze_directory(project_dir),
            release_id="v2-ddeeff00",
            created_at=2.0,
            previous=first,
        )
        assert second.find_object("detectors", "health_bar").version == 2
        assert second.find_object("detectors", "ready_button").version == 1  # 未变沿用
        assert second.find_object("detectors", "health_bar").sha256 != first.find_object("detectors", "health_bar").sha256

    def test_manifest_rejects_unknown_schema_version(self) -> None:
        """Schema 版本不符的清单被拒绝（防止静默错读）。"""
        with pytest.raises(ReleaseError, match="Schema"):
            ReleaseManifest.from_dict({"schema_version": "999", "release_id": "v1-aaaaaaaa", "created_at": 0.0, "files": {}, "content_sha": "x"})


# ---------------------------------------------------------------------------
# publisher（VER-002 / VER-005）
# ---------------------------------------------------------------------------


class TestPublisher:
    def test_publish_creates_record_manifest_and_readonly(self, project_dir: Path, clock: FakeClock) -> None:
        """发布产生 ReleaseRecord、manifest.json、manifest.sig，目录只读（尽力而为）。"""
        publisher = make_publisher(project_dir, clock)
        record = publisher.publish()
        assert record.release_id == "v1-" + record.content_sha[:8]
        assert record.manifest_path.is_file()
        assert (record.directory / "manifest.sig").is_file()
        manifest = ReleaseManifest.load(record.manifest_path)
        assert manifest.release_id == record.release_id
        assert "manifest.json" not in manifest.files
        for rel in manifest.files:
            mode = (record.directory / rel).stat().st_mode
            assert mode & 0o222 == 0, f"发布文件应为只读：{rel}"

    def test_publish_uses_injected_clock(self, project_dir: Path, clock: FakeClock) -> None:
        """created_at 来自注入时钟（可确定性测试）。"""
        publisher = make_publisher(project_dir, clock)
        first = publisher.publish()
        assert first.created_at == 1000.0
        clock.advance(5)
        edit_yaml(project_dir / "policies" / "default.yaml", lambda data: data.update(max_runtime_minutes=15))
        second = publisher.publish()
        assert publisher.load_release(second.release_id).created_at == 1005.0

    def test_publish_idempotent_for_same_content(self, project_dir: Path, clock: FakeClock) -> None:
        """同内容重复发布复用同一 release_id（内容寻址幂等）。"""
        publisher = make_publisher(project_dir, clock)
        first = publisher.publish()
        clock.advance(1)
        second = publisher.publish()
        assert second.release_id == first.release_id
        assert len(publisher.list_releases()) == 1
        assert second.created_at == 1000.0  # 沿用既有清单，不覆盖时间戳

    def test_publish_new_version_and_sequence_on_change(self, project_dir: Path, clock: FakeClock) -> None:
        """任何内容变化产生新版本，序号递增，短哈希来自内容指纹。"""
        publisher = make_publisher(project_dir, clock)
        first = publisher.publish()
        edit_yaml(project_dir / "detectors" / "health_bar.yaml", lambda data: data.update(threshold=0.7))
        second = publisher.publish()
        assert first.release_id == "v1-" + first.content_sha[:8]
        assert second.release_id == "v2-" + second.content_sha[:8]
        assert second.content_sha != first.content_sha
        assert [r.release_id for r in publisher.list_releases()] == [first.release_id, second.release_id]

    def test_publish_blocked_by_static_errors(self, tmp_path: Path, clock: FakeClock) -> None:
        """静态分析 error 级问题阻断发布（protected_online 反例，AC-P0-10）。"""
        project = tmp_path / "protected"
        shutil.copytree(REPO / "examples" / "protected_online_demo", project)
        publisher = ReleasePublisher(project, tmp_path / "releases", clock)
        issues = analyze(project)
        assert any(issue.rule == "protected_online_no_real_input" for issue in issues)
        with pytest.raises(ReleaseError) as exc:
            publisher.publish()
        assert any(issue.rule == "protected_online_no_real_input" for issue in exc.value.issues)
        assert not (tmp_path / "releases").exists() or not list((tmp_path / "releases").iterdir())

    def test_publish_blocked_by_unparseable_project(self, project_dir: Path, clock: FakeClock) -> None:
        """项目解析失败（结构错误）同样阻断发布。"""
        (project_dir / "detectors" / "broken.yaml").write_text("type: [unclosed\n", encoding="utf-8")
        publisher = make_publisher(project_dir, clock)
        with pytest.raises(ReleaseError) as exc:
            publisher.publish()
        assert exc.value.issues, "解析失败应携带可定位 Issue"

    def test_publish_blocked_by_gate_check_with_reason(self, project_dir: Path, clock: FakeClock) -> None:
        """质量闸门钩子失败阻断发布并保留原因（VER-005 挂点）。"""
        publisher = make_publisher(project_dir, clock)

        def failing_gate(bundle) -> CheckOutcome:
            return CheckOutcome(ok=False, name="perception_golden", reason="黄金集指标未达标")

        with pytest.raises(ReleaseError) as named:
            publisher.publish(checks=[failing_gate])
        assert any("perception_golden" in r and "黄金集指标未达标" in r for r in named.value.reasons)

        with pytest.raises(ReleaseError) as plain:
            publisher.publish(checks=[lambda bundle: False])
        assert any("返回 False" in r for r in plain.value.reasons)
        releases_dir = project_dir.parent / "releases"
        assert not releases_dir.exists() or not list(releases_dir.iterdir())

    def test_publish_passing_gate_checks_allowed(self, project_dir: Path, clock: FakeClock) -> None:
        """全部闸门通过时正常发布。"""
        publisher = make_publisher(project_dir, clock)
        record = publisher.publish(
            checks=[lambda bundle: True, lambda bundle: CheckOutcome(ok=True, name="replay", reason="")]
        )
        assert record.release_id.startswith("v1-")

    def test_release_tamper_detected_by_verify(self, project_dir: Path, clock: FakeClock) -> None:
        """发布后篡改任一文件 -> verify_directory 报告该文件（VER-002）。"""
        publisher = make_publisher(project_dir, clock)
        record = publisher.publish()
        manifest = publisher.load_release(record.release_id)
        target = record.directory / "detectors" / "health_bar.yaml"
        os.chmod(target, 0o644)  # 模拟攻击者去除只读位
        target.write_text("schema_version: 1\ntampered: true\n", encoding="utf-8")
        problems = verify_directory(record.directory, manifest.files, ignore_extra=True)
        assert "modified: detectors/health_bar.yaml" in problems

    def test_list_and_load_release_roundtrip(self, project_dir: Path, clock: FakeClock) -> None:
        """list_releases / load_release 与清单内容一致。"""
        publisher = make_publisher(project_dir, clock)
        record = publisher.publish()
        manifest = publisher.load_release(record.release_id)
        assert manifest.objects["detectors"]
        assert manifest.test_summary.passed == 0
        assert [r.release_id for r in publisher.list_releases()] == [record.release_id]

    def test_load_missing_release_raises(self, project_dir: Path, clock: FakeClock) -> None:
        publisher = make_publisher(project_dir, clock)
        publisher.publish()
        with pytest.raises(ReleaseError, match="不存在"):
            publisher.load_release("v99-00000000")


# ---------------------------------------------------------------------------
# snapshots（VER-003）
# ---------------------------------------------------------------------------


class TestSnapshots:
    def test_snapshot_create_list_load(self, project_dir: Path, clock: FakeClock) -> None:
        """创建命名快照 -> 列表 -> 读取内容与冻结清单一致。"""
        manager = SnapshotManager(project_dir, project_dir.parent / "snapshots", clock=clock)
        ref = manager.create("baseline")
        clock.advance(2)
        manager.create("after-tuning")
        refs = manager.list()
        assert [r.name for r in refs] == ["baseline", "after-tuning"]
        data = manager.load(ref)
        assert data.files == freeze_directory(project_dir)
        assert data.ref.content_sha == content_digest(data.files)

    def test_snapshot_load_detects_tampering(self, project_dir: Path, clock: FakeClock) -> None:
        """快照被篡改时 load 抛错（快照不可变）。"""
        manager = SnapshotManager(project_dir, project_dir.parent / "snapshots", clock=clock)
        ref = manager.create("baseline")
        target = ref.directory / "detectors" / "health_bar.yaml"
        target.write_text("schema_version: 1\ntampered: true\n", encoding="utf-8")
        with pytest.raises(ReleaseError, match="完整性"):
            manager.load(ref)

    def test_snapshot_diff_object_level(self, project_dir: Path, clock: FakeClock) -> None:
        """对象级差异：新增/删除/修改（按对象 id + 内容哈希）。"""
        manager = SnapshotManager(project_dir, project_dir.parent / "snapshots", clock=clock)
        before = manager.create("before")
        edit_yaml(project_dir / "detectors" / "health_bar.yaml", lambda data: data.update(threshold=0.7))
        (project_dir / "detectors" / "extra_bar.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "detector_id": "extra_bar",
                    "type": "color_region",
                    "roi": [0.6, 0.6, 0.2, 0.2],
                    "threshold": 0.5,
                    "stable_frames": 1,
                    "field_name": "extra_flag",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (project_dir / "detectors" / "gate_button.yaml").unlink()
        after = manager.create("after")
        diff: ObjectDiff = manager.diff(before, after)
        assert diff.added["detectors"] == ["extra_bar"]
        assert diff.removed["detectors"] == ["gate_button"]
        assert diff.modified["detectors"] == ["health_bar"]
        assert "detectors/extra_bar.yaml" in diff.files_added
        assert "detectors/gate_button.yaml" in diff.files_removed

    def test_snapshot_diff_field_level(self, project_dir: Path, clock: FakeClock) -> None:
        """字段级差异：对 YAML dict 做 key 路径级对比。"""
        manager = SnapshotManager(project_dir, project_dir.parent / "snapshots", clock=clock)
        before = manager.create("before")
        edit_yaml(project_dir / "detectors" / "health_bar.yaml", lambda data: data.update(threshold=0.7))
        edit_yaml(
            project_dir / "machines" / "main.machine.yaml",
            lambda data: data["states"]["exercise"]["transitions"][0].update(when="health_ratio.value < 0.30"),
        )
        after = manager.create("after")
        diff = manager.diff(before, after)
        changes = diff.field_changes["detectors/health_bar"]
        assert changes["threshold"] == (0.5, 0.7)
        machine_changes = diff.field_changes["machines/main"]
        assert machine_changes["states.exercise.transitions.0.when"] == ("health_ratio.value < 0.20", "health_ratio.value < 0.30")
        assert diff.to_dict()["field_changes"]["detectors/health_bar"]["threshold"] == [repr(0.5), repr(0.7)]


# ---------------------------------------------------------------------------
# impact（VER-004）
# ---------------------------------------------------------------------------


class TestImpact:
    def test_impact_template_asset_full_chain(self, project_dir: Path) -> None:
        """改 gate 模板 -> gate_button 检测器 -> manual_gate 字段 -> 迁移/机器。"""
        graph = ImpactGraph(project_dir)
        report = graph.affected_by_asset("assets\\templates\\gate.png")  # 反斜杠输入也应命中
        assert report.detectors == ["gate_button"]
        assert report.fields == ["manual_gate"]
        assert "main/awaiting_manual_gate#0" in report.transitions
        assert report.machines == ["main"]
        assert "detector:gate_button" in report.suggested_rerun
        assert "machine:main" in report.suggested_rerun

    def test_impact_health_template_chain(self, project_dir: Path) -> None:
        """改 health 模板 -> health_bar 检测器 / health_ratio 字段 / 相关迁移。"""
        # 示例中 health_bar 是 color_bar_ratio（无模板）；在临时副本中给它接一个模板资产
        template = project_dir / "assets" / "templates" / "health.png"
        template.write_bytes(b"\x89PNG\r\n\x1a\nhealth-template")

        def mutate_assets(data: dict) -> None:
            data["assets"].append(
                {
                    "asset_id": "asset-health-bar",
                    "path": "assets/templates/health.png",
                    "kind": "template",
                    "version": 1,
                    "sha256": content_hash(template),
                }
            )

        edit_yaml(project_dir / "assets" / "assets.yaml", mutate_assets)
        edit_yaml(
            project_dir / "detectors" / "health_bar.yaml",
            lambda data: data.update(type="template_match", template="assets/templates/health.png"),
        )
        assert not [i for i in analyze(project_dir) if i.severity == "error"]
        report = ImpactGraph(project_dir).affected_by_asset("assets/templates/health.png")
        assert report.detectors == ["health_bar"]
        assert report.fields == ["health_ratio"]
        assert any(t.startswith("main/exercise") for t in report.transitions)
        assert report.machines == ["main"]

    def test_impact_by_detector_downstream(self, project_dir: Path) -> None:
        """检测器变更影响：输出字段与下游迁移/机器。"""
        graph = ImpactGraph(project_dir)
        report = graph.affected_by_detector("ready_button")
        assert report.trigger == "detector:ready_button"
        assert report.fields == ["ready"]
        assert "main/idle#0" in report.transitions
        assert report.machines == ["main"]

    def test_impact_json_export(self, project_dir: Path, tmp_path: Path) -> None:
        """依赖图可 JSON 导出（资产->检测器->字段->迁移->机器）。"""
        graph = ImpactGraph(project_dir)
        target = tmp_path / "impact.json"
        graph.export_json(target)
        data = json.loads(target.read_text(encoding="utf-8"))
        edges = {(e["from"], e["to"], e["relation"]) for e in data["edges"]}
        assert ("asset:assets/templates/gate.png", "detector:gate_button", "used_by") in edges
        assert ("detector:gate_button", "field:manual_gate", "produces") in edges
        assert any(rel == "guards" for _, _, rel in edges)
        assert any(rel == "belongs_to" for _, _, rel in edges)

    def test_impact_unknown_detector_raises(self, project_dir: Path) -> None:
        with pytest.raises(ReleaseError, match="未知检测器"):
            ImpactGraph(project_dir).affected_by_detector("nope")

    def test_impact_counts_project_tests_when_present(self, project_dir: Path) -> None:
        """项目 tests/ 目录存在时，文本引用受影响对象的用例计入建议重跑集。"""
        tests_dir = project_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "health_replay.yaml").write_text("case: health_ratio 回放回归\n", encoding="utf-8")
        (tests_dir / "unrelated.yaml").write_text("case: 与感知无关\n", encoding="utf-8")
        report = ImpactGraph(project_dir).affected_by_detector("health_bar")
        assert report.test_cases == ["tests/health_replay.yaml"]
        assert "tests/health_replay.yaml" in report.suggested_rerun


# ---------------------------------------------------------------------------
# signing（VER-006）
# ---------------------------------------------------------------------------


class TestSigning:
    def test_hmac_roundtrip_and_tamper(self) -> None:
        signer = HMACSigner(b"release-key")
        payload = b'{"release_id": "v1-aaaaaaaa"}'
        signature = signer.sign(payload)
        assert len(signature) == 64
        assert signer.verify(payload, signature)
        assert not signer.verify(payload + b" ", signature)
        assert not HMACSigner(b"other-key").verify(payload, signature)

    def test_null_signer_behavior(self) -> None:
        signer = NullSigner()
        assert signer.algorithm == "null"
        assert signer.sign(b"anything") == "unsigned"
        assert signer.verify(b"anything", "unsigned")
        assert not HMACSigner(b"k").verify(b"anything", signer.sign(b"anything"))

    def test_signed_package_import_flow(self, project_dir: Path, clock: FakeClock, tmp_path: Path) -> None:
        """签名包：正确密钥导入通过；错误密钥 signature_invalid 硬拒绝。"""
        publisher = make_publisher(project_dir, clock)
        record = publisher.publish(signer=HMACSigner(b"release-key"))
        zip_path = export_release(record.release_id, tmp_path / "signed.zip", project_dir.parent / "releases")
        good = import_release(zip_path, tmp_path / "projects", signer=HMACSigner(b"release-key"), allow_unsigned=False)
        assert good.ok and good.release_id == record.release_id
        bad = import_release(zip_path, tmp_path / "projects", signer=HMACSigner(b"wrong-key"), allow_unsigned=True)
        assert not bad.ok
        assert "signature_invalid" in rules_of(bad)


# ---------------------------------------------------------------------------
# transfer（VER-007 / SEC-002 / AC-P0-11）
# ---------------------------------------------------------------------------


class TestTransfer:
    def test_export_import_roundtrip(self, project_dir: Path, clock: FakeClock, tmp_path: Path) -> None:
        """导出 zip -> 导入隔离目录：全部登记文件哈希一致。"""
        publisher = make_publisher(project_dir, clock)
        record = publisher.publish()
        manifest = publisher.load_release(record.release_id)
        zip_path = export_release(record.release_id, tmp_path / "out.zip", project_dir.parent / "releases")
        report = import_release(zip_path, tmp_path / "projects")
        assert report.ok and report.warnings, "未签名包默认可导入但应有 unsigned_package 警告"
        assert any("real_input" in w for w in report.warnings)
        assert report.import_dir is not None and report.import_dir.is_dir()
        for rel, sha in manifest.files.items():
            assert content_hash(report.import_dir / rel) == sha, rel
        assert (report.import_dir / "manifest.json").is_file()

    def test_import_rejects_path_traversal(self, tmp_path: Path) -> None:
        """``../`` 穿越条目被 path_escape 拒绝且包外无文件写入（AC-P0-11）。"""
        projects_root = tmp_path / "projects"
        zip_path = make_zip(tmp_path / "evil.zip", {"../evil.txt": b"evil", "project.yaml": b"schema_version: 1\n"})
        report = import_release(zip_path, projects_root)
        assert not report.ok
        assert "path_escape" in rules_of(report)
        assert report.import_dir is None
        assert not list(tmp_path.rglob("evil.txt")), "穿越文件不得落盘"

    def test_import_rejects_absolute_and_drive_paths(self, tmp_path: Path) -> None:
        """绝对路径与盘符条目被 path_escape 拒绝。"""
        zip_path = make_zip(tmp_path / "evil.zip", {"/etc/evil.txt": b"x", "C:\\evil.txt": b"x"})
        report = import_release(zip_path, tmp_path / "projects")
        assert not report.ok
        assert "path_escape" in rules_of(report)
        assert not (tmp_path / "etc" / "evil.txt").exists()
        assert not list(tmp_path.rglob("evil.txt"))

    def test_import_rejects_symlink_entry(self, tmp_path: Path) -> None:
        """符号链接条目按路径逃逸拒绝（防解压逃逸）。"""
        path = tmp_path / "link.zip"
        with zipfile.ZipFile(path, "w") as archive:
            info = zipfile.ZipInfo("link/evil")
            info.external_attr = 0o120777 << 16  # S_IFLNK
            archive.writestr(info, b"/etc/passwd")
        report = import_release(path, tmp_path / "projects")
        assert not report.ok
        assert "path_escape" in rules_of(report)

    def test_import_rejects_executable_payload(self, tmp_path: Path) -> None:
        """可执行载荷扩展名（exe）被 executable_payload 拒绝。"""
        zip_path = make_zip(tmp_path / "evil.zip", {"tools/evil.exe": b"MZfake"})
        report = import_release(zip_path, tmp_path / "projects")
        assert not report.ok
        assert "executable_payload" in rules_of(report)
        assert report.import_dir is None

    def test_import_rejects_oversize_file(self, tmp_path: Path) -> None:
        """单文件超过上限被 file_too_large 拒绝。"""
        payload = bytes(range(256)) * 4  # 1024 字节、不可压缩（避开压缩比规则）
        zip_path = make_zip(tmp_path / "big.zip", {"data/blob.bin": payload})
        report = import_release(zip_path, tmp_path / "projects", max_file_bytes=100)
        assert not report.ok
        assert "file_too_large" in rules_of(report)

    def test_import_rejects_total_size_exceeded(self, tmp_path: Path) -> None:
        """解压总量（压缩前累计）超过上限被 total_size_exceeded 拒绝。"""
        payload = random.Random(42).randbytes(4_000_000)  # 4 MB 不可压缩（避开压缩比规则）
        zip_path = make_zip(tmp_path / "big.zip", {"a.bin": payload, "b.bin": payload})
        report = import_release(zip_path, tmp_path / "projects", max_total_bytes=5_000_000)
        assert not report.ok
        assert "total_size_exceeded" in rules_of(report)

    def test_import_rejects_compression_ratio_bomb(self, tmp_path: Path) -> None:
        """高压缩比（zip bomb 特征）被 compression_ratio_abuse 拒绝。"""
        zip_path = make_zip(tmp_path / "bomb.zip", {"bomb.txt": bytes(5_000_000)})  # 5MB 零 -> 约 5KB
        report = import_release(zip_path, tmp_path / "projects")
        assert not report.ok
        assert "compression_ratio_abuse" in rules_of(report)
        assert report.import_dir is None

    def test_import_corrupt_zip_safe_failure(self, tmp_path: Path) -> None:
        """损坏 zip 安全失败（corrupt_archive），不抛裸异常、不写文件。"""
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"this is not a zip archive")
        report = import_release(bad, tmp_path / "projects")
        assert not report.ok
        assert "corrupt_archive" in rules_of(report)
        assert not (tmp_path / "projects" / "imports").exists()

    def test_import_rejects_manifest_hash_mismatch(self, project_dir: Path, clock: FakeClock, tmp_path: Path) -> None:
        """包内文件与 manifest 登记哈希不一致 -> manifest_invalid。"""
        publisher = make_publisher(project_dir, clock)
        record = publisher.publish()
        zip_path = export_release(record.release_id, tmp_path / "out.zip", project_dir.parent / "releases")
        # 重打包：替换其中一个对象文件的内容
        with zipfile.ZipFile(zip_path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries["detectors/health_bar.yaml"] += b"\n# tampered\n"
        tampered = make_zip(tmp_path / "tampered.zip", entries)
        report = import_release(tampered, tmp_path / "projects")
        assert not report.ok
        assert "manifest_invalid" in rules_of(report)
        assert any(r.entry == "detectors/health_bar.yaml" for r in report.rejected)
        assert not (tmp_path / "projects" / "imports").exists()

    def test_import_rejects_unlisted_extra_file(self, project_dir: Path, clock: FakeClock, tmp_path: Path) -> None:
        """包内出现清单未登记的文件 -> manifest_invalid（防夹带）。"""
        publisher = make_publisher(project_dir, clock)
        record = publisher.publish()
        zip_path = export_release(record.release_id, tmp_path / "out.zip", project_dir.parent / "releases")
        with zipfile.ZipFile(zip_path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries["smuggled.txt"] = b"payload"
        repacked = make_zip(tmp_path / "repacked.zip", entries)
        report = import_release(repacked, tmp_path / "projects")
        assert not report.ok
        assert any(r.rule_id == "manifest_invalid" and r.entry == "smuggled.txt" for r in report.rejected)

    def test_import_unsigned_policy(self, project_dir: Path, clock: FakeClock, tmp_path: Path) -> None:
        """未知签名：默认允许导入但标注禁止 real_input；严格模式拒绝。"""
        publisher = make_publisher(project_dir, clock)  # 默认 NullSigner -> 视为未签名
        record = publisher.publish()
        zip_path = export_release(record.release_id, tmp_path / "out.zip", project_dir.parent / "releases")
        permissive = import_release(zip_path, tmp_path / "projects", allow_unsigned=True)
        assert permissive.ok
        assert any("unsigned_package" in w and "real_input" in w for w in permissive.warnings)
        strict = import_release(zip_path, tmp_path / "projects", allow_unsigned=False)
        assert not strict.ok
        assert "unsigned_package" in rules_of(strict)


# ---------------------------------------------------------------------------
# rollback（VER-008/009/010，AC-P0-12/14）
# ---------------------------------------------------------------------------


class TestRollback:
    @pytest.fixture()
    def two_versions(self, project_dir: Path, clock: FakeClock):
        """发布 v1 -> 同时修改状态机/模板/阈值/标定/策略（并新增对象）-> 发布 v2。"""
        publisher = make_publisher(project_dir, clock)
        v1 = publisher.publish()
        manifest_v1 = publisher.load_release(v1.release_id)
        edit_yaml(project_dir / "detectors" / "health_bar.yaml", lambda data: data.update(threshold=0.7))
        bump_template(project_dir)
        edit_yaml(
            project_dir / "machines" / "main.machine.yaml",
            lambda data: data["states"]["exercise"]["transitions"][0].update(when="health_ratio.value < 0.30"),
        )
        edit_json(project_dir / CALIBRATION_FILE, lambda data: data.update(dpi_percent=125))
        edit_yaml(project_dir / "policies" / "default.yaml", lambda data: data.update(max_runtime_minutes=15))
        (project_dir / "detectors" / "rogue_bar.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "detector_id": "rogue_bar",
                    "type": "color_region",
                    "roi": [0.5, 0.5, 0.2, 0.2],
                    "threshold": 0.5,
                    "stable_frames": 1,
                    "field_name": "rogue_flag",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        v2 = publisher.publish()
        rollback = ReleaseRollback(project_dir, project_dir.parent / "releases", clock=clock)
        return publisher, rollback, v1, manifest_v1, v2

    def test_full_rollback_restores_all_objects(self, two_versions, project_dir: Path) -> None:
        """AC-P0-12：五类对象 + 资产哈希全部恢复 v1 冻结值。"""
        publisher, rollback, v1, manifest_v1, v2 = two_versions
        assert v1.release_id != v2.release_id
        result = rollback.rollback_to(v1.release_id)
        assert result.release_id == v1.release_id
        assert result.backup_dir.is_dir()
        # 文件级：工作区与 v1 冻结清单完全一致（含模板资产与标定）
        assert verify_directory(project_dir, manifest_v1.files) == []
        # 对象级：五类对象哈希与 v1 清单一致
        catalog = build_object_catalog(load_project(project_dir))
        for category, entries in catalog.items():
            assert {e.id: e.sha256 for e in entries} == {
                e.id: e.sha256 for e in manifest_v1.objects[category]
            }, category
        bundle = load_project(project_dir)
        assert bundle.detectors["health_bar"].threshold == 0.5  # 阈值
        assert bundle.calibrations["1920x1080-100"].dpi_percent == 100  # 标定
        assert bundle.policies["default"].max_runtime_minutes == 20  # 策略
        assert bundle.machines["main"].states["exercise"].transitions[0].when == "health_ratio.value < 0.20"  # 状态机
        assert (project_dir / "assets" / "templates" / "gate.png").read_bytes().startswith(b"\x89PNG")  # 资产存在
        assert content_hash(project_dir / "assets" / "templates" / "gate.png") == manifest_v1.assets["assets/templates/gate.png"]
        assert not (project_dir / "detectors" / "rogue_bar.yaml").exists()  # v2 新增对象被清除

    def test_rollback_preserves_history(self, two_versions, project_dir: Path) -> None:
        """AC-P0-12：v2 历史仍可列出与审计（不删除发布记录）。"""
        publisher, rollback, v1, _manifest_v1, v2 = two_versions
        rollback.rollback_to(v1.release_id)
        ids = [r.release_id for r in publisher.list_releases()]
        assert ids == [v1.release_id, v2.release_id]
        manifest_v2 = publisher.load_release(v2.release_id)
        assert manifest_v2.find_object("detectors", "rogue_bar") is not None  # v2 内容仍可审计

    def test_rollback_rejects_tampered_release(self, two_versions, project_dir: Path) -> None:
        """发布目录被篡改时拒绝回滚（回滚来源必须可信）。"""
        publisher, rollback, v1, _m1, _v2 = two_versions
        target = project_dir.parent / "releases" / v1.release_id / "detectors" / "ready_button.yaml"
        os.chmod(target, 0o644)
        target.write_text("schema_version: 1\ntampered: true\n", encoding="utf-8")
        with pytest.raises(ReleaseError, match="完整性"):
            rollback.rollback_to(v1.release_id)

    def test_upgrade_failure_restores_workspace(self, project_dir: Path, clock: FakeClock) -> None:
        """AC-P0-14：迁移中途失败 -> 原状恢复、可再次打开、备份存在、诊断含失败步骤。"""
        publisher = make_publisher(project_dir, clock)
        publisher.publish()
        rollback = ReleaseRollback(project_dir, project_dir.parent / "releases", clock=clock)
        files_before = freeze_directory(project_dir)

        def failing_migration(root: Path) -> None:
            (root / "detectors" / "half_migrated.yaml").write_text("schema_version: 1\n", encoding="utf-8")
            raise MigrationStepError("2-rename-fields", "字段重命名冲突")

        result = rollback.upgrade_with_backup(failing_migration)
        assert result.ok is False
        assert result.restored is True
        assert result.backup_dir.is_dir() and (result.backup_dir / "backup.json").is_file()
        assert "2-rename-fields" in "\n".join(result.diagnostics)
        assert not (project_dir / "detectors" / "half_migrated.yaml").exists()  # 无半迁移对象
        bundle = load_project(project_dir)  # 原项目可再次打开
        assert "half_migrated" not in bundle.detectors
        assert verify_directory(project_dir, files_before) == []

    def test_upgrade_generic_exception_also_restores(self, project_dir: Path, clock: FakeClock) -> None:
        """非约定异常同样触发恢复，失败步骤记为 unspecified。"""
        publisher = make_publisher(project_dir, clock)
        publisher.publish()
        rollback = ReleaseRollback(project_dir, project_dir.parent / "releases", clock=clock)

        def broken_migration(root: Path) -> None:
            (root / "policies" / "default.yaml").write_text("mode: bogus\n", encoding="utf-8")
            raise RuntimeError("boom")

        result = rollback.upgrade_with_backup(broken_migration)
        assert result.ok is False and result.restored is True
        assert "unspecified" in "\n".join(result.diagnostics)
        assert load_project(project_dir).policies["default"].mode_value.value == "shadow"

    def test_upgrade_success_keeps_backup(self, project_dir: Path, clock: FakeClock) -> None:
        """升级成功：备份保留、迁移结果生效。"""
        publisher = make_publisher(project_dir, clock)
        publisher.publish()
        rollback = ReleaseRollback(project_dir, project_dir.parent / "releases", clock=clock)

        def migration(root: Path) -> None:
            (root / "detectors" / "added_by_migration.yaml").write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 1,
                        "detector_id": "added_by_migration",
                        "type": "color_region",
                        "roi": [0.1, 0.5, 0.2, 0.2],
                        "threshold": 0.5,
                        "stable_frames": 1,
                        "field_name": "migrated_flag",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

        result = rollback.upgrade_with_backup(migration)
        assert result.ok is True and result.restored is False
        assert result.backup_dir.is_dir()
        assert "added_by_migration" in load_project(project_dir).detectors

    def test_compat_check_blocks_incompatible_rollback(self, project_dir: Path, clock: FakeClock) -> None:
        """VER-010：SemVer 范围检查，不兼容拒绝并给出迁移建议文案。"""
        publisher = make_publisher(project_dir, clock)
        record = publisher.publish(runtime_compat=RuntimeCompat(min_runtime="2.0.0", max_runtime="3.0.0"))
        manifest = publisher.load_release(record.release_id)
        assert compat_check(manifest, "2.5.0") is True
        assert compat_check(manifest, "1.9.9") is False
        assert compat_check(manifest, "3.0.1") is False
        assert compat_check(manifest, "3.0.0") is True  # 闭区间上限
        message = compat_message(manifest, "1.9.9")
        assert "2.0.0" in message and "迁移建议" in message

        rollback = ReleaseRollback(project_dir, project_dir.parent / "releases", clock=clock)
        with pytest.raises(CompatError, match="迁移建议"):
            rollback.rollback_to(record.release_id, current_runtime="1.9.9")
        result = rollback.rollback_to(record.release_id, current_runtime="2.5.0")  # 兼容则放行
        assert result.release_id == record.release_id

    def test_compat_invalid_version_string_rejected(self, project_dir: Path, clock: FakeClock) -> None:
        """版本串非法 -> 明确错误（不静默放行）。"""
        publisher = make_publisher(project_dir, clock)
        record = publisher.publish(runtime_compat=RuntimeCompat(min_runtime="1.0.0"))
        manifest = publisher.load_release(record.release_id)
        with pytest.raises(ReleaseError, match="语义化版本"):
            compat_check(manifest, "not-a-version")

    def test_rollback_missing_release_raises(self, project_dir: Path, clock: FakeClock) -> None:
        publisher = make_publisher(project_dir, clock)
        publisher.publish()
        rollback = ReleaseRollback(project_dir, project_dir.parent / "releases", clock=clock)
        with pytest.raises(ReleaseError, match="不存在"):
            rollback.rollback_to("v99-00000000")
