"""E05 屏幕采集与帧管线单测（CAP-001~008 基础版）。

覆盖：帧数据结构、环形缓冲（并发无半帧/单调 seq/过期识别）、
ROI 裁剪与转换、Fake 驱动的管线背压（快慢订阅者）、采集链故障切换、
以及真实采集冒烟（不可用环境一律 skip，不 fail）。
"""

from __future__ import annotations

import dataclasses
import threading
import time

import numpy as np
import pytest

from capture_api import (
    AdapterUnavailableError,
    CaptureChain,
    DXCAM_AVAILABLE,
    DxcamAdapter,
    FakeCaptureSource,
    Frame,
    FrameMeta,
    FramePipeline,
    FrameRingBuffer,
    MSS_AVAILABLE,
    MssAdapter,
    convert_bgr_rgb,
    make_frame,
    resize,
    roi_crop,
)
from common.clock import FakeClock

W, H = 64, 48  # 合成帧默认尺寸


# ---------------------------------------------------------------------------
# CAP-001：帧数据结构
# ---------------------------------------------------------------------------


def test_frame_meta_frozen_and_frame_helpers() -> None:
    """FrameMeta 不可变；Frame 的宽高/校验和/统一色辅助正确。"""
    meta = FrameMeta(
        seq=7, ts_monotonic=1.5, adapter="fake", source_width=W, source_height=H
    )
    frame = make_frame(7, W, H, (10, 20, 30), 1.5)
    assert frame.meta == meta
    assert frame.width == W and frame.height == H
    assert frame.checksum() == (10 + 20 + 30) * W * H
    assert frame.is_uniform()
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.meta.seq = 8  # type: ignore[misc]


def test_frame_copy_is_independent() -> None:
    """copy() 深拷贝像素：改动副本不影响原帧。"""
    frame = make_frame(1, 4, 3, (1, 2, 3))
    clone = frame.copy()
    clone.pixels[:] = 0
    assert frame.checksum() == 6 * 4 * 3
    assert clone.checksum() == 0


# ---------------------------------------------------------------------------
# CAP-004：环形缓冲
# ---------------------------------------------------------------------------


def test_ring_buffer_rejects_bad_capacity_and_regressing_seq() -> None:
    """容量必须为正；seq 回退/重复直接拒绝（单调性）。"""
    with pytest.raises(ValueError):
        FrameRingBuffer(0)
    buf = FrameRingBuffer(4)
    buf.put(make_frame(1))
    buf.put(make_frame(2))
    with pytest.raises(ValueError):
        buf.put(make_frame(2))  # 重复 seq
    with pytest.raises(ValueError):
        buf.put(make_frame(1))  # 回退 seq
    assert buf.last_seq == 2


def test_ring_buffer_capacity_wrap_and_overwrite_counter() -> None:
    """容量循环覆盖最旧帧，覆盖计数与最新 seq 正确。"""
    buf = FrameRingBuffer(3)
    for seq in range(1, 6):
        buf.put(make_frame(seq, 4, 3, (seq, 0, 0)))
    assert len(buf) == 3
    assert buf.overwritten == 2
    newest = buf.latest()
    assert newest is not None and newest.meta.seq == 5
    assert newest.pixels[0, 0, 0] == 5


def test_ring_buffer_latest_returns_independent_copy() -> None:
    """latest() 返回拷贝：消费侧改动不影响缓冲内的帧。"""
    buf = FrameRingBuffer(2)
    buf.put(make_frame(1, 4, 3, (9, 9, 9)))
    got = buf.latest()
    assert got is not None
    got.pixels[:] = 0
    again = buf.latest()
    assert again is not None and again.checksum() == 9 * 3 * 4 * 3  # 值9×3通道×12像素


