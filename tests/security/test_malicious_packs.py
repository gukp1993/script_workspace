"""TST-010 安全与恶意包测试集（§8.3 恶意与异常输入集，SEC-002/004 口径）。

真实构造 zip 夹具，逐类断言"拒绝规则命中 + 零侧效"：

- 路径穿越：``../`` 条目、深层逃逸（Zip Slip，§9.6 SEC-003）；
- 绝对路径：盘符 / POSIX / UNC 条目；
- 设备路径：保留设备名（CON/NUL/COM1）与 ``\\\\?\\`` 前缀；
- 超长路径：>260（警告级放行并记录）与 >4096（错误级拒绝）；
- 可执行载荷：exe / dll / bat / ps1 / py（§9.6 SEC-004：默认拒绝不执行）；
- 资源炸弹：高压缩比 zip bomb / 海量重复文件（§9.6 SEC-005：安全终止）；
- 损坏 Zip：安全失败不落盘；
- 伪造 manifest / 非法 manifest / 重复版本号 / 篡改哈希（VER-006/SEC-004 口径）；
- 解压目标内预埋符号链接/junction 的逃逸尝试；
- 全量零侧效兜底：dest 父目录在拒绝场景前后文件数不变（SEC-003：
  "导入失败，项目目录之外无写入"）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

from security_kit import (
    SizeLimits,
    check_entry_path,
    inspect_release_pack,
    precheck_zip,
    safe_extract_zip,
)


# ----------------------------------------------------------------------
# 夹具构造
# ----------------------------------------------------------------------


def make_zip(path: Path, entries: dict[str, bytes], *, compress: bool = True) -> Path:
    """构造真实 zip 夹具（默认 DEFLATED，便于构造高压缩比）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(path, "w", method) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return path


def extract_and_guard(
    tmp_path: Path, archive: Path, limits: SizeLimits | None = None, *, dirname: str = "dest"
):
    """在受监视的父目录下解包；返回 (report, dest, 解包前后父目录文件名快照)。"""
    parent = tmp_path / "sandbox"
    parent.mkdir(parents=True, exist_ok=True)
    before = sorted(p.name for p in parent.iterdir())
    dest = parent / dirname
    report = safe_extract_zip(archive, dest, limits)
    after = sorted(p.name for p in parent.iterdir())
    return report, dest, (before, after)


def assert_zero_side_effect(dest: Path, snapshot: tuple[list[str], list[str]]) -> None:
    """零侧效：dest 不存在（或为空），且父目录除 dest 外无任何新增。"""
    before, after = snapshot
    if dest.exists():
        assert dest.is_dir()
        assert list(dest.iterdir()) == [], "解压目录必须为空"
    added = [name for name in after if name not in before]
    assert added in ([], [dest.name]), f"dest 之外出现写入：{added}"


def make_release_pack(
    path: Path,
    files: dict[str, bytes],
    manifest: dict[str, object] | bytes | str | None,
) -> Path:
    """构造发布包 zip；manifest 传 dict 时自动补 files 哈希。"""
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


def make_link(target: Path, link: Path) -> bool:
    """创建 symlink（Windows junction 兜底）；失败返回 False 供跳过。"""
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
        if link.exists():
            return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return link.exists()
        except OSError:
            return False
    return False


# ======================================================================
# 路径穿越 / Zip Slip
# ======================================================================


def test_zip_slip_simple_parent_entry(tmp_path: Path) -> None:
    """SEC-003：含 ../ 条目的包被 parent_escape 拒绝且零侧效。"""
    archive = make_zip(
        tmp_path / "slip.zip",
        {"project.yaml": b"name: x\n", "../escape.txt": b"pwned"},
    )
    report, dest, snapshot = extract_and_guard(tmp_path, archive)
    assert report.ok is False
    assert "parent_escape" in report.error_rules
    assert report.extracted == []
    assert_zero_side_effect(dest, snapshot)


