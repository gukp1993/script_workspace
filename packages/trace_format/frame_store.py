"""帧内容寻址存储与索引（TRC-003）。

- 内容寻址：``ref = sha256(像素原始字节)``（64 位小写十六进制，与
  arena_lab 的逐帧哈希同口径），重复内容自动去重（同 ref 只落一个 blob）；
- 存储：``<root>/frames/<ref>.npy``（numpy 原生格式，``get`` 逐字节还原）；
- 索引：``<root>/index.jsonl`` 追加写，每次 ``put`` 记一行
  ``{"ref", "shape", "dtype", "deduplicated"}``；
- 隐私模式：``enabled=False`` 时 ``put`` 直接返回 ``None``、不落任何文件，
  ``get`` 抛 :class:`FrameStorageDisabled`（原始帧按策略可整体关闭）；
- 引用不存在：``get`` 抛带引用原文的 :class:`KeyError`；引用格式非法
  （非 64 位十六进制，可能含路径穿越）抛 :class:`ValueError`。

去重语义：内容相同 -> 同一 ref -> 不重写文件，只追加一条
``deduplicated=True`` 的索引行（保留"被引用次数"信息）；``refs()``
按首次出现顺序返回去重后的引用列表。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["FrameStorageDisabled", "FrameStore", "INDEX_FILENAME", "BLOBS_DIRNAME"]

#: 索引文件名（相对 root）。
INDEX_FILENAME: str = "index.jsonl"

#: 帧文件子目录名（相对 root）。
BLOBS_DIRNAME: str = "frames"

#: 合法帧引用：64 位小写十六进制（sha256）。
_FRAME_REF_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")


class FrameStorageDisabled(Exception):
    """帧存储处于关闭状态（隐私模式）时的读取/访问错误。"""


class FrameStore:
    """内容寻址的原始帧存储（TRC-003）。

    Args:
        root:    存储根目录（自动创建 ``frames/`` 子目录与索引文件）。
        enabled: 是否启用；``False`` 为隐私模式——不写任何文件，
                 ``put`` 返回 ``None``，``get`` 抛 :class:`FrameStorageDisabled`。
    """

    def __init__(self, root: str | Path, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = bool(enabled)
        self.blobs_dir = self.root / BLOBS_DIRNAME
        self.index_path = self.root / INDEX_FILENAME
        if self.enabled:
            # 隐私模式下绝不创建任何目录/文件。
            self.blobs_dir.mkdir(parents=True, exist_ok=True)
        #: 本次进程内已见过的引用（内存去重，避免反复 stat 磁盘）。
        self._known: set[str] = set()

    # ------------------------------------------------------------------ 写入

    @staticmethod
    def ref_for(pixels: np.ndarray) -> str:
        """计算像素内容的寻址引用（sha256(原始字节)，十六进制小写）。"""
        return hashlib.sha256(np.ascontiguousarray(pixels).tobytes()).hexdigest()

    def put(self, pixels: np.ndarray) -> str | None:
        """存储一帧并返回内容寻址引用；内容已存在时只追加索引（去重）。

        隐私模式（``enabled=False``）下不产生任何副作用，返回 ``None``。
        """
        if not self.enabled:
            return None
        if not isinstance(pixels, np.ndarray):
            raise TypeError(f"pixels 必须是 numpy.ndarray，得到 {type(pixels).__name__}")
        arr = np.ascontiguousarray(pixels)
        ref = hashlib.sha256(arr.tobytes()).hexdigest()
        path = self.blobs_dir / f"{ref}.npy"
        deduplicated = ref in self._known or path.exists()
        if not deduplicated:
            np.save(path, arr, allow_pickle=False)
        self._append_index(
            {
                "ref": ref,
                "shape": [int(v) for v in arr.shape],
                "dtype": str(arr.dtype),
                "deduplicated": deduplicated,
            }
        )
        self._known.add(ref)
        return ref

    # ------------------------------------------------------------------ 读取

    def get(self, ref: str) -> np.ndarray:
        """按引用取回帧像素（与存入逐字节一致）。

        Raises:
            FrameStorageDisabled: 隐私模式下调用。
            ValueError: 引用格式非法（非 64 位小写十六进制）。
            KeyError: 引用不存在（消息带引用原文）。
        """
        if not self.enabled:
            raise FrameStorageDisabled(f"帧存储已关闭（隐私模式），无法读取引用 {ref!r}")
        path = self.blob_path(ref)
        if not path.exists():
            raise KeyError(f"帧引用不存在: {ref}")
        return np.load(path, allow_pickle=False)

    def has(self, ref: str) -> bool:
        """引用是否已存储（隐私模式恒 False；非法引用返回 False）。"""
        if not self.enabled or not isinstance(ref, str) or not _FRAME_REF_RE.fullmatch(ref):
            return False
        return (self.blobs_dir / f"{ref}.npy").exists()

    def blob_path(self, ref: str) -> Path:
        """引用对应的 blob 文件路径（引用格式非法抛 ValueError）。"""
        return self.blobs_dir / f"{self._check_ref(ref)}.npy"

    # ------------------------------------------------------------------ 索引

    def read_index(self) -> list[dict[str, Any]]:
        """读取索引行（JSONL 逐行解析；文件不存在返回空列表）。"""
        if not self.enabled or not self.index_path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.index_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(dict(json.loads(line)))
        return out

    def refs(self) -> list[str]:
        """索引中出现过的全部引用（按首次出现顺序去重）。"""
        seen: dict[str, None] = {}
        for record in self.read_index():
            ref = record.get("ref")
            if isinstance(ref, str):
                seen.setdefault(ref, None)
        return list(seen)

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_ref(ref: str) -> str:
        """校验引用格式：64 位小写十六进制（同时阻断路径穿越）。"""
        if not isinstance(ref, str) or not _FRAME_REF_RE.fullmatch(ref):
            raise ValueError(f"非法帧引用 {ref!r}：必须是 64 位小写十六进制（sha256）")
        return ref

    def _append_index(self, record: dict[str, Any]) -> None:
        """索引追加一行（JSONL，确定性键序）。"""
        with self.index_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