def test_ring_buffer_expiry_marks_and_returns_none() -> None:
    """过期帧：返回 None 且过期可识别（expired_count / last_expired_seq）。"""
    clock = FakeClock(start=100.0)
    buf = FrameRingBuffer(2)
    buf.put(make_frame(1, 4, 3, ts_monotonic=clock.now()))
    assert buf.latest(max_age_s=1.0, clock=clock) is not None  # 新鲜
    clock.advance(2.0)
    assert buf.latest(max_age_s=1.0, clock=clock) is None  # 过期
    assert buf.expired_count == 1
    assert buf.last_expired_seq == 1
    # 不带过期参数时仍可读出（标记不销毁帧）
    assert buf.latest().meta.seq == 1


def test_ring_buffer_empty_returns_none() -> None:
    """空缓冲 latest 返回 None，不计过期。"""
    buf = FrameRingBuffer(2)
    clock = FakeClock()
    assert buf.latest(max_age_s=1.0, clock=clock) is None
    assert buf.expired_count == 0


def test_ring_buffer_concurrent_no_torn_frames() -> None:
    """单生产者 + 双消费者并发：永读不到半写帧（校验和一致），seq 各自单调。"""
    buf = FrameRingBuffer(8)
    width, height, rounds = 16, 12, 400
    torn: list[str] = []
    non_monotonic: list[str] = []

    def produce() -> None:
        for seq in range(1, rounds + 1):
            value = (seq % 250) + 1  # 每帧唯一填充值（非 0，便于区分空帧）
            frame = make_frame(seq, width, height, (value, value, value))
            buf.put(frame)

    def consume(tag: str) -> None:
        last_seq = 0
        # latest() 是非破坏性读取：以"观察到最新 seq"为终止条件
        while last_seq < rounds:
            frame = buf.latest()
            if frame is None:
                time.sleep(0.001)
                continue
            # latest() 重复读到同一最新帧是合法语义；只有严格回退才算违规
            if frame.meta.seq < last_seq:
                non_monotonic.append(f"{tag}:{frame.meta.seq}<{last_seq}")
            last_seq = max(last_seq, frame.meta.seq)
            value = frame.pixels.flat[0]
            expected = int(value) * width * height * 3
            if frame.checksum() != expected:
                torn.append(f"{tag}:seq={frame.meta.seq}")

    producer = threading.Thread(target=produce)
    consumers = [threading.Thread(target=consume, args=(f"c{i}",)) for i in range(2)]
    for t in (producer, *consumers):
        t.start()
    producer.join(timeout=30.0)
    for t in consumers:
        t.join(timeout=30.0)
        assert not t.is_alive(), f"消费线程未终止: {t.name}"
    assert not torn, f"读到半写帧: {torn[:5]}"
    assert not non_monotonic, f"消费侧 seq 回退: {non_monotonic[:5]}"
    assert buf.put_count == rounds


# ---------------------------------------------------------------------------
# CAP-005：ROI 裁剪 / 缩放 / 色彩转换
# ---------------------------------------------------------------------------


def _gradient_frame(seq: int = 1, width: int = 32, height: int = 16) -> Frame:
    """像素值 = x + y*width 的梯度帧，便于逐像素断言裁剪正确性。"""
    frame = make_frame(seq, width, height, (0, 0, 0))
    for y in range(height):
        for x in range(width):
            frame.pixels[y, x] = (x + y * width) % 256
    return frame


def test_roi_crop_center_pixels_exact() -> None:
    """中心 ROI 裁剪：像素值与手工切片完全一致，seq 沿用，源尺寸不变。"""
    frame = _gradient_frame()
    crop = roi_crop(frame, (0.25, 0.5, 0.5, 0.25))
    assert crop.pixels.shape == (4, 16, 3)
    np.testing.assert_array_equal(crop.pixels, frame.pixels[8:12, 8:24])
    assert crop.meta.seq == frame.meta.seq
    assert crop.meta.source_width == 32 and crop.meta.source_height == 16


def test_roi_crop_identity_for_full_frame() -> None:
    """(0, 0, 1, 1) 全幅 ROI 等价原帧。"""
    frame = _gradient_frame()
    crop = roi_crop(frame, (0.0, 0.0, 1.0, 1.0))
    np.testing.assert_array_equal(crop.pixels, frame.pixels)


