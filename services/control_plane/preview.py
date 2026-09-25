"""实时预览抓帧端点（UI-004 后端基础）。

M1 基础版：``GET /api/v1/preview/shot`` 用 :class:`capture_api.MssAdapter`
抓一帧主屏 → OpenCV 编码 JPEG（质量 80）→ 二进制响应。预览属于带宽
优先场景，支持 ``?max_width=<int>`` 等比缩放（INTER_AREA）。

安全与稳健性约定：
- 路由挂载在 ``/api/v1`` 前缀下，自动受既有防火墙中间件保护
  （Host/Origin/``X-VAW-Token``，CTL-001/002），无需额外依赖注入；
- mss 实例绑定创建线程：FastAPI 同步端点在线程池执行，线程不定——
  因此**每次请求新建 MssAdapter（start→grab→stop）**，从根上规避
  跨线程复用问题；M2 换目标窗口采集时一并重构；
- 采集失败（适配器不可用/单次抓帧异常）返回 503 统一错误体，不回显
  敏感细节；``?max_width`` 非法时返回 400。
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, Query, Response
from starlette.responses import JSONResponse

from capture_api import MssAdapter

#: JPEG 编码质量（UI-004 预览：带宽优先，M1 固定 80）
JPEG_QUALITY: int = 80

#: 预览端点路径（供文档/测试引用）
PREVIEW_SHOT_PATH = "/api/v1/preview/shot"


def _encode_jpeg(pixels_rgb: np.ndarray) -> bytes:
    """RGB uint8 帧编码为 JPEG 字节串（质量 80）。"""
    # OpenCV 期望 BGR 通道序：反转颜色轴后再编码
    ok, buf = cv2.imencode(".jpg", pixels_rgb[:, :, ::-1], [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:  # pragma: no cover - 正常输入不会触发
        raise RuntimeError("JPEG 编码失败")
    return buf.tobytes()


def _scaled(pixels_rgb: np.ndarray, max_width: int) -> np.ndarray:
    """按 ``max_width`` 等比缩小帧（只缩不放，INTER_AREA 适合降采样）。"""
    height, width = pixels_rgb.shape[:2]
    if max_width >= width or width <= 0:
        return pixels_rgb
    new_width = max(1, int(max_width))
    new_height = max(1, round(height * new_width / width))
    return cv2.resize(pixels_rgb, (new_width, new_height), interpolation=cv2.INTER_AREA)


def register_preview_routes(app: FastAPI, config: Any = None) -> None:
    """注册预览路由（M1 基础版；config 预留，M2 换目标窗口采集时使用）。"""
    del config  # M2：按会话目标切换采集源时消费配置

    @app.get(PREVIEW_SHOT_PATH, response_class=Response, responses={200: {"content": {"image/jpeg": {}}}})
    def preview_shot(max_width: int | None = Query(default=None, ge=1, description="等比缩放的最大宽度（像素）")) -> Response:
        # mss 线程安全：每请求新建适配器（在本工作线程内 start/grab/stop）
        adapter = MssAdapter(monitor_index=0)
        try:
            adapter.start()
            frame = adapter.grab()
        except Exception:  # AdapterUnavailableError 及设备级故障
            return JSONResponse(
                status_code=503,
                content={"detail": {"error": "capture_unavailable", "message": "屏幕采集不可用，请检查显示器/采集适配器"}},
            )
        finally:
            adapter.stop()

        if frame is None:
            return JSONResponse(
                status_code=503,
                content={"detail": {"error": "capture_no_frame", "message": "本次未抓到帧，请稍后重试"}},
            )

        pixels = _scaled(frame.pixels, max_width) if max_width is not None else frame.pixels
        return Response(content=_encode_jpeg(pixels), media_type="image/jpeg")
