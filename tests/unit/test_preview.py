"""预览抓帧端点单测（UI-004 后端基础）。

覆盖：
- 携带令牌请求成功：200 + image/jpeg，且可被 OpenCV 解码为有效帧；
- 缺失令牌 401（跟随既有防火墙中间件，CTL-001）；
- ``?max_width=`` 等比缩放生效：解码宽度 <= max_width 且小于原始宽度；
- 非法 ``max_width``（0/负数）被 FastAPI 校验拒绝（422）。

本机有显示器：真抓一帧主屏（mss 每请求新建实例，线程池内执行安全）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from control_plane.app import create_app
from control_plane.config import ControlPlaneConfig
from control_plane.preview import JPEG_QUALITY, PREVIEW_SHOT_PATH

TOKEN = "preview-test-token"
PORT = 17653
HEADERS = {"X-VAW-Token": TOKEN}
BASE_URL = f"http://127.0.0.1:{PORT}"


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    """构建带预览路由的控制面测试客户端（无真实网络监听）。"""
    config = ControlPlaneConfig(project_root=tmp_path, token=TOKEN, port=PORT)
    with TestClient(create_app(config), base_url=BASE_URL) as c:
        yield c


def _decode_jpeg(data: bytes) -> np.ndarray:
    """JPEG 字节解码为 BGR ndarray（断言失败时给出可读信息）。"""
    arr = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    assert image is not None, "响应体不是可解码的 JPEG"
    return image


def test_preview_shot_success_returns_jpeg(client: TestClient) -> None:
    """带令牌真抓一帧：200、image/jpeg、非空且可解码。"""
    r = client.get(PREVIEW_SHOT_PATH, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/jpeg")
    assert len(r.content) > 100  # 空屏 JPEG 也远大于此
    image = _decode_jpeg(r.content)
    assert image.ndim == 3 and image.shape[2] == 3
    assert image.shape[0] > 0 and image.shape[1] > 0


def test_preview_shot_requires_token(client: TestClient) -> None:
    """缺失令牌 401（防火墙中间件对 /api/v1 全量生效）。"""
    r = client.get(PREVIEW_SHOT_PATH)
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert detail["error"] == "unauthorized"
    # 错误体不回显令牌等敏感细节
    assert TOKEN not in r.text


def test_preview_shot_wrong_token_401(client: TestClient) -> None:
    """错误令牌同样 401。"""
    r = client.get(PREVIEW_SHOT_PATH, headers={"X-VAW-Token": "wrong-token"})
    assert r.status_code == 401


def test_preview_shot_max_width_scales_down(client: TestClient) -> None:
    """``max_width`` 等比缩放：解码宽度 <= max_width 且小于原始宽度。"""
    full = _decode_jpeg(client.get(PREVIEW_SHOT_PATH, headers=HEADERS).content)
    source_width = full.shape[1]
    target = max(1, source_width // 4)  # 远小于原始宽度的目标宽

    r = client.get(PREVIEW_SHOT_PATH, params={"max_width": target}, headers=HEADERS)
    assert r.status_code == 200, r.text
    scaled = _decode_jpeg(r.content)

    assert scaled.shape[1] <= target
    assert scaled.shape[1] < source_width
    # 等比：高度按同一比例缩放（允许 ±1 像素舍入误差）
    expected_height = round(full.shape[0] * scaled.shape[1] / source_width)
    assert abs(scaled.shape[0] - expected_height) <= 1


def test_preview_shot_max_width_larger_than_screen_is_noop(client: TestClient) -> None:
    """``max_width`` 大于屏幕宽度时不放大（只缩不放）。"""
    full = _decode_jpeg(client.get(PREVIEW_SHOT_PATH, headers=HEADERS).content)
    r = client.get(PREVIEW_SHOT_PATH, params={"max_width": full.shape[1] * 4}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert _decode_jpeg(r.content).shape == full.shape


@pytest.mark.parametrize("bad", ["0", "-10"])
def test_preview_shot_invalid_max_width_rejected(client: TestClient, bad: str) -> None:
    """非法 ``max_width``（<1）被查询参数校验拒绝。"""
    r = client.get(PREVIEW_SHOT_PATH, params={"max_width": bad}, headers=HEADERS)
    assert r.status_code == 422


def test_jpeg_quality_constant() -> None:
    """编码质量固定 80（UI-004 预览带宽优先约定）。"""
    assert JPEG_QUALITY == 80
