"""control_plane——控制面、本地 API 与存储（E10：CTL-001~008、CTL-009 基础版）。

职责：
- 仅回环绑定的本地 API 与随机令牌（CTL-001，:mod:`control_plane.config` /
  :mod:`control_plane.security`）；
- Origin/Host 校验与 ``/api/v1`` 版本前缀（CTL-002）；
- 项目/目标/策略/检测器/状态机/标定 CRUD，写前校验 + 乐观锁（CTL-003，
  :mod:`control_plane.storage`）；
- 视觉资产文件存储：类型/大小限制、路径穿越防护、SHA-256 去重（CTL-004，
  :mod:`control_plane.assets`）；
- 运行会话生命周期与单 RealInput 实例约束（CTL-005/006，
  :mod:`control_plane.sessions`）；
- WebSocket 事件总线：单调 seq、断线重连补发、resync 兜底（CTL-007，
  :mod:`control_plane.events`）;
- 配置与安全默认值（CTL-008，:mod:`control_plane.settings`）；
- 启动恢复：非终态会话标记 interrupted（CTL-009 基础版，
  :mod:`control_plane.recovery`）。

安全边界：
- uvicorn 硬编码绑定 127.0.0.1（见 ``__main__.py``），令牌仅打印本地控制台；
- 无 CORS 放行中间件；帧像素数据禁止经 WS/REST 传输（像素走文件存储）。
"""

from __future__ import annotations

# 版本号先于子模块导入定义，避免 app.py 的 `from control_plane import __version__`
# 在包初始化过程中读到未定义属性（循环导入）。
__version__ = "0.1.0"

from control_plane.app import create_app
from control_plane.config import DEFAULT_PORT, ControlPlaneConfig

__all__ = ["DEFAULT_PORT", "ControlPlaneConfig", "create_app", "__version__"]