def test_zip_slip_deep_traversal_escape(tmp_path: Path) -> None:
    """深层 .. 逃逸（a/b/../../../evil.png）同样被拒绝。"""
    archive = make_zip(
        tmp_path / "deep.zip",
        {"a/b/../../../evil.png": b"x", "ok.yaml": b"y"},
    )
    report, dest, snapshot = extract_and_guard(tmp_path, archive)
    assert report.ok is False
    assert "parent_escape" in report.error_rules
    assert_zero_side_effect(dest, snapshot)
    # 字符串规则层也直接命中
    assert check_entry_path("a/b/../../../evil.png").rule_id == "parent_escape"


# ======================================================================
# 绝对路径条目
# ======================================================================


def test_absolute_drive_path_entry(tmp_path: Path) -> None:
    """盘符绝对路径条目（C:\\...）拒绝。"""
    archive = make_zip(tmp_path / "abs.zip", {"C:\\Windows\\evil.yaml": b"x"})
    report, dest, snapshot = extract_and_guard(tmp_path, archive)
    assert report.ok is False
    assert "absolute_path" in report.error_rules
    assert_zero_side_effect(dest, snapshot)


def test_absolute_posix_and_unc_entries(tmp_path: Path) -> None:
    """POSIX 绝对路径与 UNC 条目拒绝。"""
    archive = make_zip(
        tmp_path / "abs2.zip",
        {"/etc/shadow": b"x", "\\\\srv\\share\\evil.yaml": b"y"},
    )
    report, dest, snapshot = extract_and_guard(tmp_path, archive)
    assert report.ok is False
    assert "absolute_path" in report.error_rules
    assert report.extracted == []
    assert_zero_side_effect(dest, snapshot)


# ======================================================================
# 设备路径条目
# ======================================================================


@pytest.mark.parametrize("name", ["CON", "NUL.txt", "COM1", "LPT9.log"])
def test_reserved_device_name_entry(tmp_path: Path, name: str) -> None:
    """保留设备名条目（含带扩展名形式）拒绝。"""
    archive = make_zip(tmp_path / "dev.zip", {name: b"x"})
    report, dest, snapshot = extract_and_guard(tmp_path, archive)
    assert report.ok is False
    assert "device_path" in report.error_rules
    assert_zero_side_effect(dest, snapshot)


def test_device_path_prefix_entry(tmp_path: Path) -> None:
    """\\\\?\\ 设备路径前缀条目拒绝。"""
    archive = make_zip(tmp_path / "devp.zip", {"\\\\?\\C:\\evil\\x.yaml": b"x"})
    report, dest, snapshot = extract_and_guard(tmp_path, archive)
    assert report.ok is False
    assert "device_path" in report.error_rules
    assert_zero_side_effect(dest, snapshot)


# ======================================================================
# 超长路径
# ======================================================================


def test_overlong_path_warns_in_precheck_and_blocks_extract(tmp_path: Path) -> None:
    """>260 路径为警告级：precheck 放行并记录；解包策略整体拒绝、零侧效。"""
    long_name = "d/" + "a" * 300 + ".png"
    archive = make_zip(tmp_path / "long.zip", {long_name: b"x", "ok.yaml": b"y"})
    pre = precheck_zip(archive)
    assert pre.ok is True
    assert pre.warning_rules == ["path_too_long"]
    report, dest, snapshot = extract_and_guard(tmp_path, archive)
    assert report.ok is False
    assert "path_too_long" in report.warning_rules
    assert report.extracted == []
    assert_zero_side_effect(dest, snapshot)


def test_overlong_path_beyond_hard_limit_rejected(tmp_path: Path) -> None:
    """>4096 硬上限路径为错误级：整包拒绝、零侧效。"""
    long_name = "d/" + "a" * 4200 + ".png"
    archive = make_zip(tmp_path / "toolong.zip", {long_name: b"x", "ok.yaml": b"y"})
    report, dest, snapshot = extract_and_guard(tmp_path, archive)
    assert report.ok is False
    assert "path_too_long" in report.error_rules
    assert report.extracted == []
    assert_zero_side_effect(dest, snapshot)