def test_roi_crop_strict_out_of_bounds_raises() -> None:
    """严格模式：越界（右/下超出）与负坐标均 ValueError，不崩溃。"""
    frame = make_frame(1, 32, 16)
    with pytest.raises(ValueError, match="越界"):
        roi_crop(frame, (0.5, 0.0, 0.6, 1.0))  # x+w > 1
    with pytest.raises(ValueError, match="越界"):
        roi_crop(frame, (0.0, 0.5, 1.0, 0.6))  # y+h > 1
    with pytest.raises(ValueError, match="范围"):
        roi_crop(frame, (-0.1, 0.0, 0.5, 0.5))
    with pytest.raises(ValueError, match="范围"):
        roi_crop(frame, (0.0, 0.0, 1.2, 0.5))


def test_roi_crop_clip_mode_safe_crop() -> None:
    """宽松模式：越界安全裁剪到画面内（CAP-009 的安全裁剪路径）。"""
    frame = _gradient_frame(1, 32, 16)
    crop = roi_crop(frame, (0.5, 0.5, 0.75, 0.75), on_out_of_bounds="clip")
    assert crop.pixels.shape == (8, 16, 3)
    np.testing.assert_array_equal(crop.pixels, frame.pixels[8:16, 16:32])


def test_roi_crop_invalid_roi_raises() -> None:
    """空 ROI / 非四元组 / 零宽高均报错。"""
    frame = make_frame(1, 8, 8)
    for bad in [(0.0, 0.0, 0.0, 0.5), (0.0, 0.0, 0.5, 0.0), (0.5, 0.5, 0.0, 0.0)]:
        with pytest.raises(ValueError, match="宽高|范围"):
            roi_crop(frame, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="四元组"):
        roi_crop(frame, (0.0, 0.0, 0.5))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="仅支持"):
        roi_crop(frame, (0.0, 0.0, 0.5, 0.5), on_out_of_bounds="yolo")


def test_resize_dimensions_and_uniform_content() -> None:
    """缩放：尺寸正确；纯色帧缩放后仍为同色（内容不漂移）。"""
    frame = make_frame(1, 64, 48, (30, 60, 90))
    small = resize(frame.pixels, 32, 24)
    assert small.shape == (24, 32, 3) and small.dtype == np.uint8
    assert int(small.flat[0]) == 30 and int(small.flat[1]) == 60
    big = resize(small, 64, 48)
    assert big.shape == (48, 64, 3)
    assert int(big[0, 0, 2]) == 90
    # 原尺寸直通
    assert resize(frame.pixels, 64, 48) is frame.pixels


def test_resize_rejects_invalid_input() -> None:
    """非法输入：非 (H, W, 3) 像素或非正尺寸报 ValueError。"""
    with pytest.raises(ValueError):
        resize(np.zeros((4, 4), dtype=np.uint8), 2, 2)
    with pytest.raises(ValueError):
        resize(np.zeros((4, 4, 3), dtype=np.uint8), 0, 2)


def test_convert_bgr_rgb_channel_swap_and_roundtrip() -> None:
    """BGR↔RGB：通道互换正确；两次转换往返还原。"""
    pixels = np.zeros((2, 3, 3), dtype=np.uint8)
    pixels[0, 0] = (1, 2, 3)  # 按 BGR 解释
    swapped = convert_bgr_rgb(pixels)
    assert tuple(swapped[0, 0]) == (3, 2, 1)
    np.testing.assert_array_equal(convert_bgr_rgb(swapped), pixels)


# ---------------------------------------------------------------------------
# Fake 采集源
# ---------------------------------------------------------------------------


