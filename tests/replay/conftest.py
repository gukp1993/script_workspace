"""tests/replay 公共配置：确保单仓包路径可导入。

根 pyproject 已配置 ``pythonpath = ["packages", "services", "apps"]``，
此处按同一约定再做一次防御性注入，保证从任意工作目录运行
``pytest tests/replay`` 时 import 路径一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _rel in ("packages", "services", "apps"):
    _path = str(_REPO / _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)
