"""security_kit 单测（E14：SEC-002/004/005/006/007 完整版）。

覆盖：
- path_guard：各路径规则正反例、symlink 逃逸（tmp 内造 symlink/junction）、
  zip bomb（高压缩比）、安全解包"先全量预检后落盘"；
- integrity：建基线 -> 改 -> 验（报告被改/缺失/多余）-> ensure_untouched 抛错，
  以及发布包预检（manifest/版本/哈希）；
- privacy：遮罩后区域像素恒为填充色、thumbnail_safe 组合、MaskedPixels
  导出强制、Frame 往返 meta 保留；
- retention：过期删除/未过期保留/受保护不删/dry_run 无副作用/审计完整；
- sanitize：中文用户名主目录替换、递归 payload、trace 脱敏链重算可 verify、
  diagnostic_bundle 无原图。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

from capture_api.frames import Frame, FrameMeta
from common.clock import FakeClock
from security_kit import (
    AssetIntegrity,
    IntegrityError,
    MaskedPixels,
    PathGuardError,
    PrivacyMask,
    RetentionPolicy,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    SizeLimits,
    StorageLayout,
    TamperReport,
    assume_masked,
    build_hashes,
    check_entry_path,
    diagnostic_bundle,
    enforce,
    ensure_untouched,
    export_png,
    inspect_release_pack,
    precheck_zip,
    protected_paths,
    safe_extract_zip,
    safe_resolve,
    sanitize_event_payload,
    sanitize_text,
    sanitize_trace,
    verify_assets,
)
from trace_format import GENESIS_HASH, JsonlTraceReader, JsonlTraceWriter, verify

#: 保留策略测试用的固定纪元秒（与文件 mtime 同单位）。
EPOCH: float = 1_700_000_000.0

DAY: float = 86_400.0


# ----------------------------------------------------------------------
# 夹具辅助
# ----------------------------------------------------------------------


def make_zip(path: Path, entries: dict[str, bytes], *, compress: bool = True) -> Path:
    """构造真实 zip 夹具。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(path, "w", method) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return path


def make_release_pack(
    path: Path,
    files: dict[str, bytes],
    manifest: dict[str, object] | bytes | str | None,
) -> Path:
    """构造发布包 zip；manifest 传 dict 时自动补 files 哈希，None 时不写入。返回 zip 路径。"""
    if isinstance(manifest, dict) and "files" not in manifest:
        manifest = {
            **manifest,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        }
    entries: dict[str, bytes] = dict(files)
    if isinstance(manifest, dict):
        entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    elif isinstance(manifest, (bytes, str)):
        entries["manifest.json"] = manifest.encode("utf-8") if isinstance(manifest, str) else manifest
    return make_zip(path, entries)


def make_frame(pixels: np.ndarray, seq: int = 0) -> Frame:
    """构造 (H, W, 3) uint8 测试帧。"""
    meta = FrameMeta(
        seq=seq,
        ts_monotonic=float(seq),
        adapter="test",
        source_width=int(pixels.shape[1]),
        source_height=int(pixels.shape[0]),
    )
    return Frame(pixels, meta)


def make_link(target: Path, link: Path) -> bool:
    """创建 symlink（或 Windows junction 兜底）；失败返回 False 供跳过。"""
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
        if link.exists():
            return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        try:
            import _winapi

            # CPython 约定：CreateJunction(src, dst) 在 dst 处建指向 src 的 junction
            _winapi.CreateJunction(str(target), str(link))
            return link.exists()
        except OSError:
            return False
    return False


def rule_of(name: str) -> str:
    """取 check_entry_path 命中的规则 ID（无问题时报错）。"""
    issue = check_entry_path(name)
    assert issue is not None, f"预期 {name!r} 被拒绝，但通过了"
    return issue.rule_id


# ======================================================================
# SEC-002 path_guard：字符串规则
# ======================================================================


def test_reject_absolute_drive_path() -> None:
    """盘符绝对路径（C:/ 与 C:\\ 两种写法）一律 absolute_path。"""
    assert rule_of("C:/Windows/system32/evil.png") == "absolute_path"
    assert rule_of("C:\\temp\\evil.png") == "absolute_path"


def test_reject_absolute_unc_and_posix() -> None:
    """UNC 与 POSIX 绝对路径拒绝。"""
    assert rule_of("\\\\server\\share\\x.png") == "absolute_path"
    assert rule_of("//server/share/x.png") == "absolute_path"
    assert rule_of("/etc/passwd") == "absolute_path"


def test_reject_parent_escape_any_depth() -> None:
    """任意层级的 .. 段（Zip Slip）拒绝。"""
    assert rule_of("../evil.png") == "parent_escape"
    assert rule_of("a/b/../../../c.png") == "parent_escape"
    assert rule_of("..\\..\\evil.png") == "parent_escape"