def test_fake_source_fifo_latest_and_diagnostics() -> None:
    """Fake 源按 FIFO 出帧，耗尽返回 None；latest/诊断计数正确。"""
    frames = [make_frame(seq, 4, 3, (seq, 0, 0)) for seq in range(1, 4)]
    source = FakeCaptureSource(frames, name="fake-a")
    source.start()
    assert source.grab().meta.seq == 1
    assert source.latest().meta.seq == 1
    assert source.grab().meta.seq == 2
    source.inject(make_frame(99, 4, 3))  # 排在队尾（FIFO）：先 3 后 99
    assert source.grab().meta.seq == 3
    assert source.grab().meta.seq == 99
    assert source.grab() is None  # 耗尽
    assert source.none_count == 1 and source.grabbed_count == 4
    diag = source.diagnostics()
    assert diag["adapter"] == "fake-a" and diag["grabbed"] == 4


def test_fake_source_provider_and_injected_failure() -> None:
    """provider 回调供帧；fail_on_grab 注入设备级异常（供采集链测试）。"""
    seq_box = [0]

    def provider() -> Frame | None:
        seq_box[0] += 1
        return make_frame(seq_box[0], 4, 3, (5, 5, 5))

    source = FakeCaptureSource(provider=provider)
    assert source.grab().meta.seq == 1
    source.set_fail(RuntimeError("device lost"))
    with pytest.raises(RuntimeError, match="device lost"):
        source.grab()
    source.set_fail(None)
    assert source.grab() is not None


# ---------------------------------------------------------------------------
# CAP-005/006：FramePipeline 背压与订阅者隔离
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout_s: float = 5.0) -> bool:
    """轮询等待条件成立（测试专用，避免 sleep 竞态）。"""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_pipeline_fast_subscriber_receives_all_without_drops() -> None:
    """快订阅者：全收且零丢帧。"""
    source = FakeCaptureSource(
        [make_frame(seq, 4, 3, (seq, 0, 0)) for seq in range(1, 21)]
    )
    pipe = FramePipeline(source)
    seen: list[int] = []
    pipe.subscribe("fast", lambda f: seen.append(f.meta.seq))
    pipe.start()
    try:
        # 小间隔泵送：快订阅者（微秒级回调）每帧都能排空 -> 全收零丢
        assert pipe.run(20, interval_s=0.01) == 20
        assert _wait_until(lambda: len(seen) == 20)
        assert seen == list(range(1, 21))
        stats = pipe.subscriber_stats("fast")
        assert stats.dropped == 0 and stats.processed == 20
    finally:
        pipe.stop()


def test_pipeline_slow_subscriber_drops_own_frames_only() -> None:
    """慢订阅者：只丢自己的帧并计数；快订阅者全收（隔离，CAP-006）。"""
    source = FakeCaptureSource(
        [make_frame(seq, 4, 3, (seq, 0, 0)) for seq in range(1, 25)]
    )
    pipe = FramePipeline(source)
    seen: list[int] = []
    pipe.subscribe("fast", lambda f: seen.append(f.meta.seq))
    pipe.subscribe("slow", lambda f: time.sleep(0.03), queue_size=2)
    pipe.start()
    try:
        pump_started = time.perf_counter()
        pumped = pipe.run(24, interval_s=0.01)  # 10ms 间隔，模拟真实采集节奏
        pump_elapsed = time.perf_counter() - pump_started
        # 生产者不被阻塞：24 帧总耗时远小于慢消费者串行总耗时（24*30ms）
        assert pumped == 24
        assert pump_elapsed < 0.5, f"生产者疑似被阻塞: {pump_elapsed:.3f}s"
        slow = pipe.subscriber_stats("slow")
        assert _wait_until(lambda: slow.processed + slow.dropped + len(slow._queue) == 24)
        assert slow.dropped > 0, "慢订阅者应发生丢帧"
        assert slow.processed < 24
        assert _wait_until(lambda: len(seen) == 24)  # 快订阅者不受影响，全收
        assert pipe.subscriber_stats("fast").dropped == 0
    finally:
        pipe.stop()