# ======================================================================
# 可执行载荷（SEC-004：默认拒绝，不执行）
# ======================================================================


@pytest.mark.parametrize(
    "name",
    ["payload.exe", "lib.dll", "run.bat", "install.ps1", "script.py"],
)
def test_executable_payload_entries_rejected(tmp_path: Path, name: str) -> None:
    """exe/dll/bat/ps1/py 载荷拒绝：规则 executable_payload，零落盘。"""
    archive = make_zip(
        tmp_path / "payload.zip",
        {"assets/ok.png": b"\x89PNG", f"scripts/{name}": b"MZ fake binary"},
    )
    report, dest, snapshot = extract_and_guard(tmp_path, archive)
    assert report.ok is False
    assert "executable_payload" in report.error_rules
    assert report.extracted == []
    assert_zero_side_effect(dest, snapshot)


# ======================================================================
# 资源炸弹（SEC-005：达到限制安全终止，不耗尽磁盘/内存）
# ======================================================================


def test_high_compression_ratio_zip_bomb(tmp_path: Path) -> None:
    """50MB 全零文件（压缩比约 1000:1）-> compression_ratio_abuse，零落盘。"""
    archive = make_zip(tmp_path / "bomb.zip", {"bomb.bin": b"\x00" * 50_000_000})
    assert archive.stat().st_size < 1_000_000, "夹具必须是高压缩比"
    limits = SizeLimits(max_compression_ratio=200.0)
    report, dest, snapshot = extract_and_guard(tmp_path, archive, limits)
    assert report.ok is False
    assert "compression_ratio_abuse" in report.error_rules
    assert_zero_side_effect(dest, snapshot)


def test_many_duplicate_entries_total_size_exceeded(tmp_path: Path) -> None:
    """海量重复大文件触发累计大小上限，安全终止。"""
    entries = {f"dup{i}.bin": b"Z" * 4096 for i in range(20)}
    archive = make_zip(tmp_path / "dups.zip", entries)
    limits = SizeLimits(
        max_file_bytes=1_000_000,
        max_total_bytes=32_768,
        max_compression_ratio=10_000,
    )
    report, dest, snapshot = extract_and_guard(tmp_path, archive, limits)
    assert report.ok is False
    assert "total_size_exceeded" in report.error_rules
    assert_zero_side_effect(dest, snapshot)


# ======================================================================
# 损坏 Zip
# ======================================================================