def test_reject_device_paths_and_reserved_names() -> None:
    """设备路径前缀与保留设备名（含带扩展名形式）拒绝。"""
    assert rule_of("\\\\?\\C:\\x\\y.png") == "device_path"
    assert rule_of("\\\\.\\PhysicalDrive0") == "device_path"
    for name in ("CON", "con.txt", "NUL", "COM1", "com5.bin", "LPT9.log", "aux"):
        assert rule_of(name) == "device_path", name


def test_reject_illegal_paths() -> None:
    """空路径、控制符、非法字符、空段与结尾点/空格拒绝。"""
    assert rule_of("") == "illegal_path"
    assert rule_of("   ") == "illegal_path"
    assert rule_of("a<b.png") == "illegal_path"
    assert rule_of("a|b.png") == "illegal_path"
    assert rule_of("a\x00b.png") == "illegal_path"
    assert rule_of("a//b.png") == "illegal_path"
    assert rule_of("name.") == "illegal_path"
    assert rule_of("name ") == "illegal_path"


def test_overlong_path_warning_semantics(tmp_path: Path) -> None:
    """>260 警告级：check_entry_path 报 warning；precheck 放行记录、解包策略拒绝落盘。"""
    long_name = "d/" + "a" * 300 + ".png"
    issue = check_entry_path(long_name)
    assert issue is not None and issue.rule_id == "path_too_long"
    assert issue.severity == SEVERITY_WARNING
    root = Path(".")  # 不落盘，仅解析字符串规则
    resolved = safe_resolve(root, "ok.png")  # 常规路径不受影响
    assert resolved.is_absolute()

    archive = make_zip(tmp_path / "warn.zip", {long_name: b"x", "ok.yaml": b"y"})
    pre = precheck_zip(archive)
    assert pre.ok is True, "预检口径：警告级不算失败，但必须记录"
    assert pre.warning_rules == ["path_too_long"]
    dest = tmp_path / "dest"
    report = safe_extract_zip(archive, dest)
    assert report.ok is False, "解包口径：任何问题（含警告）都不落盘"
    assert "path_too_long" in report.warning_rules
    assert report.extracted == []
    assert not dest.exists()


def test_overlong_path_beyond_hard_limit_is_error() -> None:
    """>4096 硬上限为错误级，safe_resolve 抛 PathGuardError。"""
    long_name = "d/" + "a" * 4200 + ".png"
    issue = check_entry_path(long_name)
    assert issue is not None and issue.rule_id == "path_too_long"
    assert issue.severity == SEVERITY_ERROR


def test_normal_paths_pass_check() -> None:
    """常规相对路径（含目录条目）通过。"""
    assert check_entry_path("assets/templates/btn.png") is None
    assert check_entry_path("machines/main.yaml") is None
    assert check_entry_path("sub/dir/") is None


def test_safe_resolve_keeps_inside_root(tmp_path: Path) -> None:
    """safe_resolve 正常路径解析到 root 内。"""
    root = tmp_path / "root"
    root.mkdir()
    resolved = safe_resolve(root, "a/b.png")
    assert resolved == (root / "a" / "b.png").resolve()
    assert root.resolve() in resolved.parents


@pytest.mark.parametrize("bad", ["../x.png", "a/../../x.png", "C:/x.png", "con"])
def test_safe_resolve_rejects(tmp_path: Path, bad: str) -> None:
    """safe_resolve 对错误级规则一律抛 PathGuardError。"""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathGuardError):
        safe_resolve(root, bad)


def test_symlink_escape_detected(tmp_path: Path) -> None:
    """root 内符号链接指向外部时，safe_resolve 报 symlink_escape。"""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    if not make_link(outside, root / "link"):
        pytest.skip("当前环境无法创建符号链接/junction")
    with pytest.raises(PathGuardError) as excinfo:
        safe_resolve(root, "link/secret.txt")
    assert excinfo.value.rule_id == "symlink_escape"


# ======================================================================
# SEC-002 path_guard：安全解包
# ======================================================================


def test_safe_extract_zip_happy_path(tmp_path: Path) -> None:
    """正常包：解压成功、目录结构保持、审计完整。"""
    archive = make_zip(
        tmp_path / "pack.zip",
        {
            "assets/a.png": b"\x89PNG\r\n\x1a\nfake",
            "assets/sub/b.yaml": b"k: v\n",
            "machines/main.yaml": b"steps: []\n",
        },
    )
    dest = tmp_path / "dest"
    report: ExtractReport = safe_extract_zip(archive, dest)
    assert report.ok is True
    assert report.error_rules == []
    assert sorted(report.extracted) == ["assets/a.png", "assets/sub/b.yaml", "machines/main.yaml"]
    assert (dest / "assets" / "a.png").is_file()
    assert (dest / "assets" / "sub" / "b.yaml").is_file()
    assert report.bytes_written > 0
    assert report.entries_checked == 3