def test_pipeline_diagnostics_fps_adapter_latency_dropped() -> None:
    """诊断：fps>0、适配器名、latest 延迟、各订阅者 dropped 字段齐全。"""
    source = FakeCaptureSource([make_frame(seq, 4, 3) for seq in range(1, 31)])
    pipe = FramePipeline(source)
    pipe.subscribe("s", lambda f: None)
    pipe.start()
    try:
        assert pipe.run(30, interval_s=0.005) == 30
        assert _wait_until(lambda: pipe.subscriber_stats("s").processed == 30)
        diag = pipe.diagnostics()
        assert diag["fps"] > 0
        assert diag["adapter"] == "fake"
        assert diag["latest_seq"] == 30
        assert diag["latest_latency_s"] is not None
        assert diag["subscribers"]["s"]["dropped"] == 0
        assert diag["subscribers"]["s"]["processed"] == 30
        assert diag["last_error"] is None
    finally:
        pipe.stop()


def test_pipeline_subscriber_error_isolated_and_recorded() -> None:
    """订阅者回调抛异常：错误计数、不拖垮管线，后续帧继续消费。"""
    source = FakeCaptureSource([make_frame(seq, 4, 3) for seq in range(1, 6)])
    pipe = FramePipeline(source)
    good: list[int] = []
    calls = [0]

    def flaky(frame: Frame) -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("boom")

    pipe.subscribe("flaky", flaky)
    pipe.subscribe("good", lambda f: good.append(f.meta.seq))
    pipe.start()
    try:
        assert pipe.run(5, interval_s=0.01) == 5
        assert _wait_until(lambda: pipe.subscriber_stats("flaky").processed == 4)
        stats = pipe.subscriber_stats("flaky")
        assert stats.error_count == 1 and "boom" in (stats.last_error or "")
        assert _wait_until(lambda: len(good) == 5)  # 其他订阅者不受影响
        assert pipe.diagnostics()["pumped"] == 5
    finally:
        pipe.stop()


def test_pipeline_rejects_duplicate_and_bad_queue_size() -> None:
    """重复订阅名与非法队列长度报 ValueError。"""
    pipe = FramePipeline(FakeCaptureSource())
    pipe.subscribe("a", lambda f: None)
    with pytest.raises(ValueError, match="重名"):
        pipe.subscribe("a", lambda f: None)
    with pytest.raises(ValueError, match="queue_size"):
        pipe.subscribe("b", lambda f: None, queue_size=0)
    with pytest.raises(ValueError, match="queue_size"):
        FramePipeline(FakeCaptureSource(), queue_size=0)


# ---------------------------------------------------------------------------
# CAP-007：采集链（黑帧 / 尺寸异常 / 异常切换）
# ---------------------------------------------------------------------------

BLACK = (0, 0, 0)
MARKER = (200, 30, 40)  # fallback 帧的标记色


def test_chain_black_frame_switches_to_fallback_and_needs_reconfirm() -> None:
    """连续黑帧 -> 不返回坏帧，切 fallback；needs_reconfirm=True（AC-P0-13）。"""
    primary = FakeCaptureSource([make_frame(1, 4, 3, BLACK)], name="primary")
    fallback = FakeCaptureSource([make_frame(1, 4, 3, MARKER)], name="fallback")
    chain = CaptureChain(primary, fallback)
    chain.start()
    try:
        frame = chain.grab()
        assert frame is not None
        assert tuple(int(c) for c in frame.pixels.flat[:3]) == MARKER  # 是 fallback 帧
        assert chain.switched and chain.needs_reconfirm
        assert chain.active is fallback
        assert primary._stopped, "切换前应先停止主源（停止输入）"
        event = chain.events[-1]
        assert event["reason"] == "black_frame" and event["switched"]
        assert chain.black_frame_count == 1
        # 上层确认后清除标志
        chain.acknowledge_reconfirm()
        assert not chain.needs_reconfirm
    finally:
        chain.stop()


