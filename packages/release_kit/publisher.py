"""发布流水线与不可变发布目录（VER-002 / VER-005，ADR-0007 §3）。

发布流程（``ReleasePublisher.publish``）：
1. 解析项目（结构错误阻断）；
2. 静态分析（domain_model.static_analysis，error 级阻断，AC-P0-10）；
3. 质量闸门钩子（:class:`ReleaseChecks`：callable 列表，感知/回放/E2E
   等检查在此挂接，任一失败即阻断并记录原因——VER-005 的挂点）；
4. 冻结工作区（内容哈希清单）；
5. 同内容幂等：内容指纹与既有发布相同 => 复用既有 release_id；
6. 复制到 ``releases/<release_id>/`` 并尽力设为只读（非强制，见 hashing）；
7. 写 manifest.json（VER-001）+ manifest.sig（VER-006 签名信封）。

release_id 规则：``v<递增序号>-<内容短哈希>``；任何内容变化产生新版本，
发布后内容不可原地修改（篡改由 verify_directory 检出）。
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from common.clock import Clock
from domain_model.dsl import SEVERITY_ERROR
from domain_model.errors import DomainValidationError
from domain_model.parsing import ProjectBundle, load_project
from domain_model.static_analysis import analyze

from release_kit.errors import ReleaseError
from release_kit.hashing import (
    EXCLUDED_DIR_NAMES,
    content_digest,
    freeze_directory,
    make_tree_readonly,
)
from release_kit.manifest import ReleaseManifest, RuntimeCompat, TestSummary, build_release_manifest
from release_kit.signing import NullSigner, Signer, signature_envelope

#: release_id 形如 ``v3-1a2b3c4d``
_RELEASE_ID_RE = re.compile(r"^v(\d+)-([0-9a-f]{8})$")


@dataclass(frozen=True)
class CheckOutcome:
    """质量闸门检查结果。

    检查 callable 可返回：``True``/``None``（通过）、``False``（失败，
    原因缺省）或本结构（失败时携带机器可读 name 与人读 reason）。
    """

    ok: bool
    reason: str = ""
    name: str = ""


#: 质量闸门检查函数：接收项目聚合根，返回 True/None/False 或 CheckOutcome
ReleaseCheck = Callable[[ProjectBundle], "bool | CheckOutcome | None"]
#: 质量闸门检查列表（VER-005：任一失败即阻断发布）
ReleaseChecks = Sequence[ReleaseCheck]


@dataclass(frozen=True)
class ReleaseRecord:
    """一次发布的引用：ID、目录、清单路径与内容指纹。"""

    release_id: str
    directory: Path
    manifest_path: Path
    #: 发布内容指纹（冻结清单总摘要）
    content_sha: str
    created_at: float

    @property
    def sequence(self) -> int:
        """发布序号（从 release_id 解析）。"""
        match = _RELEASE_ID_RE.match(self.release_id)
        if match is None:
            raise ReleaseError(f"release_id 格式非法：{self.release_id}")
        return int(match.group(1))


def _prune_runtime_dirs(directory: str, names: list[str]) -> list[str]:
    """copytree ignore 钩子：发布目录不包含运行产物与版本库目录。"""
    return [name for name in names if name in EXCLUDED_DIR_NAMES]


def _run_checks(checks: ReleaseChecks, bundle: ProjectBundle) -> list[str]:
    """执行质量闸门钩子，收集失败原因（VER-005）。"""
    reasons: list[str] = []
    for index, check in enumerate(checks):
        outcome = check(bundle)
        if outcome is None or outcome is True:
            continue
        if isinstance(outcome, CheckOutcome):
            if not outcome.ok:
                label = outcome.name or f"#{index}"
                reasons.append(f"质量闸门 {label} 未通过：{outcome.reason or '无原因说明'}")
        elif outcome is False:
            reasons.append(f"质量闸门 #{index} 未通过：检查返回 False")
        else:
            reasons.append(f"质量闸门 #{index} 返回值无法解释：{outcome!r}")
    return reasons


class ReleasePublisher:
    """发布器：把整个项目发布为不可变、可追溯的版本包。

    Attributes:
        project_dir:  项目工作区目录。
        releases_dir: 发布根目录（``releases/<release_id>/``）。
        clock:        注入时钟（manifest.created_at 与发布时序）。
    """

    def __init__(self, project_dir: str | Path, releases_dir: str | Path, clock: Clock) -> None:
        self._project_dir = Path(project_dir)
        self._releases_dir = Path(releases_dir)
        self._clock = clock

    # -- 发布 ---------------------------------------------------------------

    def publish(
        self,
        checks: ReleaseChecks = (),
        *,
        runtime_compat: RuntimeCompat | None = None,
        test_summary: TestSummary | None = None,
        signer: Signer | None = None,
    ) -> ReleaseRecord:
        """发布当前工作区；任何前置失败都会抛出 :class:`ReleaseError`。

        Args:
            checks:         质量闸门钩子列表（感知/回放/E2E 检查挂点，VER-005）。
            runtime_compat: 运行时兼容范围（VER-010）。
            test_summary:   测试摘要留痕。
            signer:         签名器（缺省 NullSigner，开发模式）。
        """
        # 1) 解析项目（结构错误阻断）
        try:
            bundle = load_project(self._project_dir)
        except DomainValidationError as exc:
            raise ReleaseError("项目解析失败，发布被阻断", issues=list(exc.issues)) from None

        # 2) 静态分析：error 级问题阻断发布（VER-005 / AC-P0-10）
        blocking = [issue for issue in analyze(self._project_dir) if issue.severity == SEVERITY_ERROR]
        if blocking:
            raise ReleaseError("静态分析存在 error 级问题，发布被阻断", issues=blocking)

        # 3) 质量闸门钩子（任一 False/失败即阻断并记录原因）
        reasons = _run_checks(checks, bundle)
        if reasons:
            raise ReleaseError("发布质量闸门未通过", reasons=reasons)

        # 4) 冻结工作区并计算内容指纹
        files = freeze_directory(self._project_dir)
        digest = content_digest(files)

        # 5) 幂等：同内容重复发布复用既有 release_id
        for record in self.list_releases():
            if record.content_sha == digest:
                return record

        # 6) 分配序号与 ID，复制冻结内容到发布目录
        release_id = f"v{self._next_sequence()}-{digest[:8]}"
        release_dir = self._releases_dir / release_id
        self._releases_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self._project_dir, release_dir, ignore=_prune_runtime_dirs)

        # 7) 写清单（以上一个发布为版本号基准）与签名信封，然后设只读
        manifest = build_release_manifest(
            bundle=bundle,
            files=files,
            release_id=release_id,
            created_at=float(self._clock.now()),
            runtime_compat=runtime_compat,
            test_summary=test_summary,
            previous=self._latest_manifest(),
        )
        manifest.save(release_dir / "manifest.json")
        envelope = signature_envelope((release_dir / "manifest.json").read_bytes(), signer or NullSigner())
        sig_text = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
        (release_dir / "manifest.sig").write_text(sig_text, encoding="utf-8")
        make_tree_readonly(release_dir)
        return ReleaseRecord(
            release_id=release_id,
            directory=release_dir,
            manifest_path=release_dir / "manifest.json",
            content_sha=digest,
            created_at=manifest.created_at,
        )

    # -- 查询 ---------------------------------------------------------------

    def list_releases(self) -> list[ReleaseRecord]:
        """列出全部发布（按序号升序）；历史记录只增不删（回滚可审计）。"""
        records: list[ReleaseRecord] = []
        if not self._releases_dir.is_dir():
            return records
        for entry in sorted(self._releases_dir.iterdir()):
            if not entry.is_dir() or _RELEASE_ID_RE.match(entry.name) is None:
                continue
            manifest_path = entry / "manifest.json"
            if not manifest_path.is_file():
                continue  # 损坏的发布目录不参与枚举（审计时可见目录本身）
            manifest = ReleaseManifest.load(manifest_path)
            records.append(
                ReleaseRecord(
                    release_id=manifest.release_id,
                    directory=entry,
                    manifest_path=manifest_path,
                    content_sha=manifest.content_sha,
                    created_at=manifest.created_at,
                )
            )
        return sorted(records, key=lambda r: r.sequence)

    def load_release(self, release_id: str) -> ReleaseManifest:
        """加载指定发布的清单；不存在或 ID 不符抛 :class:`ReleaseError`。"""
        manifest_path = self._releases_dir / release_id / "manifest.json"
        if not manifest_path.is_file():
            raise ReleaseError(f"发布不存在或缺少清单：{release_id}")
        manifest = ReleaseManifest.load(manifest_path)
        if manifest.release_id != release_id:
            raise ReleaseError(f"清单 release_id 与目录不符：{manifest.release_id} != {release_id}")
        return manifest

    # -- 内部 ---------------------------------------------------------------

    def _next_sequence(self) -> int:
        """下一个发布序号 = 现有最大序号 + 1。"""
        existing = self.list_releases()
        return existing[-1].sequence + 1 if existing else 1

    def _latest_manifest(self) -> ReleaseManifest | None:
        """当前最新发布的清单（对象版本号递增的基准）；无发布时 None。"""
        records = self.list_releases()
        return self.load_release(records[-1].release_id) if records else None
