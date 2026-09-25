"""extension_runner 单测（PLG-001/002，M4）。

覆盖：
- 清单契约：合法加载、缺失清单、未注册能力拒载、input.* 能力拒载、
  坏哈希拒载、非法 entry、未知字段/版本；
- Runner：合法调用回传结果、能力门面（声明内/声明外）、运行中越权拒绝、
  死循环超时 kill、崩溃隔离（os._exit / 导入失败）、受控扩展异常、
  输出超限、非 JSON 载荷拒绝、撤销后拒载；
- Host：注册/调用/撤销/审计事件流；
- 架构保证：父子进程源码不导入 input_broker。

注意 Windows 子进程 spawn 开销：所有扩展都是毫秒级小任务，超时用 2~5s。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from extension_runner import (
    EXTENSION_CAPABILITY_REGISTRY,
    ExtensionError,
    ExtensionHost,
    ExtensionRunner,
    ManifestError,
    compute_code_hash,
    load_manifest,
)

REPO = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# 夹具辅助：在 tmp_path 内生成"内置测试扩展"（代码 + 清单 + 正确哈希）
# ---------------------------------------------------------------------------


def make_extension(
    tmp_path: Path,
    extension_id: str,
    code: str,
    *,
    module: str = "ext_mod",
    func: str = "run",
    capabilities: list[str] | None = None,
    version: str = "1.0.0",
    max_memory_mb: int = 128,
    max_runtime_s: float = 10,
    sha_override: str | None = None,
    entry: str | None = None,
) -> Path:
    """生成一个扩展目录，返回 manifest.yaml 路径。"""
    ext_dir = tmp_path / extension_id
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / f"{module}.py").write_text(code, encoding="utf-8")
    sha = sha_override if sha_override is not None else compute_code_hash(ext_dir)
    caps = capabilities if capabilities is not None else []
    entry_value = entry if entry is not None else f"{module}:{func}"
    (ext_dir / "manifest.yaml").write_text(
        f"schema_version: 1\n"
        f"extension_id: {extension_id}\n"
        f"version: '{version}'\n"
        f"capabilities: [{', '.join(caps)}]\n"
        f"entry: {entry_value}\n"
        f"resource_limits:\n"
        f"  max_memory_mb: {max_memory_mb}\n"
        f"  max_runtime_s: {max_runtime_s}\n"
        f"sha256: {sha}\n",
        encoding="utf-8",
    )
    return ext_dir / "manifest.yaml"


GOOD_EXTENSION = """def run(context, payload):
    a = int(payload.get("a", 0))
    b = int(payload.get("b", 0))
    return {"sum": a + b, "notify_allowed": context.has("notify.desktop"), "ext": context.extension_id}
"""

RUNAWAY_EXTENSION = """def run(context, payload):
    while True:
        pass
"""

OVERREACH_EXTENSION = """def run(context, payload):
    context.require("input.mouse")   # 声明外能力（且 input.* 根本不可注册）
    return {"sent": True}
"""

CRASH_EXTENSION = """import os

def run(context, payload):
    os._exit(7)   # 不可捕获的硬崩溃
"""

RAISING_EXTENSION = """def run(context, payload):
    raise ValueError("boom")
"""

NOISY_EXTENSION = """def run(context, payload):
    print("log line before result")   # 扩展自身 print 不应影响信封解析
    return {"ok_field": payload.get("x")}
"""

BIG_OUTPUT_EXTENSION = """def run(context, payload):
    print("x" * 200000)
    return {"done": True}