def test_safe_extract_zip_rejects_traversal_without_any_write(tmp_path: Path) -> None:
    """含 ../ 条目的包：整体拒绝且解压目录零写入（先全量预检后落盘）。"""
    archive = make_zip(
        tmp_path / "evil.zip",
        {"assets/good.png": b"data", "../evil.txt": b"boom"},
    )
    dest = tmp_path / "dest"
    report = safe_extract_zip(archive, dest)
    assert report.ok is False
    assert "parent_escape" in report.error_rules
    assert report.extracted == []
    assert not dest.exists(), "违规包不应创建解压目录"


def test_zip_bomb_high_ratio_rejected(tmp_path: Path) -> None:
    """高压缩比 zip bomb（50MB 全零 -> 压缩比 ~1000:1）被拒绝，零写入。"""
    archive = make_zip(tmp_path / "bomb.zip", {"bomb.bin": b"\x00" * 50_000_000})
    dest = tmp_path / "dest"
    report = safe_extract_zip(archive, dest, SizeLimits(max_compression_ratio=200.0))
    assert report.ok is False
    assert "compression_ratio_abuse" in report.error_rules
    assert not dest.exists()


def test_zip_total_size_exceeded(tmp_path: Path) -> None:
    """累计大小超限拒绝（重复文件资源耗尽向量），零写入。"""
    archive = make_zip(
        tmp_path / "big.zip",
        {"a.bin": b"A" * 1000, "b.bin": b"B" * 1000},
    )
    limits = SizeLimits(max_file_bytes=1_000_000, max_total_bytes=1500, max_compression_ratio=10_000)
    report = safe_extract_zip(archive, tmp_path / "dest", limits)
    assert report.ok is False
    assert "total_size_exceeded" in report.error_rules
    assert not (tmp_path / "dest").exists()


def test_zip_single_file_too_large(tmp_path: Path) -> None:
    """单文件超限拒绝。"""
    archive = make_zip(tmp_path / "one.zip", {"huge.bin": b"X" * 2048})
    limits = SizeLimits(max_file_bytes=1024, max_compression_ratio=10_000)
    report = safe_extract_zip(archive, tmp_path / "dest", limits)
    assert report.ok is False
    assert "file_too_large" in report.error_rules


def test_corrupt_zip_fails_safe(tmp_path: Path) -> None:
    """损坏 zip：corrupt_zip 报告，安全失败零写入。"""
    archive = tmp_path / "broken.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"PK\x03\x04 not really a zip file")
    report = safe_extract_zip(archive, tmp_path / "dest")
    assert report.ok is False
    assert report.error_rules == ["corrupt_zip"]
    assert not (tmp_path / "dest").exists()


def test_zip_executable_payload_blocked(tmp_path: Path) -> None:
    """黑名单扩展名（.bat）拒绝，正常文件也不落盘（全有或全无）。"""
    archive = make_zip(
        tmp_path / "payload.zip",
        {"assets/ok.png": b"data", "scripts/run.bat": b"echo pwned"},
    )
    dest = tmp_path / "dest"
    report = safe_extract_zip(archive, dest)
    assert report.ok is False
    assert "executable_payload" in report.error_rules
    assert report.extracted == []
    assert not dest.exists()


def test_precheck_zip_reports_but_never_writes(tmp_path: Path) -> None:
    """precheck_zip 只读不写：违规包给出规则清单，dest 概不创建。"""
    archive = make_zip(
        tmp_path / "mixed.zip",
        {"../escape.png": b"x", "run.ps1": b"y"},
    )
    report = precheck_zip(archive)
    assert report.ok is False
    assert set(report.error_rules) == {"parent_escape", "executable_payload"}
    assert report.extracted == []
    assert not (tmp_path / "dest").exists()


# ======================================================================
# SEC-004 integrity：资产基线与篡改检测
# ======================================================================


def _make_project(tmp_path: Path) -> Path:
    """构造带 assets 的项目目录。"""
    project = tmp_path / "proj"
    (project / "assets" / "templates").mkdir(parents=True)
    (project / "assets" / "templates" / "btn.png").write_bytes(b"\x89PNG fake-1")
    (project / "assets" / "logo.png").write_bytes(b"\x89PNG fake-2")
    return project


