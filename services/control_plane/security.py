"""仅回环本地 API 的安全防线（CTL-001/002）。

规则（按检查顺序）：
1. Host 头必须是回环地址（127.0.0.1 / localhost / ::1），否则 403——
   防止 DNS 重绑定把本机 API 暴露给外部域名；
2. 携带 Origin 头时必须是 ``http://127.0.0.1:<port>`` 或
   ``http://localhost:<port>``，否则 403——防止浏览器跨站调用；
   未携带 Origin 的非浏览器客户端（curl/桌面壳）不受影响；
3. 所有 ``/api/v1/*`` 请求必须携带 ``X-VAW-Token`` 请求头且与配置令牌
   一致（常数时间比较），否则 401；401/403 响应体不回显令牌等敏感细节。

其他约定：
- 本服务**不注册任何 CORS 放行中间件**：跨源浏览器脚本拿不到响应；
- API 版本前缀固定为 ``/api/v1``（CTL-002），其余版本前缀由路由表 404 拒绝；
- WebSocket 无法自定义请求头，令牌经查询参数传递（见 events.py），
  由 :func:`ws_security_reject` 在握手时执行同样三条检查。
"""

from __future__ import annotations

import secrets
from typing import Any, Awaitable, Callable

from fastapi import Request, Response, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from control_plane.config import ControlPlaneConfig

#: 令牌请求头名称（CTL-001）
TOKEN_HEADER = "X-VAW-Token"

#: WebSocket 查询参数中的令牌字段名
TOKEN_QUERY = "token"

#: 回环主机名白名单
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def token_matches(provided: str | None, token: str) -> bool:
    """常数时间比较请求令牌与配置令牌（防时序侧信道）。"""
    if not provided or not token:
        return False
    return secrets.compare_digest(provided, token)


def host_is_loopback(host_header: str | None) -> bool:
    """Host 头是否指向回环地址（容忍带端口 / IPv6 方括号形式）。"""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8080
        end = host.find("]")
        if end == -1:
            return False
        host = host[1:end]
    elif host.count(":") == 1:  # 127.0.0.1:17653
        host = host.split(":", 1)[0]
    return host in _LOOPBACK_HOSTS


def origin_is_allowed(origin: str | None, port: int) -> bool:
    """Origin 是否在白名单内；未携带 Origin 视为非浏览器客户端，放行。"""
    if origin is None:
        return True
    return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def http_firewall(
    config: ControlPlaneConfig,
) -> Callable[[Request, RequestResponseEndpoint], Awaitable[Response]]:
    """生成 http 作用域防火墙中间件（经 BaseHTTPMiddleware 挂载）。

    仅作用于 http 作用域；WebSocket 握手走 :func:`ws_security_reject`。
    """

    async def dispatch(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not host_is_loopback(request.headers.get("host")):
            return _deny(403, "forbidden_host", "Host 必须是本机回环地址")
        if not origin_is_allowed(request.headers.get("origin"), config.port):
            return _deny(403, "forbidden_origin", "Origin 不在允许列表")
        # 令牌只保护 API 路径：静态 SPA 资源（/assets、index.html）不含数据，
        # 所有数据通道都在 /api/* 之下并逐一鉴权；WS 握手在端点内自校验。
        if request.url.path.startswith("/api"):
            provided = request.headers.get(TOKEN_HEADER)
            if request.method == "GET":
                # GET 兼容 ?token= 查询参数（与 WS 握手同约定）：浏览器首次
                # 导航/加载页面时不带自定义请求头，桌面壳经该参数注入令牌。
                provided = provided or request.query_params.get(TOKEN_QUERY)
            if not token_matches(provided, config.token or ""):
                # 401 响应体不回显期望令牌等敏感细节（CTL-001）
                return _deny(401, "unauthorized", "缺少或错误的访问令牌")
        return await call_next(request)

    return dispatch


def ws_security_reject(websocket: WebSocket, config: ControlPlaneConfig) -> int | None:
    """WebSocket 握手安全检查；返回拒绝关闭码，None 表示放行。"""
    if not host_is_loopback(websocket.headers.get("host")):
        return 4403
    if not origin_is_allowed(websocket.headers.get("origin"), config.port):
        return 4403
    if not token_matches(websocket.query_params.get(TOKEN_QUERY), config.token or ""):
        return 4401
    return None


def mount_firewall(app: Any, config: ControlPlaneConfig) -> None:
    """把 http 防火墙挂载到 FastAPI 应用（不注册任何 CORS 中间件）。"""
    app.add_middleware(BaseHTTPMiddleware, dispatch=http_firewall(config))


def _deny(status_code: int, code: str, message: str) -> JSONResponse:
    """统一的拒绝响应体（不含任何敏感细节）。"""
    return JSONResponse(status_code=status_code, content={"detail": {"error": code, "message": message}})
