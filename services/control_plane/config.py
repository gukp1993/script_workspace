"""控制面配置（CTL-001）。

token 为 None 时在构造配置（即启动）时自动生成随机 hex 令牌；令牌只存在
于进程内存中，由 ``__main__.py`` 打印到本地控制台，供本地桌面壳读取。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

#: 默认监听端口（仅回环绑定）
DEFAULT_PORT: int = 17653

#: 自动生成令牌的随机字节数（hex 编码后 40 个字符）
TOKEN_BYTES: int = 20

#: 视觉资产默认大小上限：20MB（CTL-004）
DEFAULT_ASSET_MAX_BYTES: int = 20 * 1024 * 1024


@dataclass
class ControlPlaneConfig:
    """控制面运行配置。

    Attributes:
        project_root:      工作区根目录；``projects/``、``sessions/``、
                           ``assets-store/`` 与 ``settings.json`` 均存放于此。
        token:             本地 API 访问令牌；None 时启动时自动生成随机 hex（CTL-001）。
        port:              仅回环绑定的监听端口（uvicorn 侧硬编码 127.0.0.1）。
        event_buffer_size: 事件有界缓冲条数（断线重连补发窗口，CTL-007）。
        asset_max_bytes:   视觉资产上传大小上限（默认 20MB，CTL-004）。
    """

    project_root: Path
    token: str | None = None
    port: int = DEFAULT_PORT
    event_buffer_size: int = 1024
    asset_max_bytes: int = DEFAULT_ASSET_MAX_BYTES

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root)
        if self.token is None:
            # CTL-001：每次启动自动生成随机令牌（不落盘）
            self.token = secrets.token_hex(TOKEN_BYTES)
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("token 不能为空")
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise ValueError("port 必须是 1~65535 的整数")
        if self.event_buffer_size < 1:
            raise ValueError("event_buffer_size 必须 >= 1")
        if self.asset_max_bytes < 1:
            raise ValueError("asset_max_bytes 必须 >= 1")
