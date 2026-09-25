"""发布包签名与校验（VER-006 简化版，SEC-004 协作）。

当前实现：
- :class:`HMACSigner`：hmac-sha256 对 manifest 字节签名（对称密钥）；
- :class:`NullSigner`：开发/本地测试用"空签名"（不提供任何防伪能力）；
- :func:`verify_signature` / :func:`signature_envelope` / :func:`verify_envelope`：
  签名值与"签名信封"（算法 + 签名值的 JSON）的生成与校验。

产品化路径（接口不变）：把 :class:`Signer` 协议替换为非对称签名
（Ed25519/RSA）或 Windows 代码签名实现即可——发布器与导入预检只依赖协议，
不感知具体算法。导入侧约定：未知/缺失签名默认**不启用 real_input**
（见 :mod:`release_kit.transfer` 的 ``unsigned_package`` 处理）。
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Protocol, runtime_checkable


@runtime_checkable
class Signer(Protocol):
    """签名器协议：发布器签名、导入预检验证只依赖本接口。"""

    #: 算法标识（写入签名信封，导入侧据此匹配验证器）
    algorithm: str

    def sign(self, payload: bytes) -> str:
        """返回签名字符串（十六进制）。"""
        ...

    def verify(self, payload: bytes, signature: str) -> bool:
        """校验签名；任何异常都应返回 False 而非抛出。"""
        ...


class HMACSigner:
    """hmac-sha256 对称签名器（VER-006 简化实现）。"""

    algorithm: str = "hmac-sha256"

    def __init__(self, key: bytes | str) -> None:
        self._key = bytes(key, encoding="utf-8") if isinstance(key, str) else bytes(key)

    def sign(self, payload: bytes) -> str:
        """计算 HMAC-SHA256 签名（十六进制小写）。"""
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def verify(self, payload: bytes, signature: str) -> bool:
        """常数时间比较，防时序侧信道；输入非法一律 False。"""
        if not isinstance(signature, str):
            return False
        return hmac.compare_digest(self.sign(payload), signature)


class NullSigner:
    """开发用空签名器：签名恒为 ``"unsigned"``、校验恒通过。

    仅用于本地开发/单测；导入侧把 ``null`` 算法视为"未签名"处理
    （默认不允许启用 real_input，见 transfer 模块说明）。
    """

    algorithm: str = "null"

    def sign(self, payload: bytes) -> str:  # noqa: ARG002 - 协议要求同形参
        return "unsigned"

    def verify(self, payload: bytes, signature: str) -> bool:  # noqa: ARG002
        return True


def verify_signature(payload: bytes, signature: str, signer: Signer) -> bool:
    """用指定签名器校验 manifest 字节的签名（VER-006 入口）。"""
    return bool(signer.verify(payload, signature))


def signature_envelope(payload: bytes, signer: Signer) -> dict[str, str]:
    """生成签名信封：``{"algorithm": ..., "signature": ...}``。

    信封随包分发（manifest.sig），导入侧按算法匹配验证器。
    """
    return {"algorithm": signer.algorithm, "signature": signer.sign(payload)}


def verify_envelope(payload: bytes, envelope: dict[str, str], signer: Signer) -> bool:
    """校验签名信封：算法匹配且签名有效才返回 True。"""
    if envelope.get("algorithm") != signer.algorithm:
        return False
    return verify_signature(payload, str(envelope.get("signature", "")), signer)
