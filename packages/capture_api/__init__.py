"""capture_api——屏幕采集与帧管线（M1/E05，CAP-001~008 基础版）。

公共 API：
- 数据结构：``Frame`` / ``FrameMeta`` / ``SourceInfo``（CAP-001）
- 协议与错误：``CaptureSource`` / ``AdapterUnavailableError``
- Fake：``FakeCaptureSource`` / ``make_frame``（测试复用）
- 真实适配器：``DxcamAdapter``（CAP-002）/ ``MssAdapter``（CAP-003）
- 环形缓冲：``FrameRingBuffer``（CAP-004）
- 管线：``roi_crop`` / ``resize`` / ``convert_bgr_rgb`` / ``FramePipeline``
  （CAP-005/006）
- 故障切换：``CaptureChain``（CAP-007 基础版）

所有适配器输出统一为 ``(H, W, 3)`` uint8 **RGB**，时间戳为单调时钟秒。
"""

from __future__ import annotations

from capture_api.base import AdapterUnavailableError, CaptureSource
from capture_api.chain import CaptureChain
from capture_api.dxcam_adapter import DXCAM_AVAILABLE, DxcamAdapter
from capture_api.fake import FakeCaptureSource, make_frame
from capture_api.frames import Frame, FrameMeta, Rect, SourceInfo
from capture_api.mss_adapter import MSS_AVAILABLE, MssAdapter
from capture_api.pipeline import (
    FramePipeline,
    SubscriberStats,
    convert_bgr_rgb,
    resize,
    roi_crop,
)
from capture_api.ring_buffer import FrameRingBuffer

__all__ = [
    # 数据结构（CAP-001）
    "Frame",
    "FrameMeta",
    "SourceInfo",
    "Rect",
    # 协议与错误
    "CaptureSource",
    "AdapterUnavailableError",
    # Fake
    "FakeCaptureSource",
    "make_frame",
    # 真实适配器
    "DxcamAdapter",
    "DXCAM_AVAILABLE",
    "MssAdapter",
    "MSS_AVAILABLE",
    # 环形缓冲（CAP-004）
    "FrameRingBuffer",
    # 管线（CAP-005/006）
    "roi_crop",
    "resize",
    "convert_bgr_rgb",
    "FramePipeline",
    "SubscriberStats",
    # 故障切换（CAP-007）
    "CaptureChain",
]