"""


@pytest.fixture()
def runner() -> ExtensionRunner:
    return ExtensionRunner()


# ---------------------------------------------------------------------------
# 清单契约（PLG-001）
# ---------------------------------------------------------------------------


def test_load_manifest_ok(tmp_path):
    path = make_extension(tmp_path, "calc-ext", GOOD_EXTENSION, capabilities=["notify.desktop"])
    mf = load_manifest(path)
    assert mf.extension_id == "calc-ext"
    assert mf.version == "1.0.0"
    assert mf.capabilities == ("notify.desktop",)
    assert mf.entry == "ext_mod:run"
    assert mf.entry_module == "ext_mod"
    assert mf.entry_function == "run"
    assert mf.resource_limits.max_memory_mb == 128
    assert mf.resource_limits.max_runtime_s == 10
    assert re.fullmatch(r"[0-9a-f]{64}", mf.sha256)
    assert mf.describe()["capabilities"] == ["notify.desktop"]


def test_load_manifest_missing_file(tmp_path):
    with pytest.raises(ManifestError, match="不存在"):
        load_manifest(tmp_path / "no_such" / "manifest.yaml")


def test_manifest_rejects_unregistered_capability(tmp_path):
    """白名单外能力默认拒绝：notify.push 未注册。"""
    path = make_extension(tmp_path, "greedy", "def run(ctx, p):\n    return 1\n",
                          capabilities=["notify.push"])
    with pytest.raises(ManifestError, match="默认拒绝"):
        load_manifest(path)


def test_manifest_rejects_input_capability(tmp_path):
    """input.* 不在扩展能力注册表中——扩展永远拿不到真实输入能力。"""
    assert not any(c.startswith("input.") for c in EXTENSION_CAPABILITY_REGISTRY)
    path = make_extension(tmp_path, "input-thief", "def run(ctx, p):\n    return 1\n",
                          capabilities=["input.mouse"])
    with pytest.raises(ManifestError, match="input.mouse"):
        load_manifest(path)


def test_manifest_rejects_bad_hash(tmp_path):
    """代码包被篡改（哈希不符）→ 拒载。"""
    path = make_extension(tmp_path, "tampered", GOOD_EXTENSION)
    ext_dir = path.parent
    (ext_dir / "ext_mod.py").write_text("# 后期篡改\ndef run(c, p):\n    return 2\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="哈希不符"):
        load_manifest(path)


def test_manifest_rejects_bad_entry_and_unknown_field(tmp_path):
    path = make_extension(tmp_path, "broken", "def run(c, p):\n    return 1\n", entry="ext_mod run")
    with pytest.raises(ManifestError, match="entry"):
        load_manifest(path)

    ext_dir = tmp_path / "extra-field"
    ext_dir.mkdir()
    (ext_dir / "m.py").write_text("def run(c, p):\n    return 1\n", encoding="utf-8")
    (ext_dir / "manifest.yaml").write_text(
        "schema_version: 1\nextension_id: extra-field\nversion: '1'\n"
        "capabilities: []\nentry: m:run\n"
        f"sha256: {compute_code_hash(ext_dir)}\nsecret_backdoor: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="secret_backdoor"):
        load_manifest(ext_dir / "manifest.yaml")


def test_manifest_rejects_bad_version_and_schema_version(tmp_path):
    path = make_extension(tmp_path, "ver-check", "def run(c, p):\n    return 1\n", version="abc")
    with pytest.raises(ManifestError, match="version"):
        load_manifest(path)


# ---------------------------------------------------------------------------
# Runner（PLG-002）
# ---------------------------------------------------------------------------


def test_runner_returns_computed_result(tmp_path, runner):
    """合法扩展：声明能力 + 返回计算结果经 stdout 信封回传。"""
    path = make_extension(tmp_path, "calc", GOOD_EXTENSION, capabilities=["notify.desktop"])
    result = runner.run(path, {"a": 2, "b": 3}, timeout_s=5)
    assert result.ok is True
    assert result.reason is None
    assert result.value == {"sum": 5, "notify_allowed": True, "ext": "calc"}
    assert result.duration_s > 0
    assert result.outcome == "invoked"


def test_capability_facade_default_deny(tmp_path, runner):
    """能力门面：声明内 has()=True；声明外（含未注册 input.*）has()=False。"""
    code = (
        "def run(context, payload):\n"
        "    return {\n"
        "        'declared': context.has('trace.read'),\n"
        "        'undeclared': context.has('notify.desktop'),\n"
        "        'unregistrable': context.has('input.key'),\n"
        "        'limits': context.limits['max_memory_mb'],\n"
        "    }\n"
    )
    path = make_extension(tmp_path, "facade", code, capabilities=["trace.read"])
    result = runner.run(path, {}, timeout_s=5)
    assert result.ok
    assert result.value == {"declared": True, "undeclared": False, "unregistrable": False, "limits": 128}


def test_runner_denies_undeclared_capability_call(tmp_path, runner):
    """越权：manifest 声明外能力调用 → 受控拒绝（ok=False + capability_denied）。"""
    path = make_extension(tmp_path, "overreach", OVERREACH_EXTENSION, capabilities=["trace.read"])
    result = runner.run(path, {}, timeout_s=5)
    assert result.ok is False
    assert result.reason == "capability_denied"
    assert "input.mouse" in (result.detail or "")
    assert result.outcome == "rejected"


def test_runner_timeout_kills_runaway(tmp_path, runner):
    """死循环：超时强制 kill，返回 timeout 错误，主进程不受影响。"""
    path = make_extension(tmp_path, "runaway", RUNAWAY_EXTENSION)
    result = runner.run(path, {}, timeout_s=2)
    assert result.ok is False
    assert result.reason == "timeout"
    assert "强制终止" in (result.detail or "")
    assert result.outcome == "crashed"


def test_runner_crash_isolated_hard_exit(tmp_path, runner):
    """硬崩溃（os._exit 非零退出）：返回 crash 错误，不波及主进程。"""
    path = make_extension(tmp_path, "hard-crash", CRASH_EXTENSION)
    result = runner.run(path, {}, timeout_s=5)
    assert result.ok is False
    assert result.reason == "crash"
    assert "退出码 7" in (result.detail or "")


def test_runner_crash_on_import_failure(tmp_path, runner):
    """entry 模块不存在：子进程崩溃返回错误，主进程拿到原因。"""
    path = make_extension(tmp_path, "ghost", "def run(c, p):\n    return 1\n", entry="no_such_module:run")
    result = runner.run(path, {}, timeout_s=5)
    assert result.ok is False
    assert result.reason == "crash"
    assert "导入失败" in (result.detail or "")


def test_runner_reports_extension_exception(tmp_path, runner):
    """扩展内部异常：受控信封（extension_error），子进程不产生假崩溃。"""
    path = make_extension(tmp_path, "raising", RAISING_EXTENSION)
    result = runner.run(path, {}, timeout_s=5)
    assert result.ok is False
    assert result.reason == "extension_error"
    assert "ValueError" in (result.detail or "")
    assert result.outcome == "crashed"


def test_runner_output_size_limit(tmp_path):
    """资源限额（基础版）：stdout 超过输出上限 → output_limit 拒绝。"""
    from extension_runner import RunnerConfig

    small = ExtensionRunner(RunnerConfig(default_timeout_s=5, max_output_bytes=10_000))
    path = make_extension(tmp_path, "noisy-big", BIG_OUTPUT_EXTENSION)
    result = small.run(path, {}, timeout_s=5)
    assert result.ok is False
    assert result.reason == "output_limit"


def test_runner_tolerates_extension_print_noise(tmp_path, runner):
    """扩展 print 不破坏信封解析（父进程取最后一条信封行）。"""
    path = make_extension(tmp_path, "loggy", NOISY_EXTENSION)
    result = runner.run(path, {"x": 42}, timeout_s=5)
    assert result.ok
    assert result.value == {"ok_field": 42}


def test_runner_rejects_non_serializable_payload(tmp_path, runner):
    """架构边界：对象/句柄无法 JSON 过界 → payload_invalid，子进程不启动。"""
    path = make_extension(tmp_path, "calc2", GOOD_EXTENSION, capabilities=["notify.desktop"])
    result = runner.run(path, {"broker": object()}, timeout_s=5)
    assert result.ok is False
    assert result.reason == "payload_invalid"
    assert "JSON" in (result.detail or "")


def test_runner_revoke_blocks_and_restore(tmp_path, runner):
    """撤销后拒载（不再启动子进程）；显式恢复后可再次调用。"""
    path = make_extension(tmp_path, "revocable", GOOD_EXTENSION, capabilities=["notify.desktop"])
    runner.revoke("revocable")
    denied = runner.run(path, {"a": 1, "b": 1}, timeout_s=5)
    assert denied.ok is False
    assert denied.reason == "revoked"
    assert denied.duration_s < 1  # 未启动子进程，立即拒绝

    runner.restore("revocable")
    ok = runner.run(path, {"a": 1, "b": 1}, timeout_s=5)
    assert ok.ok is True
    assert ok.value["sum"] == 2


# ---------------------------------------------------------------------------
# Host（注册/调用/撤销/审计）
# ---------------------------------------------------------------------------


def test_host_register_call_revoke_and_audit(tmp_path):
    path = make_extension(tmp_path, "hosted", GOOD_EXTENSION, capabilities=["notify.desktop"])
    host = ExtensionHost()
    mf = host.register(path)
    assert mf.extension_id == "hosted"

    listing = host.list_extensions()
    assert listing == [{**mf.describe(), "revoked": False}]

    result = host.call("hosted", {"a": 4, "b": 6}, timeout_s=5)
    assert result.ok and result.value["sum"] == 10

    host.revoke("hosted")
    denied = host.call("hosted", {}, timeout_s=5)
    assert denied.ok is False and denied.reason == "revoked"
    assert host.list_extensions()[0]["revoked"] is True

    types = [e["type"] for e in host.events]
    assert types == ["registered", "invoked", "revoked", "rejected"]


def test_host_rejects_unknown_duplicate_and_callbacks(tmp_path):
    path = make_extension(tmp_path, "dupe", GOOD_EXTENSION)
    events: list[dict] = []
    host = ExtensionHost(on_event=events.append)

    unknown = host.call("missing-ext", {})
    assert unknown.ok is False
    assert unknown.reason == "unknown_extension"
    host.register(path)
    with pytest.raises(ExtensionError, match="已注册"):
        host.register(path)
    with pytest.raises(ExtensionError, match="未知扩展"):
        host.revoke("missing-ext")

    assert [e["type"] for e in events] == ["rejected", "registered"]
    assert events[0]["reason"] == "unknown_extension"

    # 审计回调抛异常不影响主流程
    def bad_callback(_event):
        raise RuntimeError("observer down")

    host2 = ExtensionHost(on_event=bad_callback)
    host2.register(path)
    result = host2.call("dupe", {"a": 1, "b": 2}, timeout_s=5)
    assert result.ok
    assert any(e["type"] == "audit_callback_error" for e in host2.events)


def test_child_boundary_never_imports_input_broker():
    """架构保证：父/子进程源码均不导入 input_broker（扩展不持有其句柄）。"""
    pattern = re.compile(r"^\s*(?:import|from)\s+input_broker\b", re.MULTILINE)
    for name in ("child.py", "runner.py", "host.py", "contract.py"):
        source = (REPO / "services" / "extension_runner" / name).read_text(encoding="utf-8")
        assert pattern.search(source) is None, f"{name} 不应导入 input_broker"