def test_chain_size_anomaly_switches_to_fallback() -> None:
    """尺寸突变（与基线不符）-> 切换并要求重新确认。"""
    primary = FakeCaptureSource(
        [make_frame(1, 64, 48, (50, 50, 50)),
         make_frame(2, 32, 24, (80, 80, 80))],  # 尺寸突变且非黑 -> 命中尺寸异常
        name="primary"
    )
    fallback = FakeCaptureSource([make_frame(1, 32, 24, MARKER)], name="fallback")
    chain = CaptureChain(primary, fallback)
    chain.start()
    try:
        assert chain.grab() is not None  # 正常首帧，建立基线
        frame = chain.grab()  # 尺寸异常帧 -> 切换
        assert frame is not None and tuple(int(c) for c in frame.pixels.flat[:3]) == MARKER
        assert chain.switched and chain.needs_reconfirm
        assert chain.events[-1]["reason"] == "size_anomaly"
        assert chain.size_anomaly_count == 1
    finally:
        chain.stop()


def test_chain_primary_exception_switches_and_records_detail() -> None:
    """主源抓帧异常 -> 记录异常详情 -> 立即从 fallback 出帧。"""
    primary = FakeCaptureSource(
        fail_on_grab=RuntimeError("device lost"), name="primary"
    )
    fallback = FakeCaptureSource(
        [make_frame(1, 4, 3, MARKER, adapter="fallback")], name="fallback"
    )
    chain = CaptureChain(primary, fallback)
    chain.start()
    try:
        frame = chain.grab()
        assert frame is not None and frame.meta.adapter == "fallback"
        assert chain.switched and chain.needs_reconfirm
        assert chain.events[-1]["reason"] == "grab_error"
        assert "device lost" in (chain.events[-1]["detail"] or "")
    finally:
        chain.stop()


def test_chain_healthy_primary_never_touches_fallback() -> None:
    """主源健康：全部帧来自主源，fallback 一帧都不抓、无切换。"""
    primary = FakeCaptureSource(
        [make_frame(seq, 4, 3, (10, 10, 10), adapter="primary") for seq in range(1, 6)],
        name="primary",
    )
    fallback = FakeCaptureSource(name="fallback")
    chain = CaptureChain(primary, fallback)
    chain.start()
    try:
        got = [chain.grab() for _ in range(5)]
        assert all(f is not None and f.meta.adapter == "primary" for f in got)
        assert not chain.switched and not chain.needs_reconfirm
        assert fallback.grabbed_count == 0
        assert chain.events == []
    finally:
        chain.stop()


def test_chain_no_fallback_stops_input_on_failure() -> None:
    """无 fallback：黑帧只记录并保持停止输入（返回 None，不返回坏帧）。"""
    primary = FakeCaptureSource([make_frame(1, 4, 3, BLACK), make_frame(2, 4, 3)], name="p")
    chain = CaptureChain(primary, None)
    chain.start()
    try:
        assert chain.grab() is None
        assert not chain.switched  # 无处可切
        assert chain.black_frame_count == 1
        assert chain.events[-1]["reason"] == "black_frame"
        assert not chain.events[-1]["switched"]
    finally:
        chain.stop()


def test_chain_none_passthrough_and_start_failure_fallback() -> None:
    """暂无新帧透传不误切；主源启动不可用时直接以 fallback 运行。"""
    chain = CaptureChain(FakeCaptureSource(name="p"), FakeCaptureSource(name="f"))
    chain.start()
    try:
        assert chain.grab() is None  # 双方都无帧
        assert not chain.switched
    finally:
        chain.stop()

    broken = FakeCaptureSource(name="broken")
    broken.start = (  # type: ignore[method-assign]
        lambda: (_ for _ in ()).throw(AdapterUnavailableError("no display", adapter="broken"))
    )
    healthy = FakeCaptureSource([make_frame(1, 4, 3, MARKER)], name="healthy")
    chain2 = CaptureChain(broken, healthy)
    chain2.start()
    try:
        assert chain2.active is healthy
        assert chain2.switched and chain2.needs_reconfirm
        assert chain2.events[-1]["reason"] == "start_error"
    finally:
        chain2.stop()