def test_corrupt_zip_fails_safe_without_writes(tmp_path: Path) -> None:
    """损坏 zip：corrupt_zip 安全失败，项目目录之外无写入。"""
    archive = tmp_path / "broken.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("assets/ok.png", b"\x89PNG")
    raw = archive.read_bytes()
    archive.write_bytes(raw[: len(raw) // 2])  # 截断成残缺 zip
    report, dest, snapshot = extract_and_guard(tmp_path, archive)
    assert report.ok is False
    assert report.error_rules == ["corrupt_zip"]
    assert_zero_side_effect(dest, snapshot)


# ======================================================================
# 发布包：伪造 manifest / 重复版本号 / 篡改哈希
# ======================================================================


def test_forged_pack_without_manifest(tmp_path: Path) -> None:
    """伪造发布包（缺 manifest.json）-> manifest_missing。"""
    pack = make_zip(tmp_path / "pack.zip", {"assets/a.png": b"x"})
    assert [i.rule_id for i in inspect_release_pack(pack)] == ["manifest_missing"]


def test_pack_with_invalid_manifest(tmp_path: Path) -> None:
    """manifest 非法 JSON 或缺 version -> manifest_invalid。"""
    for raw in (b"{not json", json.dumps({"files": {}}).encode("utf-8")):
        pack = make_release_pack(tmp_path / "pack2.zip", {"assets/a.png": b"x"}, raw)
        issues = inspect_release_pack(pack)
        assert [i.rule_id for i in issues] == ["manifest_invalid"], raw


def test_pack_duplicate_version(tmp_path: Path) -> None:
    """重复版本号：同版本已发布 -> duplicate_version。"""
    pack = make_release_pack(
        tmp_path / "pack3.zip", {"assets/a.png": b"x"}, {"version": "2.0.0"}
    )
    releases = tmp_path / "releases"
    (releases / "2.0.0").mkdir(parents=True)
    assert [i.rule_id for i in inspect_release_pack(pack, releases_dir=releases)] == [
        "duplicate_version"
    ]


def test_pack_tampered_hash(tmp_path: Path) -> None:
    """篡改哈希：包内内容与 manifest 声明不符 -> hash_mismatch。"""
    manifest = {
        "version": "1.0.0",
        "files": {"assets/a.png": hashlib.sha256(b"original").hexdigest()},
    }
    pack = make_release_pack(
        tmp_path / "pack4.zip", {"assets/a.png": b"tampered!"}, manifest
    )
    issues = inspect_release_pack(pack)
    assert [i.rule_id for i in issues] == ["hash_mismatch"]


def test_release_pack_good_case_passes(tmp_path: Path) -> None:
    """对照组：合法发布包预检零问题（防测试集只会说不）。"""
    files = {"assets/a.png": b"png", "machines/main.yaml": b"steps: []\n"}
    pack = make_release_pack(
        tmp_path / "good.zip", files, {"version": "1.0.0", "schema_version": 1}
    )
    assert inspect_release_pack(pack) == []


# ======================================================================
# 符号链接逃逸（dest 内预埋链接）
# ======================================================================


def test_symlink_escape_via_planted_link(tmp_path: Path) -> None:
    """dest 内预埋指向外部的 symlink/junction：条目解析越界即拒绝，零写入。"""
    parent = tmp_path / "sandbox2"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = parent / "dest"
    dest.mkdir()
    if not make_link(outside, dest / "link"):
        pytest.skip("当前环境无法创建符号链接/junction")
    archive = make_zip(tmp_path / "sneak.zip", {"link/evil.txt": b"pwned"})
    before = sorted(p.name for p in parent.iterdir())
    report = safe_extract_zip(archive, dest)
    after = sorted(p.name for p in parent.iterdir())
    assert report.ok is False
    assert "symlink_escape" in report.error_rules
    assert report.extracted == []
    assert (outside / "evil.txt").exists() is False, "外部目录不得被写入"
    assert [n for n in after if n not in before] == []


# ======================================================================
# 全量零侧效兜底
# ======================================================================


def test_all_malicious_packs_leave_dest_parent_unchanged(tmp_path: Path) -> None:
    """TST-010 汇总：所有恶意包被拒后，dest 父目录无任何新增（除空 dest）。"""
    cases: list[Path] = [
        make_zip(tmp_path / "c1.zip", {"../x.txt": b"1"}),
        make_zip(tmp_path / "c2.zip", {"C:\\x.txt": b"1"}),
        make_zip(tmp_path / "c3.zip", {"/x.txt": b"1"}),
        make_zip(tmp_path / "c4.zip", {"CON": b"1"}),
        make_zip(tmp_path / "c5.zip", {"evil.exe": b"1"}),
        make_zip(tmp_path / "c6.zip", {"run.ps1": b"1"}),
        make_zip(tmp_path / "c7.zip", {"bomb.bin": b"\x00" * 5_000_000}),
        make_zip(tmp_path / "c8.zip", {"../a": b"1", "run.bat": b"1"}),
    ]
    broken = tmp_path / "c9.zip"
    with zipfile.ZipFile(broken, "w") as zf:
        zf.writestr("ok.txt", b"1")
    broken.write_bytes(broken.read_bytes()[:10])
    cases.append(broken)

    parent = tmp_path / "sweep"
    parent.mkdir()
    before = sorted(p.name for p in parent.iterdir())
    for i, archive in enumerate(cases):
        report = safe_extract_zip(archive, parent / f"dest{i}")
        assert report.ok is False, archive.name
        assert report.extracted == [], archive.name
    after = sorted(p.name for p in parent.iterdir())
    assert [n for n in after if n not in before] == [], "拒绝的包不得产生任何落盘"
