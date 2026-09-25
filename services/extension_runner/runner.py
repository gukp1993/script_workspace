"""独立进程扩展 Runner（PLG-002）。

职责（父进程侧）：
- 以 ``subprocess.Popen([sys.executable, "-m", "extension_runner.child", ...])``
  启动**隔离子进程**执行扩展——扩展崩溃/超时只影响子进程，主进程拿到
  错误结果继续运行（验收：崩溃不影响按键释放）；
- 基础版资源限额：**超时 kill**（到时 ``kill()`` 后回收管道）与**输出
  大小上限**；``max_memory_mb`` 目前作为声明契约透传（内存硬限额需
  Windows Job Object，属后续里程碑，见 README）；
- **架构保证：扩展进程绝不持有 InputBroker 句柄**——父子边界只允许
  JSON 可序列化数据（payload 先做 ``json.dumps`` 往返校验，任何
  broker/输入对象/句柄在序列化一步即被拒绝，物理上无法传递）；
- ``revoke(extension_id)`` 权限撤销：撤销后本 Runner 拒绝再为该扩展
  启动子进程（已启动进程由下一次调用前的存活检查/超时兜底）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from extension_runner.contract import ExtensionManifest, load_manifest
from extension_runner.errors import ExtensionRunError

#: 子进程结果信封前缀（与 child.SENTINEL 一致；此处重复定义避免父进程
#: 额外导入 child 模块）
_SENTINEL = "@@EXT_RESULT@@"

#: stderr 回传尾部大小（崩溃原因截断，避免日志爆炸）
_STDERR_TAIL_CHARS = 600

#: services/ 根（加入子进程 PYTHONPATH，保证 ``-m extension_runner.child`` 可用）
_SERVICES_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class ExtensionRunResult:
    """一次扩展调用的结果（永不抛异常穿透——错误全部落在字段里）。"""

    ok: bool
    value: Any = None
    #: 失败原因：timeout / crash / output_limit / revoked / payload_invalid /
    #: capability_denied / extension_error
    reason: str | None = None
    detail: str | None = None
    duration_s: float = 0.0
    #: 产出审计事件的建议类型（host 层使用）：invoked / rejected / crashed
    outcome: str = "invoked"


@dataclass
class RunnerConfig:
    """Runner 基础配置。"""

    default_timeout_s: float = 10.0
    #: stdout 总量上限（字节）——基础版"输出大小限额"
    max_output_bytes: int = 1_000_000
    #: Python 解释器（测试可注入，默认 sys.executable）
    python_exe: str = field(default_factory=lambda: sys.executable)


class ExtensionRunner:
    """子进程隔离执行扩展；错误以 :class:`ExtensionRunResult` 返回。"""

    def __init__(self, config: RunnerConfig | None = None) -> None:
        self.config = config or RunnerConfig()
        self._revoked: set[str] = set()

    # -- 权限撤销 -----------------------------------------------------------

    def revoke(self, extension_id: str) -> None:
        """撤销扩展执行权限：此后 run 一律拒绝（不启动子进程）。"""
        self._revoked.add(extension_id)

    def restore(self, extension_id: str) -> None:
        """恢复权限（宿主/治理流程显式调用；默认不存在自动恢复）。"""
        self._revoked.discard(extension_id)

    def is_revoked(self, extension_id: str) -> bool:
        """扩展是否已被撤销。"""
        return extension_id in self._revoked

    # -- 执行 ----------------------------------------------------------------

    def run(
        self,
        manifest: ExtensionManifest | str | Path,
        payload: Any = None,
        *,
        timeout_s: float | None = None,
    ) -> ExtensionRunResult:
        """执行一次扩展调用；保证不抛异常、不波及主进程。

        Args:
            manifest: 已加载清单或清单路径（路径时现场加载校验）。
            payload: 传给扩展的数据（必须 JSON 可序列化——架构边界）。
            timeout_s: 本次调用超时；缺省取 ``min(清单声明, 默认值)``。
        """
        started = time.monotonic()

        # 1) 解析清单
        try:
            mf = manifest if isinstance(manifest, ExtensionManifest) else load_manifest(manifest)
        except Exception as exc:  # noqa: BLE001
            return ExtensionRunResult(False, reason="crash", detail=f"manifest 加载失败：{exc}",
                                      duration_s=self._elapsed(started), outcome="rejected")

        # 2) 权限撤销检查（加载后判，覆盖按路径调用的情况）
        if self.is_revoked(mf.extension_id):
            return ExtensionRunResult(False, reason="revoked",
                                      detail=f"扩展 {mf.extension_id} 权限已被撤销，拒绝执行",
                                      duration_s=self._elapsed(started), outcome="rejected")

        # 3) 架构边界：只允许 JSON 数据过界。句柄/对象在这里即被拒绝，
        #    因此子进程**不可能**收到 InputBroker 或输入对象的引用。
        try:
            payload_text = json.dumps(payload if payload is not None else {}, ensure_ascii=False)
            json.loads(payload_text)
        except (TypeError, ValueError) as exc:
            return ExtensionRunResult(False, reason="payload_invalid",
                                      detail=f"payload 必须 JSON 可序列化（禁止传递对象/句柄）：{exc}",
                                      duration_s=self._elapsed(started), outcome="rejected")

        # 4) 超时决策：显式参数 > 清单声明与默认值取小
        effective = timeout_s if timeout_s is not None else min(
            mf.resource_limits.max_runtime_s, self.config.default_timeout_s
        )
        effective = max(0.1, float(effective))

        # 5) 启动隔离子进程（env 只带 JSON 文本，不带任何句柄）
        cmd = [
            self.config.python_exe, "-m", "extension_runner.child",
            str(mf.manifest_path or mf.source_file), payload_text,
        ]
        env = self._child_env()
        try:
            proc = subprocess.Popen(  # noqa: S603 —— cmd 由本模块白名单组装
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(_SERVICES_ROOT),
                env=env,
            )
        except OSError as exc:
            return ExtensionRunResult(False, reason="crash", detail=f"子进程启动失败：{exc}",
                                      duration_s=self._elapsed(started), outcome="crashed")

        try:
            stdout, stderr = proc.communicate(timeout=effective)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError):  # pragma: no cover —— kill 后回收兜底
                stdout, stderr = "", ""
            return ExtensionRunResult(
                False, reason="timeout",
                detail=f"扩展 {mf.extension_id} 超过 {effective:.1f}s 已强制终止",
                duration_s=self._elapsed(started), outcome="crashed",
            )
        except ValueError:  # pragma: no cover —— 管道异常兜底
            proc.kill()
            return ExtensionRunResult(False, reason="crash", detail="子进程管道异常",
                                      duration_s=self._elapsed(started), outcome="crashed")

        duration = self._elapsed(started)

        # 6) 输出大小限额（基础版资源限额之二）
        out_bytes = (stdout or "").encode("utf-8", errors="replace")
        if len(out_bytes) > self.config.max_output_bytes:
            return ExtensionRunResult(False, reason="output_limit",
                                      detail=f"stdout {len(out_bytes)}B 超过上限 {self.config.max_output_bytes}B",
                                      duration_s=duration, outcome="crashed")

        # 7) 崩溃（非零退出）：返回错误原因，不波及主进程
        if proc.returncode != 0:
            return ExtensionRunResult(False, reason="crash",
                                      detail=self._stderr_tail(stderr, proc.returncode),
                                      duration_s=duration, outcome="crashed")

        # 8) 解析结果信封（取 stdout 中最后一条带前缀的行）
        envelope = self._parse_envelope(stdout or "")
        if envelope is None:
            return ExtensionRunResult(False, reason="crash",
                                      detail="子进程未返回结果信封（异常退出或输出被截断）",
                                      duration_s=duration, outcome="crashed")
        if not envelope.get("ok"):
            reason = str(envelope.get("reason") or "extension_error")
            return ExtensionRunResult(False, reason=reason,
                                      detail=str(envelope.get("detail") or ""),
                                      duration_s=duration,
                                      outcome="rejected" if reason == "capability_denied" else "crashed")
        return ExtensionRunResult(True, value=envelope.get("value"),
                                  duration_s=duration, outcome="invoked")

    # -- 内部 ----------------------------------------------------------------

    @staticmethod
    def _elapsed(started: float) -> float:
        return time.monotonic() - started

    @staticmethod
    def _child_env() -> dict[str, str]:
        """子进程环境：只追加 PYTHONPATH 与编码，继承其余（无特殊句柄）。"""
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        parts = [p for p in existing.split(os.pathsep) if p]
        if str(_SERVICES_ROOT) not in parts:
            parts.insert(0, str(_SERVICES_ROOT))
        env["PYTHONPATH"] = os.pathsep.join(parts)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    @staticmethod
    def _stderr_tail(stderr: str | None, returncode: int) -> str:
        text = (stderr or "").strip().replace("\r", "")
        tail = text[-_STDERR_TAIL_CHARS:] if text else "(无 stderr 输出)"
        return f"扩展进程退出码 {returncode}：{tail}"

    @staticmethod
    def _parse_envelope(stdout: str) -> dict | None:
        """从 stdout 取**最后一条**信封行；扩展自身 print 不影响解析。"""
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith(_SENTINEL):
                try:
                    data = json.loads(line[len(_SENTINEL):])
                except ValueError:
                    return None
                return data if isinstance(data, dict) else None
        return None


def run_extension(manifest_path: str | Path, payload: Any = None, *, timeout_s: float | None = None) -> ExtensionRunResult:
    """一次性便捷入口：自带 Runner 实例执行（不复用、不注册 host）。"""
    return ExtensionRunner().run(manifest_path, payload, timeout_s=timeout_s)


# 引用保持：ExtensionRunError 供调用方 except 用（Runner 本身不抛它）
__all__ = ["ExtensionRunResult", "ExtensionRunner", "RunnerConfig", "ExtensionRunError", "run_extension"]
