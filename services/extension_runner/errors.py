"""扩展 Runner 异常类型（PLG-001/002）。

约定：
- 所有异常都继承 :class:`ExtensionError`，主进程捕获后转化为审计事件，
  **绝不因扩展问题波及主进程**（PLG-002 验收要求）；
- :class:`ManifestError` 表示清单缺失/非法（含哈希不符）——加载期拒绝；
- :class:`ExtensionRunError` 表示运行期失败（超时/崩溃/输出超限）；
- :class:`CapabilityDenied` 由子进程内能力门面抛出，经结果信封回传，
  属于**受控拒绝**而非崩溃。
"""

from __future__ import annotations


class ExtensionError(Exception):
    """扩展体系基础异常。"""


class ManifestError(ExtensionError):
    """扩展清单缺失、非法或代码包哈希不符。"""


class ExtensionRunError(ExtensionError):
    """扩展运行失败（超时/崩溃/输出超限/载荷非法）。"""


class CapabilityDenied(ExtensionError):
    """扩展请求了未声明的能力（默认拒绝）。"""