def test_build_then_verify_clean(tmp_path: Path) -> None:
    """建基线 -> 未改动 -> verify 为空，ensure_untouched 不抛。"""
    project = _make_project(tmp_path)
    baseline = build_hashes(project)
    assert set(baseline["entries"]) == {"assets/templates/btn.png", "assets/logo.png"}
    assert (project / "hashes.json").is_file()
    assert verify_assets(project) == []
    ensure_untouched(project)  # 不抛即通过
    facade = AssetIntegrity(project)
    facade.ensure_untouched()


def test_detect_modified_asset_and_raise(tmp_path: Path) -> None:
    """篡改模板内容 -> verify 报 modified；ensure_untouched 抛 IntegrityError。"""
    project = _make_project(tmp_path)
    build_hashes(project)
    (project / "assets" / "templates" / "btn.png").write_bytes(b"\x89PNG TAMPERED")
    reports = verify_assets(project)
    statuses = {r.path: r.status for r in reports}
    assert statuses["assets/templates/btn.png"] == "modified"
    modified = next(r for r in reports if r.status == "modified")
    assert modified.expected_sha256 is not None and modified.actual_sha256 is not None
    assert modified.expected_sha256 != modified.actual_sha256
    with pytest.raises(IntegrityError) as excinfo:
        ensure_untouched(project)
    assert excinfo.value.reports  # 异常携带逐条报告


def test_detect_missing_and_extra_assets(tmp_path: Path) -> None:
    """删除资产报 missing；新增资产报 extra。"""
    project = _make_project(tmp_path)
    build_hashes(project)
    (project / "assets" / "logo.png").unlink()
    (project / "assets" / "extra.yaml").write_text("k: v\n", encoding="utf-8")
    statuses = {r.path: r.status for r in verify_assets(project)}
    assert statuses["assets/logo.png"] == "missing"
    assert statuses["assets/extra.yaml"] == "extra"


def test_mtime_change_is_not_tamper(tmp_path: Path) -> None:
    """mtime 不参与篡改判定：仅 touch（内容不变）不报异常。"""
    project = _make_project(tmp_path)
    build_hashes(project)
    target = project / "assets" / "logo.png"
    st = target.stat()
    os.utime(target, (st.st_atime + 5000, st.st_mtime + 5000))
    assert verify_assets(project) == []


def test_no_baseline_is_reported(tmp_path: Path) -> None:
    """未建基线：verify 报 no_baseline，ensure_untouched 拒绝。"""
    project = _make_project(tmp_path)
    reports: list[TamperReport] = verify_assets(project)
    assert len(reports) == 1 and reports[0].status == "no_baseline"
    with pytest.raises(IntegrityError):
        ensure_untouched(project)


# ======================================================================
# SEC-004 integrity：发布包预检（manifest/版本/哈希）
# ======================================================================


def test_inspect_release_pack_ok(tmp_path: Path) -> None:
    """合法发布包：预检零问题。"""
    files = {"assets/a.png": b"png-bytes", "machines/main.yaml": b"steps: []\n"}
    pack = make_release_pack(
        tmp_path / "rel.zip", files, {"schema_version": 1, "version": "1.2.0"}
    )
    assert inspect_release_pack(pack) == []


def test_inspect_release_pack_missing_manifest(tmp_path: Path) -> None:
    """伪造包（缺 manifest.json）-> manifest_missing。"""
    pack = make_zip(tmp_path / "pack.zip", {"assets/a.png": b"x"})
    issues = inspect_release_pack(pack)
    assert [i.rule_id for i in issues] == ["manifest_missing"]


@pytest.mark.parametrize("raw", [b"not json", json.dumps({"files": {}}).encode()])
def test_inspect_release_pack_invalid_manifest(tmp_path: Path, raw: bytes) -> None:
    """manifest 非法 JSON / 缺 version -> manifest_invalid。"""
    pack = make_release_pack(tmp_path / "pack.zip", {"assets/a.png": b"x"}, raw)
    assert [i.rule_id for i in inspect_release_pack(pack)] == ["manifest_invalid"]


def test_inspect_release_pack_duplicate_version(tmp_path: Path) -> None:
    """重复版本号：目标版本目录已存在 -> duplicate_version。"""
    pack = make_release_pack(
        tmp_path / "pack.zip", {"assets/a.png": b"x"}, {"version": "1.2.0"}
    )
    releases = tmp_path / "releases"
    (releases / "1.2.0").mkdir(parents=True)
    assert [i.rule_id for i in inspect_release_pack(pack, releases_dir=releases)] == [
        "duplicate_version"
    ]
    # 未提供 releases_dir 时不做版本查重
    assert inspect_release_pack(pack) == []


