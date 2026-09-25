"""扩展子进程入口（PLG-002）。

由父进程以 ``python -m extension_runner.child <manifest_path> <payload_json>``
启动。职责边界：

- 重新校验清单（含代码包哈希）——即使父进程被绕过，子进程仍拒载篡改代码；
- 把清单所在目录加入 ``sys.path``，导入 entry 模块并调用
  ``func(context, payload)``；
- 通过 :class:`ExtensionContext` 能力门面执行**默认拒绝**：扩展运行中
  请求未声明/未注册能力即抛 :class:`CapabilityDenied`，以受控信封回传；
- 结果以最后一行 ``@@EXT_RESULT@@{...}`` 信封经 stdout 回传。受控失败
  （能力拒绝/扩展异常）信封 ``ok:false`` 且退出码 0；基础设施失败
  （清单非法、导入失败等）写 stderr 并以非零退出码崩溃——由父进程
  统一转化为错误结果，不波及主进程。

架构保证（PLG-002）：本模块**不导入也不感知** InputBroker / 输入对象；
父子边界只传递 JSON 标量数据（payload 经 JSON 序列化），任何句柄、
对象引用都无法跨越边界——扩展进程天然不持有 InputBroker 句柄。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from extension_runner.contract import (
    EXTENSION_CAPABILITY_REGISTRY,
    ExtensionManifest,
    load_manifest,
)
from extension_runner.errors import CapabilityDenied

#: 结果信封行前缀（父进程从 stdout 末尾向前扫描该前缀）
SENTINEL = "@@EXT_RESULT@@"

#: 允许回传的结果大小上限（信封 value 的保守上限，父进程另有输出总量上限）
_MAX_VALUE_CHARS = 64_000


class ExtensionContext:
    """扩展运行期上下文：能力门面 + 只读清单信息。

    故意只暴露纯数据与能力判定，**不暴露任何文件句柄、broker、
    输入对象**；扩展拿到的世界只有 `payload` 与自身清单声明。
    """

    def __init__(self, manifest: ExtensionManifest) -> None:
        self._manifest = manifest

    @property
    def extension_id(self) -> str:
        """扩展 ID。"""
        return self._manifest.extension_id

    @property
    def capabilities(self) -> tuple[str, ...]:
        """清单声明且已注册的能力（默认拒绝之外的请求一律失败）。"""
        return self._manifest.capabilities

    @property
    def limits(self) -> dict:
        """资源限额（声明值，供扩展自律）。"""
        return {
            "max_memory_mb": self._manifest.resource_limits.max_memory_mb,
            "max_runtime_s": self._manifest.resource_limits.max_runtime_s,
        }

    def has(self, capability: str) -> bool:
        """能力是否可用：已注册 **且** 在本扩展清单声明中（默认拒绝）。"""
        if capability not in EXTENSION_CAPABILITY_REGISTRY:
            return False
        return capability in self._manifest.capabilities

    def require(self, capability: str) -> None:
        """断言能力可用，否则抛 :class:`CapabilityDenied`（受控拒绝）。"""
        if not self.has(capability):
            raise CapabilityDenied(
                f"扩展 {self._manifest.extension_id!r} 未被授权能力 {capability!r}"
                "（能力白名单外或未在清单声明，默认拒绝）"
            )


def _emit(envelope: dict) -> None:
    """把结果信封写到 stdout 最后一行并立即冲刷。"""
    text = json.dumps(envelope, ensure_ascii=False, default=str)
    if len(text) > _MAX_VALUE_CHARS:
        text = json.dumps(
            {"ok": False, "reason": "output_limit", "detail": "结果信封超过回传大小上限"},
            ensure_ascii=False,
        )
    sys.stdout.write(f"{SENTINEL}{text}\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    """子进程主入口；返回进程退出码（受控失败为 0，基础设施失败非 0）。"""
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print("usage: python -m extension_runner.child <manifest_path> <payload_json>", file=sys.stderr)
        return 2
    manifest_path, payload_text = args[0], args[1]

    try:
        payload = json.loads(payload_text)
    except ValueError as exc:
        print(f"payload JSON 解析失败：{exc}", file=sys.stderr)
        return 2

    # 子进程侧二次校验（清单 + 代码包哈希）：深度防御
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001 —— 子进程边界，一切异常都要落盘
        print(f"manifest 校验失败：{exc}", file=sys.stderr)
        return 3

    code_dir = Path(manifest_path).resolve().parent
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))

    context = ExtensionContext(manifest)
    try:
        module = __import__(manifest.entry_module, fromlist=[manifest.entry_function])
        func = getattr(module, manifest.entry_function)
    except Exception as exc:  # noqa: BLE001
        print(f"entry 导入失败：{manifest.entry}：{exc}", file=sys.stderr)
        return 4

    try:
        value = func(context, payload)
        _emit({"ok": True, "value": value})
        return 0
    except CapabilityDenied as exc:
        _emit({"ok": False, "reason": "capability_denied", "detail": str(exc)})
        return 0  # 受控拒绝：不是崩溃
    except SystemExit as exc:  # 扩展主动退出视为受控结束（带回传码值）
        _emit({"ok": True, "value": {"system_exit": int(exc.code or 0)}})
        return 0
    except Exception as exc:  # noqa: BLE001 —— 扩展异常不炸子进程协议
        tail = traceback.format_exc(limit=4).strip().splitlines()[-1]
        _emit({"ok": False, "reason": "extension_error", "detail": tail})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
