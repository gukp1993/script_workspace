"""扩展宿主（PLG-002/PLG-003 的后端基座）。

职责：
- 注册（加载并校验清单）/ 列出 / 调用 / 撤销扩展；
- 每次调用/拒绝/崩溃都产生**审计事件**（dict），可选回调逐条外发，
  同时保留在内存事件表里供查询（事件表容量有界，防内存增长）；
- 撤销即生效：``revoke`` 后 ``call`` 立即拒绝，不再启动子进程
  （PLG-003 验收：撤销后立即停止调用的服务端语义）。

安全约定：
- 宿主不向扩展传递任何 InputBroker/输入对象；调用边界只有 JSON 数据
  （见 runner.py 的序列化防线）；
- 审计回调内的异常被吞掉并降级为事件记录，保证主流程不被观察者打挂。
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any, Callable

from extension_runner.contract import ExtensionManifest, load_manifest
from extension_runner.errors import ExtensionError
from extension_runner.runner import ExtensionRunner, ExtensionRunResult

#: 审计事件回调（事件为纯数据 dict：type/extension_id/reason/detail/duration_s）
AuditCallback = Callable[[dict], None]

#: 内存事件表容量上限（超出丢弃最旧事件）
_MAX_EVENTS = 500


class ExtensionHost:
    """扩展注册/调用/撤销门面，带审计事件流。"""

    def __init__(self, runner: ExtensionRunner | None = None, on_event: AuditCallback | None = None) -> None:
        self._runner = runner or ExtensionRunner()
        self._on_event = on_event
        self._manifests: dict[str, ExtensionManifest] = {}
        self._events: deque[dict] = deque(maxlen=_MAX_EVENTS)

    # -- 注册与查询 ----------------------------------------------------------

    def register(self, manifest_path: str | Path) -> ExtensionManifest:
        """加载并注册扩展清单；ID 重复或清单非法抛 :class:`ExtensionError`。"""
        manifest = load_manifest(manifest_path)
        if manifest.extension_id in self._manifests:
            raise ExtensionError(
                f"扩展 {manifest.extension_id!r} 已注册（重复注册请先撤销并卸载）"
            )
        self._manifests[manifest.extension_id] = manifest
        self._record({"type": "registered", "extension_id": manifest.extension_id,
                      "reason": None, "detail": f"version={manifest.version}"})
        return manifest

    def list_extensions(self) -> list[dict]:
        """已注册扩展摘要（含撤销状态，展示/审查 UI 用）。"""
        return [
            {**m.describe(), "revoked": self._runner.is_revoked(m.extension_id)}
            for m in self._manifests.values()
        ]

    def get(self, extension_id: str) -> ExtensionManifest:
        """取已注册清单；未知 ID 抛 :class:`ExtensionError`。"""
        manifest = self._manifests.get(extension_id)
        if manifest is None:
            raise ExtensionError(f"未知扩展 {extension_id!r}")
        return manifest

    # -- 调用与撤销 ----------------------------------------------------------

    def call(self, extension_id: str, payload: Any = None, *, timeout_s: float | None = None) -> ExtensionRunResult:
        """调用扩展：未知 ID / 已撤销 → 受控拒绝；崩溃/超时 → 错误结果。

        本方法不抛异常；所有失败都体现在 :class:`ExtensionRunResult` 并
        产出审计事件。
        """
        manifest = self._manifests.get(extension_id)
        if manifest is None:
            result = ExtensionRunResult(False, reason="unknown_extension",
                                        detail=f"未知扩展 {extension_id!r}", outcome="rejected")
            self._record(self._event("rejected", extension_id, result))
            return result

        if self._runner.is_revoked(extension_id):
            result = ExtensionRunResult(False, reason="revoked",
                                        detail=f"扩展 {extension_id} 权限已被撤销，拒绝调用",
                                        outcome="rejected")
            self._record(self._event("rejected", extension_id, result))
            return result

        result = self._runner.run(manifest, payload, timeout_s=timeout_s)
        event_type = {"invoked": "invoked", "rejected": "rejected", "crashed": "crashed"}[result.outcome]
        self._record(self._event(event_type, extension_id, result))
        return result

    def revoke(self, extension_id: str) -> None:
        """撤销扩展：本 host 与底层 runner 双侧拒绝后续调用。"""
        if extension_id not in self._manifests:
            raise ExtensionError(f"未知扩展 {extension_id!r}，无法撤销")
        self._runner.revoke(extension_id)
        self._record({"type": "revoked", "extension_id": extension_id,
                      "reason": None, "detail": "权限已撤销，后续调用将被拒绝"})

    # -- 审计 ----------------------------------------------------------------

    @property
    def events(self) -> tuple[dict, ...]:
        """审计事件快照（旧→新）。"""
        return tuple(self._events)

    def _record(self, event: dict) -> None:
        """记录事件并外发回调（回调异常不影响主流程）。"""
        self._events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(dict(event))
            except Exception:  # noqa: BLE001 —— 观察者不拖垮宿主
                self._events.append({"type": "audit_callback_error", "extension_id": event.get("extension_id"),
                                     "reason": None, "detail": "审计回调抛出异常（已忽略）"})

    @staticmethod
    def _event(event_type: str, extension_id: str, result: ExtensionRunResult) -> dict:
        return {
            "type": event_type,
            "extension_id": extension_id,
            "reason": result.reason,
            "detail": (result.detail or "")[:300],
            "duration_s": round(result.duration_s, 3),
        }