def test_inspect_release_pack_tampered_hash(tmp_path: Path) -> None:
    """篡改哈希：包内容与 manifest 声明不符 -> hash_mismatch。"""
    content = b"png-bytes"
    manifest = {
        "version": "1.0.0",
        "files": {"assets/a.png": hashlib.sha256(b"different").hexdigest()},
    }
    pack = make_release_pack(tmp_path / "pack.zip", {"assets/a.png": content}, manifest)
    issues = inspect_release_pack(pack)
    assert [i.rule_id for i in issues] == ["hash_mismatch"]
    assert "篡改" in issues[0].message


# ======================================================================
# SEC-005 privacy：遮罩与导出强制
# ======================================================================


def gradient_image() -> np.ndarray:
    """确定性渐变测试图 (60, 80, 3)。"""
    h, w = 60, 80
    yy, xx = np.mgrid[0:h, 0:w]
    return np.stack(
        [(xx * 3 % 256).astype(np.uint8), (yy * 4 % 256).astype(np.uint8), np.full((h, w), 7, np.uint8)],
        axis=2,
    )


def configured_mask() -> PrivacyMask:
    """双遮罩配置：左上区块 + 右侧竖条。"""
    mask = PrivacyMask()
    mask.add_fixed_mask("chat", (0.0, 0.0, 0.5, 0.5))
    mask.add_fixed_mask("account", (0.75, 0.1, 0.2, 0.6))
    return mask


def test_apply_fills_region_and_keeps_rest() -> None:
    """遮罩区恒为填充色，区域外像素保持原样，输入数组不被修改。"""
    pixels = gradient_image()
    original = pixels.copy()
    masked = configured_mask().apply(pixels)
    assert isinstance(masked, MaskedPixels)
    # 填充区（左上 50% x 50% 像素域 [0:30, 0:40]）
    assert np.all(masked.pixels[:30, :40] == (0, 0, 0))
    # 区域外（右下角）保持原图
    assert np.all(masked.pixels[45:, 55:] == original[45:, 55:])
    # 不可绕过：原数组不变
    assert np.array_equal(pixels, original)


def test_apply_custom_fill_color() -> None:
    """可指定非黑填充色，且统计恒为该色。"""
    masked = configured_mask().apply(gradient_image(), fill=(12, 34, 56))
    assert np.all(masked.pixels[:30, :40] == (12, 34, 56))
    masked.verify_fill()


def test_apply_without_masks_rejected() -> None:
    """零遮罩时 apply 拒绝（防止空遮罩把明文伪装成已遮罩）。"""
    with pytest.raises(ValueError):
        PrivacyMask().apply(gradient_image())


def test_add_fixed_mask_validation() -> None:
    """ROI 非法/重复名拒绝；越界 ROI 被夹取。"""
    mask = PrivacyMask()
    with pytest.raises(ValueError):
        mask.add_fixed_mask("bad", (0.0, 0.0, 0.0, 0.5))  # 宽为 0
    with pytest.raises(ValueError):
        mask.add_fixed_mask("bad", (2.0, 0.0, 0.1, 0.1))  # 完全越界
    mask.add_fixed_mask("a", (0.9, 0.9, 0.5, 0.5))  # 越界部分被裁剪
    with pytest.raises(ValueError):
        mask.add_fixed_mask("a", (0.0, 0.0, 0.1, 0.1))  # 重复名
    assert mask.mask_names == ("a",)


def test_masked_pixels_array_is_readonly() -> None:
    """MaskedPixels 内部数组只读，防原地改回原文。"""
    masked = configured_mask().apply(gradient_image())
    with pytest.raises(ValueError):
        masked.pixels[0, 0, 0] = 255
    # 可写副本正常
    copy = masked.writable_copy()
    copy[0, 0, 0] = 255
    assert copy is not masked.pixels


def test_apply_to_frame_preserves_meta(tmp_path: Path) -> None:
    """Frame 往返：meta 原样保留（同一对象），pixels 被替换为遮罩结果。"""
    frame = make_frame(gradient_image(), seq=7)
    out = configured_mask().apply_to_frame(frame)
    assert isinstance(out, Frame)
    assert out.meta is frame.meta
    assert out.meta.seq == 7
    assert out.pixels is not frame.pixels
    assert np.all(out.pixels[:30, :40] == (0, 0, 0))
    assert np.array_equal(frame.pixels, gradient_image())  # 原帧不受影响