def test_chain_size_baseline_is_per_source() -> None:
    """尺寸基线按源独立：切到分辨率不同的 fallback 后不再误判尺寸异常。"""
    primary = FakeCaptureSource([make_frame(1, 64, 48, BLACK)], name="p")
    fallback = FakeCaptureSource(
        [make_frame(1, 128, 72, MARKER, adapter="fallback"),
         make_frame(2, 128, 72, MARKER, adapter="fallback"),
         make_frame(3, 128, 72, MARKER, adapter="fallback")],
        name="f",
    )
    chain = CaptureChain(primary, fallback)
    chain.start()
    try:
        assert chain.grab() is not None  # 黑帧 -> 切换到 fallback（128x72）
        frame = chain.grab()  # fallback 帧与其自身基线一致 -> 正常
        assert frame is not None and frame.meta.adapter == "fallback"
        assert chain.size_anomaly_count == 0
        assert chain.grab() is not None  # 持续正常出帧
    finally:
        chain.stop()


# ---------------------------------------------------------------------------
# 真实采集冒烟（无显示器/驱动缺失的环境一律 skip，不 fail）
# ---------------------------------------------------------------------------


def test_mss_adapter_real_smoke() -> None:
    """mss 冒烟：真实抓一帧，shape/格式/元信息合法。"""
    if not MSS_AVAILABLE:
        pytest.skip("mss 未安装")
    adapter = MssAdapter()
    try:
        adapter.start()
    except AdapterUnavailableError as exc:
        pytest.skip(f"mss 初始化失败（可能无显示器）: {exc.reason}")
    except Exception as exc:  # 环境类失败一律 skip
        pytest.skip(f"mss 初始化异常: {exc!r}")
    try:
        frame = None
        for _ in range(5):
            try:
                frame = adapter.grab()
            except Exception as exc:
                pytest.skip(f"mss 抓帧异常: {exc!r}")
            if frame is not None:
                break
            time.sleep(0.05)
        if frame is None:
            pytest.skip("mss 连续 5 次未抓到帧（CI 无显示器场景）")
        assert frame.pixels.ndim == 3 and frame.pixels.shape[2] == 3
        assert frame.pixels.dtype == np.uint8
        assert frame.height > 0 and frame.width > 0
        assert frame.meta.adapter == "mss" and frame.meta.seq >= 1
        assert frame.meta.source_width == frame.width
        print(f"[smoke] mss 抓帧成功: {frame.width}x{frame.height}")
        assert adapter.latest() is not None
    finally:
        adapter.stop()
        adapter.stop()  # 幂等


@pytest.mark.skipif(not DXCAM_AVAILABLE, reason="dxcam 未安装")
def test_dxcam_adapter_real_smoke() -> None:
    """DXcam 冒烟：最多重试 ~10 次拿首帧（首帧 None 为已知现象），拿不到则 skip。"""
    adapter = DxcamAdapter()
    try:
        adapter.start()
    except AdapterUnavailableError as exc:
        pytest.skip(f"dxcam 初始化失败（可能无显示器/被占用）: {exc.reason}")
    except Exception as exc:
        pytest.skip(f"dxcam 初始化异常: {exc!r}")
    try:
        frame = None
        attempts = max(1, min(10, adapter.max_grab_retries))
        for attempt in range(1, attempts + 1):
            try:
                frame = adapter.grab()
            except Exception as exc:
                pytest.skip(f"dxcam 抓帧异常（第 {attempt} 次）: {exc!r}")
            if frame is not None:
                print(f"[smoke] dxcam 第 {attempt} 次尝试抓到帧")
                break
            time.sleep(0.05)  # 给桌面复制 API 一点产出新帧的时间
        if frame is None:
            print(f"[smoke] dxcam 重试 {attempts} 次仍无新帧（首帧 None 现象），跳过不 fail")
            pytest.skip(f"dxcam 重试 {attempts} 次未拿到帧")
        assert frame.pixels.ndim == 3 and frame.pixels.shape[2] == 3
        assert frame.pixels.dtype == np.uint8
        assert frame.height > 0 and frame.width > 0
        assert frame.meta.adapter == "dxcam"
        print(f"[smoke] dxcam 抓帧成功: {frame.width}x{frame.height}")
    finally:
        adapter.stop()
        adapter.stop()  # release() 幂等验证