def test_thumbnail_safe_masks_before_and_after_scaling() -> None:
    """thumbnail_safe：先遮罩再缩放再遮罩，缩略图尺寸受限且区域恒为填充色。"""
    mask = PrivacyMask()
    mask.add_fixed_mask("left", (0.0, 0.0, 0.5, 1.0))  # 左半屏全遮
    thumb = mask.thumbnail_safe(gradient_image(), max_size=40)
    h, w = thumb.shape[0], thumb.shape[1]
    assert max(h, w) <= 40
    # 左半屏（含缩放插值边界）恒为填充色
    assert np.all(thumb.pixels[:, : w // 2] == (0, 0, 0))
    thumb.verify_fill()


def test_mask_thumbnail_alias_matches() -> None:
    """mask_thumbnail 与 thumbnail_safe 等价。"""
    mask = configured_mask()
    a = mask.thumbnail_safe(gradient_image(), max_size=32)
    b = mask.mask_thumbnail(gradient_image(), max_size=32)
    assert np.array_equal(a.pixels, b.pixels)


def test_export_png_requires_masked_pixels(tmp_path: Path) -> None:
    """导出强制：普通 ndarray 抛 TypeError，MaskedPixels 才可导出。"""
    out = tmp_path / "shot.png"
    with pytest.raises(TypeError):
        export_png(gradient_image(), out)  # type: ignore[arg-type]
    assert not out.exists()
    masked = configured_mask().apply(gradient_image())
    path = export_png(masked, out)
    assert path.is_file() and out.read_bytes().startswith(b"\x89PNG")


def test_assume_masked_explicit_opt_in(tmp_path: Path) -> None:
    """普通 ndarray 必须经 assume_masked 显式声明才能导出。"""
    out = tmp_path / "explicit.png"
    marked = assume_masked(gradient_image(), reason="unit-test fixture")
    assert marked.provenance.startswith("assume_masked")
    assert export_png(marked, out).is_file()


def test_wrong_pixel_shape_rejected() -> None:
    """非 (H, W, 3) uint8 输入拒绝。"""
    with pytest.raises(ValueError):
        PrivacyMask().apply(np.zeros((4, 4), dtype=np.uint8))
    with pytest.raises(ValueError):
        assume_masked(np.zeros((4, 4, 4), dtype=np.float32))


# ======================================================================
# SEC-006 retention：保留策略与安全清理
# ======================================================================


def make_workspace(tmp_path: Path) -> StorageLayout:
    """构造工作区：traces/snapshots/frames/logs 各放新旧两个文件。"""
    root = tmp_path / "workspace"
    layout = StorageLayout(root=root)
    for sub in ("traces", "snapshots", "frames", "logs"):
        (root / sub).mkdir(parents=True)
        old = root / sub / "old.bin"
        new = root / sub / "new.bin"
        old.write_bytes(b"old")
        new.write_bytes(b"new")
        old_time = EPOCH - 100 * DAY
        os.utime(old, (old_time, old_time))  # 100 天前
    return layout


def test_expired_deleted_fresh_kept(tmp_path: Path) -> None:
    """过期删除、未过期保留。"""
    layout = make_workspace(tmp_path)
    report: CleanupReport = enforce(
        layout, FakeClock(EPOCH), RetentionPolicy(), dry_run=False
    )
    for sub in ("traces", "snapshots", "frames", "logs"):
        assert (layout.root / sub / "old.bin").exists() is False, sub
        assert (layout.root / sub / "new.bin").exists() is True, sub
    assert len(report.deleted) == 4
    assert report.protected_kept == []
    assert report.errors == []
    assert report.scanned == 8


def test_per_category_days(tmp_path: Path) -> None:
    """按类别天数：4 天前的帧（>3 天）删、4 天前的轨迹（<7 天）留。"""
    layout = StorageLayout(root=tmp_path)
    for sub in ("traces", "frames"):
        (layout.root / sub).mkdir(parents=True)
        f = layout.root / sub / "four_days.bin"
        f.write_bytes(b"x")
        t = EPOCH - 4 * DAY
        os.utime(f, (t, t))
    policy = RetentionPolicy(trace_days=7, snapshot_days=14, frame_store_days=3)
    report = enforce(layout, FakeClock(EPOCH), policy, dry_run=False)
    assert (layout.root / "traces" / "four_days.bin").exists()
    assert not (layout.root / "frames" / "four_days.bin").exists()
    assert [p.name for p in report.deleted] == ["four_days.bin"]
    assert report.deleted[0].parent.name == "frames"


def test_protected_baseline_not_deleted(tmp_path: Path) -> None:
    """被 releases manifest 引用的数据不得删除。"""
    layout = make_workspace(tmp_path)
    project = layout.root
    release_dir = project / "releases" / "v1"
    release_dir.mkdir(parents=True)
    (release_dir / "manifest.json").write_text(
        json.dumps({"version": "v1", "protected": ["traces/old.bin"]}), encoding="utf-8"
    )
    protected = protected_paths(project)
    assert (project / "traces" / "old.bin").resolve() in protected
    report = enforce(
        layout, FakeClock(EPOCH), RetentionPolicy(), dry_run=False, project_dir=project
    )
    assert (project / "traces" / "old.bin").exists(), "受保护文件必须保留"
    assert (project / "traces" / "old.bin").resolve() in [p.resolve() for p in report.protected_kept]
    assert all(p.name != "old.bin" or p.parent.name != "traces" for p in report.deleted)


def test_broken_manifest_protects_conservatively(tmp_path: Path) -> None:
    """manifest 损坏：保守起见全部保留（宁可不删不误删）。"""
    layout = make_workspace(tmp_path)
    project = layout.root
    release_dir = project / "releases" / "v1"
    release_dir.mkdir(parents=True)
    (release_dir / "manifest.json").write_bytes(b"{broken json")
    report = enforce(
        layout, FakeClock(EPOCH), RetentionPolicy(), dry_run=False, project_dir=project
    )
    assert report.deleted == []
    assert len(report.protected_kept) == 4


def test_dry_run_has_no_side_effect(tmp_path: Path) -> None:
    """干跑：报告"将删除"清单但零副作用。"""
    layout = make_workspace(tmp_path)
    report = enforce(layout, FakeClock(EPOCH), RetentionPolicy(), dry_run=True)
    assert report.dry_run is True
    assert len(report.deleted) == 4
    for sub in ("traces", "snapshots", "frames", "logs"):
        assert (layout.root / sub / "old.bin").exists(), sub
    # 默认即干跑
    assert enforce(layout, FakeClock(EPOCH), RetentionPolicy()).dry_run is True


def test_cleanup_audit_lists_are_accurate(tmp_path: Path) -> None:
    """审计清单：deleted/protected_kept/errors/scanned 与实际一致。"""
    layout = make_workspace(tmp_path)
    (layout.root / "traces" / "keep.jsonl").write_bytes(b"keep")
    keep_time = EPOCH - 1 * DAY
    os.utime(layout.root / "traces" / "keep.jsonl", (keep_time, keep_time))
    report = enforce(
        layout, FakeClock(EPOCH), RetentionPolicy(trace_days=7), dry_run=False
    )
    deleted_names = sorted(p.as_posix().split("/")[-2:] for p in report.deleted)
    assert deleted_names == sorted(
        [[sub, "old.bin"] for sub in ("frames", "logs", "snapshots", "traces")]
    )
    assert report.scanned == 9  # 4 新 + 4 旧 + 1 keep
    assert not any(p.name == "keep.jsonl" for p in report.deleted)
    assert report.errors == []


def test_prune_empty_subdirs_after_cleanup(tmp_path: Path) -> None:
    """清理后剪除空子目录（托管目录本身保留）。"""
    layout = StorageLayout(root=tmp_path)
    nested = layout.root / "traces" / "sess-1"
    nested.mkdir(parents=True)
    f = nested / "trace.jsonl"
    f.write_bytes(b"x")
    t = EPOCH - 30 * DAY
    os.utime(f, (t, t))
    enforce(layout, FakeClock(EPOCH), RetentionPolicy(), dry_run=False)
    assert not nested.exists()
    assert layout.traces_dir.exists()


# ======================================================================
# SEC-007 sanitize：文本、payload、轨迹与诊断包
# ======================================================================


def test_sanitize_home_path_with_chinese_username() -> None:
    """本机中文用户名主目录（C:/Users/顾柯鹏）被替换为 <user>，两种分隔符。"""
    home = Path.home()  # 本机即 C:\\Users\\顾柯鹏
    for sample in (
        str(home / "Desktop" / "z.png"),
        str(home / "Desktop" / "z.png").replace("\\", "/"),
    ):
        out = sanitize_text(sample)
        assert out.startswith("<user>")
        assert "顾柯鹏" not in out
        assert out.endswith("z.png")


def test_sanitize_tilde_prefix() -> None:
    """~ 与 ~/ 前缀替换。"""
    assert sanitize_text("~/secret/key.bin") == "<user>/secret/key.bin"
    assert sanitize_text("plain-token") == "plain-token"


def test_sanitize_drive_path_keeps_filename() -> None:
    """非主目录盘符路径 -> <path>/文件名。"""
    assert sanitize_text("D:/work/report.xlsx") == "<path>/report.xlsx"
    assert sanitize_text("E:\\data\\trace.jsonl") == "<path>/trace.jsonl"


def test_sanitize_long_title_token() -> None:
    """长标题（含 " - "）整体 -> <title>。"""
    title = "账号 user12345 的聊天记录窗口 - 微信"
    assert len(title) >= 24
    assert sanitize_text(title) == "<title>"


def test_sanitize_title_truncate_mode() -> None:
    """标题 truncate 模式：保留前缀 + <title>（可配）。"""
    from security_kit import SanitizeConfig

    config = SanitizeConfig(title_mode="truncate", title_keep_chars=8, title_min_len=12)
    assert sanitize_text("ABCDEFGH 敏感的窗口标题 - 记事本", config) == "ABCDEFGH<title>"
    # 短文本不受标题启发式影响
    assert sanitize_text("idle - run", config) == "idle - run"


def test_sanitize_event_payload_recursive_and_key_blacklist() -> None:
    """payload 递归脱敏 + 键名黑名单（username/user/home/title/path/file）。"""
    payload = {
        "username": "顾柯鹏",
        "title": "任意内容都不该出现",
        "path": str(Path.home() / "AppData" / "x.bin"),
        "nested": {"home": str(Path.home()), "file": "D:/logs/a.log"},
        "items": [{"user": "someone", "note": "ok"}],
        "step": 3,
    }
    out = sanitize_event_payload(payload)
    assert out["username"] == "<user>"
    assert out["title"] == "<title>"
    assert "顾柯鹏" not in out["path"]
    assert out["nested"]["home"] == "<user>"
    assert out["nested"]["file"] == "<path>/a.log"
    assert out["items"][0]["user"] == "<user>"
    assert out["items"][0]["note"] == "ok"
    assert out["step"] == 3
    # 输入不被修改
    assert payload["username"] == "顾柯鹏"


def test_sanitize_trace_recomputes_hash_chain(tmp_path: Path) -> None:
    """轨迹脱敏：输出链重新计算哈希可 verify，且以 sanitized=true 元事件开头。"""
    src = tmp_path / "trace.jsonl"
    home = str(Path.home() / "record.bin")
    with JsonlTraceWriter(src) as writer:
        for i in range(3):
            writer.append(
                "executed",
                ts_monotonic=float(i),
                correlation_id="cid",
                session_id="sess",
                payload={
                    "step": i,
                    "path": home,
                    "title": "很长的敏感窗口标题名称示例 - 微信",
                },
            )
    report = sanitize_trace(src, tmp_path / "trace.sanitized.jsonl")
    assert report.source_events == 3
    assert report.output_events == 4

    events = JsonlTraceReader(report.output_path).read()
    assert len(events) == 4
    assert verify(events) == [], "脱敏导出链必须自洽可校验"
    meta = events[0]
    assert meta.type == "trace_sanitized"
    assert meta.payload.get("sanitized") is True
    assert meta.prev_hash == GENESIS_HASH
    assert meta.hash == report.meta_event_hash

    raw = report.output_path.read_text(encoding="utf-8")
    assert "顾柯鹏" not in raw
    assert "很长的敏感窗口标题名称示例" not in raw
    # 源文件不受影响
    assert "顾柯鹏" in src.read_text(encoding="utf-8")


def test_diagnostic_bundle_excludes_images(tmp_path: Path) -> None:
    """诊断包：不含任何 png/原图文件；排除计数准确；成员清单可校验。"""
    project = tmp_path / "proj"
    (project / "assets").mkdir(parents=True)
    (project / "assets" / "logo.png").write_bytes(b"\x89PNG-not-really")
    (project / "snapshots").mkdir()
    (project / "snapshots" / "frame.npy").write_bytes(b"\x93NUMPY")
    (project / "project.yaml").write_text(
        f"name: demo\nnotes: {Path.home() / 'work'}\n", encoding="utf-8"
    )
    (project / "logs").mkdir()
    (project / "logs" / "app.log").write_text(
        f"error at {Path.home() / 'record.bin'} - 无法读取\n", encoding="utf-8"
    )
    mask = PrivacyMask()
    mask.add_fixed_mask("chat", (0.0, 0.0, 0.5, 1.0))

    out_zip = tmp_path / "bundle.zip"
    report = diagnostic_bundle(project, out_zip, mask)
    assert out_zip.is_file()
    assert report.has_images is False
    assert report.excluded_images == 2  # logo.png + frame.npy

    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
        assert not any(Path(n).suffix.lower() in {".png", ".jpg", ".npy"} for n in names)
        # 配置与日志均已脱敏
        config_text = zf.read("diagnostic/config/project.yaml.txt").decode("utf-8")
        assert "<user>" in config_text and "顾柯鹏" not in config_text
        log_text = zf.read("diagnostic/logs/app.log.txt").decode("utf-8")
        assert "顾柯鹏" not in log_text
        # 隐私说明与校验清单
        privacy = json.loads(zf.read("diagnostic/privacy.json"))
        assert privacy["images_included"] is False
        assert privacy["masks"] == ["chat"]
        manifest = json.loads(zf.read("diagnostic/bundle_manifest.json"))
        listed = {m["name"] for m in manifest["members"]}
        assert "diagnostic/config/project.yaml.txt" in listed
        for member in manifest["members"]:
            payload = zf.read(member["name"])
            assert hashlib.sha256(payload).hexdigest() == member["sha256"]
