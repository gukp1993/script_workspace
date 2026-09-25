"""E06 视觉检测与调试单元测试（VIS-001~009/011/012 + VIS-010 数据层）。

数据策略：全部用 arena_lab 确定性渲染器/场景产生**真实帧**，Oracle 真值
（SceneState / 渲染器布局）做期望；合成图仅用于受控边界（掩码/尺度/噪
声连通域）。量化门槛对齐文档 §7.2：比例 MAE ≤ 0.03、P95 绝对误差
≤ 0.05；关键检测器 precision/recall ≥ 0.98。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from arena_lab.render import Renderer, SceneConfig, SceneState
from arena_lab.scenario import run_scenario
from capture_api.fake import make_frame
from capture_api.frames import Frame, FrameMeta
from domain_model.models import Detector as DetectorConfig
from domain_model.models import FieldObservation

from detector_opencv.change_stability import ChangeStabilityDetector
from detector_opencv.color_bar import ColorBarRatioDetector
from detector_opencv.color_region import ColorRegionDetector
from detector_opencv.ocr_roi import (
    EngineUnavailableError,
    MockOcrEngine,
    OcrReading,
    RoiOcrDetector,
    TesseractOcrEngine,
)
from detector_opencv.overlay import OverlayData
from detector_opencv.template_advanced import AdvancedTemplateDetector
from detector_opencv.template_match import TemplateMatchDetector
from vision_core.base import DetectorResult, PerceptionBuilder
from vision_core.golden import GoldenDataset, build_from_scenario, metrics
from vision_core.offline import CaseResult, batch_run, diff_reports
from vision_core.registry import DETECTOR_REGISTRY, create_detector, registered_types
from vision_core.scheduler import DetectorScheduler, SchedulerEntry
from vision_core.stability import StabilityConfig, StableFrameAggregator

# ---------------------------------------------------------------------------
# 公共夹具与助手
# ---------------------------------------------------------------------------

RES = (640, 360)
SEED = 42


def _norm(rect: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
    """渲染器像素矩形 (x0,y0,x1,y1) -> 归一化 ROI (x,y,w,h)。"""
    x0, y0, x1, y1 = rect
    w, h = RES
    return (x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h)


def _pad(rect: tuple[int, int, int, int], pad: int) -> tuple[int, int, int, int]:
    """矩形四周外扩 pad 像素（裁剪到画面内）。"""
    x0, y0, x1, y1 = rect
    w, h = RES
    return (max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad))


def _bbox(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """渲染器 (x0,y0,x1,y1) -> bbox (x,y,w,h)（与 DetectorResult.bbox 同构）。"""
    x0, y0, x1, y1 = rect
    return (x0, y0, x1 - x0, y1 - y0)


def _frame(pixels: np.ndarray, seq: int = 0, ts: float = 0.0) -> Frame:
    """像素数组 -> Frame（arena 适配器语义）。"""
    meta = FrameMeta(
        seq=seq,
        ts_monotonic=ts,
        adapter="arena",
        source_width=pixels.shape[1],
        source_height=pixels.shape[0],
    )
    return Frame(pixels, meta)


def _shift(pixels: np.ndarray, delta: int) -> np.ndarray:
    """整帧加性亮度扰动（±20 用例），饱和裁剪。"""
    return np.clip(pixels.astype(np.int16) + delta, 0, 255).astype(np.uint8)


def _cfg(
    detector_id: str,
    type_str: str,
    roi: tuple[float, float, float, float],
    threshold: float,
    field_name: str,
    *,
    template: str | None = None,
) -> DetectorConfig:
    """构造 domain_model.Detector 配置（走领域校验，检测器接受它）。"""
    return DetectorConfig(
        detector_id=detector_id,
        type=type_str,
        roi=list(roi),
        threshold=threshold,
        stable_frames=1,
        field_name=field_name,
        template=template,
    )


def _res(
    present: bool,
    confidence: float,
    *,
    value: float | str | None = None,
    bbox: tuple[int, int, int, int] | None = None,
    elapsed_ms: float = 1.0,
    detector_id: str = "d",
    error: str | None = None,
) -> DetectorResult:
    """测试用便捷结果构造器。"""
    return DetectorResult(
        detector_id=detector_id,
        present=present,
        confidence=confidence,
        value=value,
        bbox=bbox,
        elapsed_ms=elapsed_ms,
        version="t",
        error=error,
    )


class _FakeDetector:
    """调度器测试用的确定性假检测器（固定耗时序列）。"""

    def __init__(
        self,
        detector_id: str,
        costs_ms: list[float],
        *,
        priority: int = 0,
        safety_critical: bool = False,
    ) -> None:
        self.detector_id = detector_id
        self.field_name = detector_id
        self._costs = list(costs_ms)
        self._calls = 0
        self.priority = priority
        self.safety_critical = safety_critical

    @property
    def calls(self) -> int:
        return self._calls

    def detect(self, frame: Frame, roi=None) -> DetectorResult:  # noqa: ANN001
        cost = self._costs[min(self._calls, len(self._costs) - 1)]
        self._calls += 1
        return DetectorResult(
            detector_id=self.detector_id,
            present=True,
            confidence=1.0,
            value=None,
            bbox=None,
            elapsed_ms=cost,
            version="fake",
        )


@pytest.fixture(scope="module")
def renderer() -> Renderer:
    """确定性渲染器（布局访问器供 ROI/真值 bbox 推导）。"""
    return Renderer(SceneConfig(seed=SEED, resolution=RES))


@pytest.fixture(scope="module")
def scene_run():
    """happy_path 场景运行产物（帧 + 状态真值，模块级复用）。"""
    return run_scenario(
        "happy_path", SEED, resolution=RES, fps=15.0, duration_s=6.0, with_frames=True
    )


@pytest.fixture(scope="module")
def template_pair(scene_run, renderer):
    """(目标模板, 弹窗模板)：从真实帧按渲染器布局裁剪。"""
    target_tpl = None
    popup_tpl = None
    for state, frame in zip(scene_run.states, scene_run.frames):
        if target_tpl is None and state.target_present:
            x0, y0, x1, y1 = renderer.target_rect()
            target_tpl = frame[y0:y1, x0:x1].copy()
        if popup_tpl is None and state.popup_open:
            x0, y0, x1, y1 = renderer.popup_rect()
            popup_tpl = frame[y0:y1, x0:x1].copy()
    assert target_tpl is not None and popup_tpl is not None
    return target_tpl, popup_tpl


@pytest.fixture(scope="module")
def golden_ds() -> GoldenDataset:
    """黄金数据集（30 样本，Oracle 自动标注）。"""
    return build_from_scenario(
        "happy_path", SEED, resolution=RES, fps=15.0, duration_s=4.0, frame_stride=2
    )


def _chain_detectors(renderer, template_pair) -> list:
    """全链路检测器组：血条/资源条/目标/弹窗/掉落/加载。

    颜色条阈值取 0.05：present 表达"条可观测"（比例语义本身由 value
    承载）；若配 0.5 则是"低血量"事件语义，会与"始终可观测"的期望冲突。
    """
    target_tpl, popup_tpl = template_pair
    return [
        ColorBarRatioDetector(
            _cfg(
                "health-bar",
                "color_bar_ratio",
                _norm(renderer.health_bar_inner_rect()),
                0.05,
                "health_ratio",
            )
        ),
        ColorBarRatioDetector(
            _cfg(
                "resource-bar",
                "color_bar_ratio",
                _norm(renderer.resource_bar_inner_rect()),
                0.05,
                "resource_ratio",
            )
        ),
        TemplateMatchDetector(
            _cfg(
                "target-mark",
                "template_match",
                _norm(_pad(renderer.target_rect(), 10)),
                0.8,
                "target_present",
                template="assets/target.png",
            ),
            template=target_tpl,
        ),
        TemplateMatchDetector(
            _cfg(
                "popup-dialog",
                "template_match",
                _norm(_pad(renderer.popup_rect(), 8)),
                0.8,
                "popup_present",
                template="assets/popup.png",
            ),
            template=popup_tpl,
        ),
        ColorRegionDetector(
            _cfg(
                "loot-mark",
                "color_region",
                _norm(renderer.loot_rect()),
                0.5,
                "loot_present",
            ),
            ranges=[((10, 150, 150), (30, 255, 255))],
            color_space="hsv",
        ),
        ColorRegionDetector(
            _cfg(
                "loading-page",
                "color_region",
                (0.0, 0.0, 1.0, 1.0),
                0.9,
                "loading_present",
            ),
            ranges=[((6, 8, 14), (14, 17, 23))],
            color_space="rgb",
        ),
    ]


@pytest.fixture(scope="module")
def chain_report(golden_ds, renderer, template_pair):
    """全链路：数据集 -> 检测器批次 -> 指标（模块级算一次，多测试复用）。"""
    detectors = _chain_detectors(renderer, template_pair)
    results = batch_run(golden_ds, detectors)
    fields = set(results[0].results)
    expected = [
        {k: v for k, v in case.expected.items() if k in fields} for case in golden_ds.cases
    ]
    return golden_ds, detectors, results, expected


# ---------------------------------------------------------------------------
# VIS-001 契约 / 注册表 / PerceptionBuilder
# ---------------------------------------------------------------------------


class TestContractAndRegistry:
    def test_detector_result_contract_fields(self):
        """DetectorResult 契约字段齐全，error 默认 None。"""
        r = DetectorResult("d", True, 0.9, 0.5, (1, 2, 3, 4), 1.5, "1.0.0")
        assert (r.detector_id, r.present, r.confidence, r.value) == ("d", True, 0.9, 0.5)
        assert r.bbox == (1, 2, 3, 4) and r.elapsed_ms == 1.5
        assert r.version == "1.0.0" and r.error is None

    def test_registry_has_all_types(self):
        """注册表覆盖 5 种检测器类型。"""
        assert set(registered_types()) == {
            "template_match",
            "color_bar_ratio",
            "color_region",
            "change_stability",
            "ocr_roi",
        }
        for name, cls in DETECTOR_REGISTRY.items():
            assert isinstance(name, str) and isinstance(cls, type)

    def test_create_detector_by_config(self):
        """create_detector 按 type 实例化并接受 domain 配置。"""
        engine = MockOcrEngine()
        cases = [
            ("template_match", {"template": np.zeros((4, 4), dtype=np.uint8)}),
            ("color_bar_ratio", {}),
            ("color_region", {"ranges": [((0, 0, 0), (9, 9, 9))]}),
            ("change_stability", {}),
            ("ocr_roi", {"engine": engine}),
        ]
        for type_str, kwargs in cases:
            cfg = _cfg(
                "d-x",
                type_str,
                (0.0, 0.0, 0.5, 0.5),
                0.5,
                "field_x",
                template="assets/x.png" if type_str == "template_match" else None,
            )
            det = create_detector(cfg, **kwargs)
            assert det.detector_id == "d-x"
            assert callable(det.detect)

    def test_perception_builder_maps_fields_and_errors(self):
        """聚合规则：字段名映射、错误结果降为未观测、未知检测器忽略。"""
        builder = PerceptionBuilder({"good": "target_present", "bad": "health_ratio"})
        snapshot = builder.build(
            [
                _res(True, 0.95, value=1.0, detector_id="good"),
                _res(False, 0.0, detector_id="bad", error="bar_not_found"),
                _res(True, 0.9, detector_id="unknown"),
            ],
            frame_seq=7,
            ts_monotonic=1.25,
        )
        assert snapshot.frame_seq == 7 and snapshot.ts_monotonic == 1.25
        target = snapshot.values["target_present"]
        assert isinstance(target, FieldObservation)
        assert (target.present, target.confidence, target.value) == (True, 0.95, 1.0)
        health = snapshot.values["health_ratio"]
        assert health.present is False and health.value is None
        assert "unknown" not in snapshot.values


# ---------------------------------------------------------------------------
# VIS-002 基础模板匹配
# ---------------------------------------------------------------------------


class TestTemplateMatch:
    def _detector(self, renderer, template_pair, *, roi, top_k: int = 3) -> TemplateMatchDetector:
        target_tpl, _ = template_pair
        return TemplateMatchDetector(
            _cfg(
                "target-mark",
                "template_match",
                roi,
                0.8,
                "target_present",
                template="assets/target.png",
            ),
            template=target_tpl,
            top_k=top_k,
        )

    def test_positive_hit_exact_bbox_and_confidence(
        self, scene_run, renderer, template_pair
    ):
        """正样本：命中、bbox 与真值框一致、置信度接近 1。"""
        det = self._detector(renderer, template_pair, roi=_norm(_pad(renderer.target_rect(), 10)))
        for state, frame in zip(scene_run.states, scene_run.frames):
            if not state.target_present:
                continue
            result = det.detect(_frame(frame))
            assert result.present and result.error is None
            assert result.bbox == _bbox(renderer.target_rect())
            assert result.confidence >= 0.99
            return
        pytest.fail("场景中没有目标帧")

    def test_negative_frames_no_false_positive(self, scene_run, template_pair):
        """负样本：无目标帧（含弹窗/掉落）全帧扫描不误报。"""
        target_tpl, _ = template_pair
        det = TemplateMatchDetector(
            _cfg(
                "target-mark",
                "template_match",
                (0.0, 0.0, 1.0, 1.0),
                0.8,
                "target_present",
                template="assets/target.png",
            ),
            template=target_tpl,
        )
        checked = 0
        for state, frame in zip(scene_run.states, scene_run.frames):
            if state.target_present or state.loading_active:
                continue
            result = det.detect(_frame(frame))
            assert not result.present and result.bbox is None
            assert result.confidence < 0.8
            checked += 1
        assert checked >= 30

    def test_roi_override_restricts_search(self, scene_run, renderer, template_pair):
        """ROI 覆盖：把搜索区挪到左上角后，即使目标在场也判未命中。"""
        det = self._detector(renderer, template_pair, roi=(0.0, 0.0, 0.2, 0.2))
        for state, frame in zip(scene_run.states, scene_run.frames):
            if state.target_present:
                result = det.detect(_frame(frame))
                assert not result.present
                return
        pytest.fail("场景中没有目标帧")

    def test_topk_candidates_sorted_and_best_first(self, scene_run, renderer, template_pair):
        """top-k 候选：分数降序、首个即最佳命中。"""
        det = self._detector(renderer, template_pair, roi=(0.0, 0.0, 1.0, 1.0), top_k=3)
        for state, frame in zip(scene_run.states, scene_run.frames):
            if state.target_present:
                result = det.detect(_frame(frame))
                candidates = det.last_candidates
                assert 1 <= len(candidates) <= 3
                scores = [c.score for c in candidates]
                assert scores == sorted(scores, reverse=True)
                assert candidates[0].bbox == result.bbox
                return
        pytest.fail("场景中没有目标帧")

    def test_missing_template_rejected_at_construction(self):
        """模板缺失在构造期报错（运行期不产生半可用检测器）。"""
        with pytest.raises(ValueError):
            TemplateMatchDetector(
                _cfg("x", "template_match", (0.0, 0.0, 0.5, 0.5), 0.8, "f", template="a.png")
            )

    def test_template_larger_than_roi_is_error(self, template_pair):
        """模板大于 ROI：结构化错误而非异常。"""
        target_tpl, _ = template_pair
        det = TemplateMatchDetector(
            _cfg("x", "template_match", (0.0, 0.0, 0.01, 0.01), 0.8, "f", template="a.png"),
            template=target_tpl,
        )
        result = det.detect(_frame(np.zeros((360, 640, 3), dtype=np.uint8)))
        assert result.error == "template_larger_than_roi"
        assert not result.present and result.value is None

    def test_brightness_invariance(self, scene_run, renderer, template_pair):
        """整帧 ±20 亮度：关键检测器仍命中且 bbox 不变（§7.2）。"""
        det = self._detector(renderer, template_pair, roi=_norm(_pad(renderer.target_rect(), 10)))
        for state, frame in zip(scene_run.states, scene_run.frames):
            if state.target_present:
                for delta in (20, -20):
                    result = det.detect(_frame(_shift(frame, delta)))
                    assert result.present, f"亮度 {delta:+d} 后未命中"
                    assert result.bbox == _bbox(renderer.target_rect())
                    assert result.confidence >= 0.9
                return
        pytest.fail("场景中没有目标帧")


# ---------------------------------------------------------------------------
# VIS-003 掩码 / 尺度 / 多模板
# ---------------------------------------------------------------------------


def _pattern(size: int, fill: tuple[int, int, int], core: tuple[int, int, int]) -> np.ndarray:
    """带暗色核心的方块图案（避免零方差模板）。"""
    img = np.empty((size, size, 3), dtype=np.uint8)
    img[:, :] = fill
    q = max(2, size // 3)
    o = size // 2 - q // 2
    img[o : o + q, o : o + q] = core
    return img


class TestAdvancedTemplate:
    def _scene_with_block(
        self, block: np.ndarray, at: tuple[int, int], size: tuple[int, int] = (80, 60)
    ) -> np.ndarray:
        scene = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        x, y = at
        scene[y : y + block.shape[0], x : x + block.shape[1]] = block
        return scene

    def test_mask_ignores_template_background(self):
        """掩码模板：掩码外（白底）不参与，黑色场景中命中红色前景块。"""
        template = np.full((40, 40, 3), 255, dtype=np.uint8)
        template[10:30, 10:30] = (200, 50, 50)
        mask = np.zeros((40, 40), dtype=np.float32)
        mask[10:30, 10:30] = 1.0
        block = np.full((20, 20, 3), (200, 50, 50), dtype=np.uint8)
        scene = self._scene_with_block(block, (30, 10))
        cfg = _cfg("masked", "template_match", (0.0, 0.0, 1.0, 1.0), 0.95, "f", template="a.png")
        det = AdvancedTemplateDetector(cfg, template, masks=[mask], scales=(1.0,))
        result = det.detect(_frame(scene))
        assert result.present and result.confidence >= 0.98
        assert result.bbox == (20, 0, 40, 40)

    def test_scale_set_hits_within_and_fails_outside(self):
        """有限尺度集：1.1x 模板在 (0.9,1.0,1.1) 内命中，仅 (1.0,) 时失败。"""
        pattern = _pattern(30, (200, 50, 50), (40, 40, 40))
        scaled = np.zeros((40, 40, 3), dtype=np.uint8)
        resized = cv2.resize(pattern, (33, 33), interpolation=cv2.INTER_AREA)
        scaled[3:36, 3:36] = resized
        cfg = _cfg("scaled", "template_match", (0.0, 0.0, 1.0, 1.0), 0.9, "f", template="a.png")
        hit = AdvancedTemplateDetector(cfg, pattern, scales=(0.9, 1.0, 1.1))
        result = hit.detect(_frame(scaled))
        assert result.present and result.confidence >= 0.99
        assert result.bbox == (3, 3, 33, 33)
        miss = AdvancedTemplateDetector(cfg, pattern, scales=(1.0,))
        result2 = miss.detect(_frame(scaled))
        assert not result2.present

    def test_multi_template_priority(self, scene_run, template_pair):
        """多模板按序优先：高优先级未中才轮到低优先级（value=序号）。"""
        target_tpl, popup_tpl = template_pair
        cfg = _cfg("multi", "template_match", (0.0, 0.0, 1.0, 1.0), 0.8, "f", template="a.png")
        det = AdvancedTemplateDetector(cfg, [target_tpl, popup_tpl], scales=(1.0,))
        target_frame = popup_frame = None
        for state, frame in zip(scene_run.states, scene_run.frames):
            if state.target_present and target_frame is None:
                target_frame = frame
            if state.popup_open and popup_frame is None:
                popup_frame = frame
        assert target_frame is not None and popup_frame is not None
        assert det.detect(_frame(target_frame)).value == 0.0  # 高优先级命中
        assert det.detect(_frame(popup_frame)).value == 1.0  # 高优先级未中，轮到弹窗


# ---------------------------------------------------------------------------
# VIS-004 颜色条比例
# ---------------------------------------------------------------------------


class TestColorBar:
    def _detector(self, renderer, **kwargs) -> ColorBarRatioDetector:
        return ColorBarRatioDetector(
            _cfg(
                "health-bar",
                "color_bar_ratio",
                _norm(renderer.health_bar_inner_rect()),
                0.5,
                "health_ratio",
            ),
            **kwargs,
        )

    def test_health_ratio_mae_and_p95_gate(self, scene_run, renderer):
        """§7.2 门槛：happy_path 全帧 health_ratio MAE ≤ 0.03、P95 ≤ 0.05。"""
        det = self._detector(renderer)
        errors = [
            abs(det.detect(_frame(frame, seq=i)).value - state.health_ratio)
            for i, (state, frame) in enumerate(zip(scene_run.states, scene_run.frames))
            if not state.loading_active
        ]
        assert len(errors) >= 50
        mae = float(np.mean(errors))
        p95 = float(np.percentile(errors, 95))
        assert mae <= 0.03, f"MAE {mae:.4f} 超门槛"
        assert p95 <= 0.05, f"P95 {p95:.4f} 超门槛"

    def test_health_ratio_robust_to_brightness(self, scene_run, renderer):
        """整帧 ±20 亮度后 MAE/P95 仍达标（相对量分类的基线鲁棒性）。"""
        det = self._detector(renderer)
        for delta in (20, -20):
            errors = [
                abs(det.detect(_frame(_shift(frame, delta))).value - state.health_ratio)
                for state, frame in zip(scene_run.states, scene_run.frames)
                if not state.loading_active
            ]
            assert float(np.mean(errors)) <= 0.03
            assert float(np.percentile(errors, 95)) <= 0.05

    def test_resource_ratio_measured(self, scene_run, renderer):
        """资源条（蓝）：施法后 0.7/0.85 档位可量测，误差 ≤ 0.01。"""
        det = ColorBarRatioDetector(
            _cfg(
                "resource-bar",
                "color_bar_ratio",
                _norm(renderer.resource_bar_inner_rect()),
                0.5,
                "resource_ratio",
            )
        )
        checked = 0
        for state, frame in zip(scene_run.states, scene_run.frames):
            if state.loading_active or state.resource_ratio == 1.0:
                continue
            value = det.detect(_frame(frame)).value
            assert abs(value - state.resource_ratio) <= 0.01
            checked += 1
        assert checked >= 10

    def test_zero_ratio_and_threshold_semantics(self, renderer):
        """空条：value=0.0（非错误），低于阈值时 present=False。"""
        state = SceneState(health_ratio=0.0, resource_ratio=0.0, frame_index=3)
        frame = Renderer(SceneConfig(seed=1, resolution=RES)).render(state)
        det = self._detector(renderer)
        result = det.detect(_frame(frame))
        assert result.value == 0.0 and result.error is None
        assert not result.present  # threshold=0.5

    def test_bar_not_found_is_structured_error(self):
        """ROI 无条槽（亮色均匀区，无填充无槽底）：value=None + error='bar_not_found'。"""
        det = ColorBarRatioDetector(
            _cfg("x", "color_bar_ratio", (0.4, 0.4, 0.2, 0.2), 0.5, "f")
        )
        frame = np.full((360, 640, 3), (200, 200, 200), dtype=np.uint8)
        result = det.detect(_frame(frame))
        assert result.error == "bar_not_found"
        assert result.value is None and not result.present

    def test_invalid_roi_is_structured_error(self, renderer):
        """非法 ROI（越界）：error='invalid_roi'，不抛异常。"""
        det = self._detector(renderer)
        result = det.detect(
            _frame(np.zeros((360, 640, 3), dtype=np.uint8)), roi=(0.9, 0.9, 0.5, 0.5)
        )
        assert result.error == "invalid_roi" and not result.present


# ---------------------------------------------------------------------------
# VIS-005 颜色区域
# ---------------------------------------------------------------------------


class TestColorRegion:
    def _target_region_detector(self, renderer) -> ColorRegionDetector:
        return ColorRegionDetector(
            _cfg(
                "target-region",
                "color_region",
                _norm(renderer.target_rect()),
                0.3,
                "target_present",
            ),
            ranges=[((15, 100, 150), (35, 255, 255))],
            color_space="hsv",
        )

    def test_target_region_area_ratio_and_bbox(self, scene_run, renderer):
        """目标标记：HSV 黄色范围命中，value≈填充占比（去边框后 >0.6）。"""
        det = self._target_region_detector(renderer)
        for state, frame in zip(scene_run.states, scene_run.frames):
            if not state.target_present:
                continue
            result = det.detect(_frame(frame))
            assert result.present and result.value >= 0.6
            assert result.bbox is not None
            assert result.bbox[0] >= renderer.target_rect()[0]
            return
        pytest.fail("场景中没有目标帧")

    def test_region_absent_below_threshold(self, scene_run, renderer):
        """无目标时面积占比低于阈值 → 不命中。"""
        det = self._target_region_detector(renderer)
        misses = 0
        for state, frame in zip(scene_run.states, scene_run.frames):
            if state.target_present or state.loading_active:
                continue
            result = det.detect(_frame(frame))
            assert not result.present and result.value < 0.3
            misses += 1
        assert misses >= 30

    def test_min_area_connected_component_filter(self):
        """连通域过滤：小于 min_area_px 的噪点不计入面积。"""
        scene = np.zeros((60, 80, 3), dtype=np.uint8)
        scene[10:13, 10:13] = (200, 50, 50)  # 3x3 噪点
        scene[30:50, 40:60] = (200, 50, 50)  # 20x20 主体
        cfg = _cfg("r", "color_region", (0.0, 0.0, 1.0, 1.0), 0.05, "f")
        ranges = [((0, 100, 100), (179, 255, 255))]
        det_small = ColorRegionDetector(cfg, ranges, color_space="hsv", min_area_px=1)
        result = det_small.detect(_frame(scene))
        # 噪点 + 主体：409/4800
        assert abs(result.value - 409 / 4800) < 0.01 and result.bbox is not None
        det_filtered = ColorRegionDetector(cfg, ranges, color_space="hsv", min_area_px=50)
        result2 = det_filtered.detect(_frame(scene))
        # 噪点被剔除：只剩主体（20x20）/4800
        assert abs(result2.value - 400 / 4800) < 0.01
        det_all = ColorRegionDetector(cfg, ranges, color_space="hsv", min_area_px=1000)
        assert not det_all.detect(_frame(scene)).present

    def test_rgb_ranges_mode(self, scene_run, renderer):
        """RGB 范围模式：弹窗浅灰底色可检测。"""
        det = ColorRegionDetector(
            _cfg(
                "popup-region",
                "color_region",
                _norm(renderer.popup_rect()),
                0.5,
                "popup_present",
            ),
            ranges=[((228, 228, 218), (242, 242, 232))],
            color_space="rgb",
        )
        hits = 0
        for state, frame in zip(scene_run.states, scene_run.frames):
            if state.popup_open:
                assert det.detect(_frame(frame)).present
                hits += 1
        assert hits >= 5

    def test_loading_screen_full_frame_ratio(self, scene_run):
        """加载画面：整帧深色（RGB 紧范围）→ 面积比例≈1.0。"""
        det = ColorRegionDetector(
            _cfg("loading", "color_region", (0.0, 0.0, 1.0, 1.0), 0.9, "loading_present"),
            ranges=[((6, 8, 14), (14, 17, 23))],
            color_space="rgb",
        )
        for state, frame in zip(scene_run.states, scene_run.frames):
            if state.loading_active:
                result = det.detect(_frame(frame))
                # 文字/进度条等元素占少量像素，但深色底应覆盖 ≥95%
                assert result.present and result.value >= 0.95
                return
        pytest.fail("场景中没有加载帧")


# ---------------------------------------------------------------------------
# VIS-006 变化 / 稳定 / 卡死
# ---------------------------------------------------------------------------


class TestChangeStability:
    def _renderer(self) -> Renderer:
        return Renderer(SceneConfig(seed=9, resolution=(320, 240)))

    def test_static_after_m_frames(self):
        """真静止（逐字节相同帧）：连续 M 帧后判稳定，value=0。"""
        det = ChangeStabilityDetector(
            _cfg("stab", "change_stability", (0.0, 0.0, 1.0, 1.0), 0.5, "screen_static"),
            emit="static",
            static_frames=3,
        )
        pixels = self._renderer().render(SceneState(health_ratio=0.6, frame_index=11))
        # 帧序：参考帧 → 低变化 1/3 → 2/3 → 3/3（进入稳定）
        states = [det.detect(_frame(pixels, i)).present for i in range(4)]
        assert states == [False, False, False, True]
        assert det.detect(_frame(pixels, 4)).value == 0.0

    def test_changed_frames_detected(self):
        """结构性变化（血条跳变）：changed 语义在变化帧命中。"""
        det = ChangeStabilityDetector(
            _cfg("chg", "change_stability", (0.0, 0.0, 1.0, 1.0), 0.5, "screen_changed"),
            emit="changed",
        )
        r = self._renderer()
        result = det.detect(_frame(r.render(SceneState(health_ratio=1.0, frame_index=1))))
        assert not result.present  # 首帧为参考帧
        result2 = det.detect(_frame(r.render(SceneState(health_ratio=0.4, frame_index=1))))
        assert result2.present and result2.value > 0.001

    def test_loading_animation_is_not_frozen(self):
        """加载动画：低活动但持续非零 → 不判卡死、不判稳定。"""
        r = self._renderer()
        frozen = ChangeStabilityDetector(
            _cfg("frz", "change_stability", (0.0, 0.0, 1.0, 1.0), 0.5, "frozen"),
            emit="frozen",
            frozen_timeout_frames=5,
        )
        changed = ChangeStabilityDetector(
            _cfg("chg", "change_stability", (0.0, 0.0, 1.0, 1.0), 0.5, "changed"),
            emit="changed",
        )
        static_det = ChangeStabilityDetector(
            _cfg("st", "change_stability", (0.0, 0.0, 1.0, 1.0), 0.5, "static"),
            emit="static",
            static_frames=3,
        )
        changed_results: list[bool] = []
        for k in range(8):
            frame = _frame(
                r.render(SceneState(loading_active=True, loading_progress=k / 8, frame_index=4))
            )
            assert not frozen.detect(frame).present, f"加载帧 {k} 被误判卡死"
            # changed 首帧是参考帧（必为 False），从第 2 帧起应持续命中
            changed_results.append(changed.detect(frame).present)
            static_det.detect(frame)
        assert not changed_results[0]
        assert all(changed_results[1:])
        assert not static_det.is_static

    def test_frozen_after_timeout(self):
        """卡死：零活动持续超过超时帧数才判 frozen。"""
        r = self._renderer()
        det = ChangeStabilityDetector(
            _cfg("frz", "change_stability", (0.0, 0.0, 1.0, 1.0), 0.5, "frozen"),
            emit="frozen",
            frozen_timeout_frames=4,
        )
        pixels = r.render(SceneState(health_ratio=0.7, frame_index=2))
        states = [det.detect(_frame(pixels, i)).present for i in range(6)]
        # 帧序：参考帧 → 零活动 1..5（第 4 帧零活动起达标）
        assert states == [False, False, False, False, True, True]

    def test_reset_clears_state(self):
        """reset()：稳定计数清零，需重新累计。"""
        r = self._renderer()
        det = ChangeStabilityDetector(
            _cfg("st", "change_stability", (0.0, 0.0, 1.0, 1.0), 0.5, "static"),
            emit="static",
            static_frames=2,
        )
        pixels = r.render(SceneState(health_ratio=0.5, frame_index=6))
        # 帧序：参考帧 → 低变化 1/2 → 2/2（进入稳定）
        det.detect(_frame(pixels, 0))
        det.detect(_frame(pixels, 1))
        det.detect(_frame(pixels, 2))
        assert det.is_static
        det.reset()
        assert not det.is_static and det.static_streak == 0
        assert not det.detect(_frame(pixels, 2)).present

    def test_frame_counter_flicker_counts_as_change(self):
        """帧号数字变化计入差分（可观测语义）：static 不进入。"""
        r = self._renderer()
        det = ChangeStabilityDetector(
            _cfg("st", "change_stability", (0.0, 0.0, 1.0, 1.0), 0.5, "static"),
            emit="static",
            static_frames=2,
        )
        for i in range(4):
            result = det.detect(_frame(r.render(SceneState(health_ratio=0.5, frame_index=i))))
            assert not result.present


# ---------------------------------------------------------------------------
# VIS-007 事件触发式 ROI OCR
# ---------------------------------------------------------------------------


class TestOcrRoi:
    def _ocr_detector(
        self,
        scene_run,
        renderer,
        template_pair,
        engine,
        **kwargs,
    ) -> tuple[RoiOcrDetector, Frame, Frame]:
        """构造带门控（目标模板）的 OCR 检测器 + 触发/未触发帧。"""
        target_tpl, _ = template_pair
        gate = TemplateMatchDetector(
            _cfg(
                "target-mark",
                "template_match",
                _norm(_pad(renderer.target_rect(), 10)),
                0.8,
                "target_present",
                template="assets/target.png",
            ),
            template=target_tpl,
        )
        header_h = renderer.header_rect()[3]
        det = RoiOcrDetector(
            _cfg(
                "score-ocr",
                "ocr_roi",
                (0.0, 0.0, 1.0, header_h / RES[1]),
                0.5,
                "score_text",
            ),
            engine,
            trigger_detector=gate,
            **kwargs,
        )
        on = second_on = off = None
        for state, frame in zip(scene_run.states, scene_run.frames):
            if state.target_present:
                if on is None:
                    on = _frame(frame, seq=1)
                elif second_on is None:
                    second_on = _frame(frame, seq=3)  # 不同帧号（头部内容不同）
            if not state.target_present and not state.loading_active and off is None:
                off = _frame(frame, seq=2)
        assert on is not None and second_on is not None and off is not None
        if isinstance(engine, MockOcrEngine):
            engine.register(on.pixels[0:header_h, 0 : RES[0]], "ARENA LAB")
        return det, on, second_on, off

    def test_no_engine_call_when_trigger_absent(self, scene_run, renderer, template_pair):
        """触发条件不满足：引擎调用次数为 0，结果非命中且无错误。"""
        det, _on, _second, off = self._ocr_detector(
            scene_run, renderer, template_pair, MockOcrEngine()
        )
        result = det.detect(off)
        assert det.engine_calls == 0
        assert not result.present and result.error is None

    def test_engine_called_and_text_returned_when_triggered(
        self, scene_run, renderer, template_pair
    ):
        """触发满足 + 已注册内容：调用引擎并返回注册文本。"""
        det, on, _second, _off = self._ocr_detector(
            scene_run, renderer, template_pair, MockOcrEngine()
        )
        result = det.detect(on)
        assert det.engine_calls == 1
        assert result.present and result.value == "ARENA LAB"
        assert result.confidence == 1.0 and result.error is None

    def test_min_interval_skips_extra_calls(self, scene_run, renderer, template_pair):
        """帧间隔未到：不调用引擎（预算保护）。"""
        det, on, _second, _off = self._ocr_detector(
            scene_run, renderer, template_pair, MockOcrEngine(), min_interval_frames=10
        )
        first = det.detect(on)
        assert first.present and det.engine_calls == 1
        second = det.detect(on)
        assert not second.present and det.engine_calls == 1

    def test_empty_result_is_not_error(self, scene_run, renderer, template_pair):
        """空结果：present=False、error=None（可观测但非故障）。"""
        det, _on, second_on, _off = self._ocr_detector(
            scene_run, renderer, template_pair, MockOcrEngine()
        )
        # 门控命中但内容未注册（另一帧的头部）→ 引擎返回空文本。
        result = det.detect(second_on)
        assert det.engine_calls == 1
        assert not result.present and result.error is None and result.value is None

    def test_timeout_protection(self, scene_run, renderer, template_pair):
        """超时保护：引擎过慢 → error='ocr_timeout'，有界耗时。"""
        slow = MockOcrEngine(simulated_latency_s=0.15)
        det, on, _second, _off = self._ocr_detector(
            scene_run, renderer, template_pair, slow, timeout_s=0.05
        )
        result = det.detect(on)
        assert result.error == "ocr_timeout" and not result.present

    def test_engine_unavailable_semantics(self, scene_run, renderer, template_pair):
        """引擎不可用：首次转结构化错误，其后熔断不再调用引擎。"""

        class _BrokenEngine:
            def __init__(self) -> None:
                self.calls = 0

            def recognize(self, image: np.ndarray) -> OcrReading:
                self.calls += 1
                raise EngineUnavailableError("依赖缺失", engine="stub")

        broken = _BrokenEngine()
        det, on, _second, _off = self._ocr_detector(
            scene_run, renderer, template_pair, broken
        )
        first = det.detect(on)
        assert first.error == "engine_unavailable" and not first.present
        second = det.detect(on)
        assert second.error == "engine_unavailable"
        assert broken.calls == 1  # 首次失败后熔断，不再调用
        assert det.engine_calls == 1

    def test_tesseract_placeholder_without_dependency(self):
        """Tesseract 占位：缺 pytesseract 时构造抛 AdapterUnavailable 语义异常。"""
        try:
            import pytesseract  # noqa: F401

            pytest.skip("环境已安装 pytesseract，跳过缺库路径")
        except ImportError:
            pass
        with pytest.raises(EngineUnavailableError):
            TesseractOcrEngine()


# ---------------------------------------------------------------------------
# VIS-008 调度 / 频率 / 预算
# ---------------------------------------------------------------------------


class TestScheduler:
    def test_frequency_gating_every_n(self):
        """每 N 帧执行一次：执行率=1/N 的精确帧序（0/3/6）。"""
        det = _FakeDetector("d", [1.0])
        scheduler = DetectorScheduler(
            [SchedulerEntry(det, every_n_frames=3, priority=1)], budget_ms=100.0
        )
        frame = make_frame(width=8, height=8)
        for _ in range(7):
            scheduler.process_frame(frame)
        stat = scheduler.stats()["d"]
        assert det.calls == 3  # 帧 0/3/6
        assert abs(stat.execution_rate - 3 / 7) < 1e-9

    def test_budget_skips_low_priority_keeps_safety(self):
        """超预算：低优先级被跳过，安全检测器每帧都执行（永不降级）。"""
        safety = _FakeDetector("safe", [40.0], priority=10, safety_critical=True)
        heavy = _FakeDetector("heavy", [40.0], priority=0)
        scheduler = DetectorScheduler(
            [
                SchedulerEntry(safety, every_n_frames=1, priority=10, safety_critical=True),
                SchedulerEntry(heavy, every_n_frames=1, priority=0),
            ],
            budget_ms=50.0,
        )
        frame = make_frame(width=8, height=8)
        for _ in range(8):
            results = scheduler.process_frame(frame)
            assert "safe" in [r.detector_id for r in results]
        stats = scheduler.stats()
        assert stats["safe"].execution_rate == 1.0
        assert stats["heavy"].executed <= 2
        assert stats["heavy"].skipped_budget >= 1
        assert stats["heavy"].execution_rate < 1.0

    def test_low_priority_demoted_on_budget_skips(self):
        """降频：被预算跳过的低优先级检测器有效间隔增大（高优先级只跳过）。"""
        safety = _FakeDetector("safe", [40.0], priority=10, safety_critical=True)
        heavy = _FakeDetector("heavy", [40.0], priority=0)
        heavy_entry = SchedulerEntry(heavy, every_n_frames=1, priority=0, max_every_n=4)
        scheduler = DetectorScheduler(
            [
                SchedulerEntry(safety, every_n_frames=1, priority=10, safety_critical=True),
                heavy_entry,
            ],
            budget_ms=50.0,
        )
        frame = make_frame(width=8, height=8)
        for _ in range(4):
            scheduler.process_frame(frame)
        assert heavy_entry.effective_every_n > heavy_entry.every_n_frames
        assert heavy_entry.demotions > 0
        assert scheduler.entries()[0].effective_every_n == 1  # 安全检测器不受影响

    def test_recovery_when_budget_respected(self):
        """预算恢复：压力消失后（无跳过的帧）有效间隔逐步降回基础值。"""
        # 前 2 帧 40ms 高压（必然跳过低优先级并降频），之后降到 5ms 压力消失。
        safety = _FakeDetector("safe", [40.0, 40.0, 5.0, 5.0, 5.0], priority=5)
        low = _FakeDetector("low", [40.0, 5.0, 5.0, 5.0, 5.0], priority=0)
        low_entry = SchedulerEntry(low, every_n_frames=1, priority=0, max_every_n=4)
        scheduler = DetectorScheduler(
            [SchedulerEntry(safety, every_n_frames=1, priority=5), low_entry],
            budget_ms=50.0,
        )
        frame = make_frame(width=8, height=8)
        for _ in range(4):
            scheduler.process_frame(frame)
        assert low_entry.demotions >= 1  # 确实经历降频（帧 1：40+40 > 50）
        assert low_entry.effective_every_n == 1  # 帧 2/3 预算充足 → 恢复到基础频率
        assert low_entry.skipped_budget == 1


# ---------------------------------------------------------------------------
# VIS-009 稳定帧 / 迟滞 / 置信度聚合
# ---------------------------------------------------------------------------


class TestStability:
    def test_requires_consecutive_frames(self):
        """单帧抖动被抑制：T/F 交替永不进入；连续 N 帧才进入。"""
        agg = StableFrameAggregator(
            "d",
            StabilityConfig(enter_frames=3, exit_frames=2, enter_threshold=0.7, exit_threshold=0.3),
        )
        for k in range(8):
            out = agg.update(_res(k % 2 == 0, 0.9))
            assert not out.present, f"第 {k} 帧不应进入稳定态"
        for _ in range(3):
            out = agg.update(_res(True, 0.9))
        assert out.present

    def test_hysteresis_gray_zone_holds_state(self):
        """迟滞：灰区（0.3~0.7）不推进进入；退出需连续低置信/未命中。"""
        agg = StableFrameAggregator(
            "d",
            StabilityConfig(enter_frames=2, exit_frames=2, enter_threshold=0.7, exit_threshold=0.3),
        )
        agg.update(_res(True, 0.9))
        agg.update(_res(True, 0.9))
        assert agg.present
        assert agg.update(_res(True, 0.5)).present  # 灰区：既不进入计数也不退出计数
        assert agg.update(_res(True, 0.2)).present  # 低于退出阈值：退出计数 1/2
        assert not agg.update(_res(False, 0.0)).present  # 退出计数 2/2 → 退出
        # 退出后灰区不应进入
        assert not agg.update(_res(True, 0.5)).present
        assert not agg.update(_res(False, 0.0)).present  # 未命中直接推进退出计数（无副作用）

    def test_confidence_window_mean(self):
        """输出置信度 = 窗口滑动均值。"""
        agg = StableFrameAggregator("d", StabilityConfig(enter_frames=1, confidence_window=4))
        out = None
        for conf in (0.8, 0.6, 0.4, 0.2):
            out = agg.update(_res(True, conf))
        assert out is not None
        assert abs(out.confidence - (0.8 + 0.6 + 0.4 + 0.2) / 4) < 1e-9

    def test_none_update_keeps_state_deterministic(self):
        """update(None)（被调度降频）不改变状态；同输入序列两次聚合完全一致。"""

        def run() -> list[bool]:
            agg = StableFrameAggregator(
                "d",
                StabilityConfig(enter_frames=2, exit_frames=1, enter_threshold=0.7, exit_threshold=0.3),
            )
            states: list[bool] = []
            for item in [_res(True, 0.9), None, _res(True, 0.2), _res(True, 0.9), _res(True, 0.9)]:
                out = agg.update(item)
                states.append(out.present)
            return states

        assert run() == run() == [False, False, False, False, True]


# ---------------------------------------------------------------------------
# VIS-010 调试叠加数据层
# ---------------------------------------------------------------------------


class TestOverlay:
    def test_overlay_data_from_results(self):
        """OverlayData 承载框/置信度/耗时/版本，可 JSON 化。"""
        results = [
            _res(True, 0.9, value=0.5, bbox=(1, 2, 3, 4), elapsed_ms=2.5, detector_id="a"),
            _res(False, 0.1, detector_id="b", error="bar_not_found"),
        ]
        data = OverlayData.from_results(results, frame_seq=12, ts_monotonic=3.5)
        assert data.frame_seq == 12
        assert data.items[0].bbox == (1, 2, 3, 4)
        assert data.items[0].version == "t" and data.items[0].elapsed_ms == 2.5
        assert data.items[1].error == "bar_not_found" and data.items[1].bbox is None
        payload = json.dumps(data.to_json_dict())
        assert "frame_seq" in payload and "confidence" in payload


# ---------------------------------------------------------------------------
# VIS-011 黄金数据集与指标
# ---------------------------------------------------------------------------


class TestGoldenDataset:
    def test_build_is_deterministic(self):
        """同参数两次构建：哈希与期望完全一致（确定性门禁）。"""
        ds1 = build_from_scenario("happy_path", SEED, resolution=RES, fps=15.0, duration_s=2.0)
        ds2 = build_from_scenario("happy_path", SEED, resolution=RES, fps=15.0, duration_s=2.0)
        assert ds1.hash == ds2.hash
        assert [c.expected for c in ds1.cases] == [c.expected for c in ds2.cases]

    def test_expected_from_oracle_states(self, golden_ds, renderer):
        """期望与 Oracle 真值一致：加载帧/血条/目标框逐项对得上。"""
        first = golden_ds.cases[0].expected
        assert "loading_present" in first and "health_ratio" not in first
        for case in golden_ds.cases:
            health = case.expected.get("health_ratio")
            target = case.expected.get("target_present")
            if health is not None:
                assert health["kind"] == "ratio" and 0.0 <= health["value"] <= 1.0
            if target is not None and target["present"]:
                assert target["bbox"] == _bbox(renderer.target_rect())

    def test_save_load_roundtrip(self, golden_ds, tmp_path):
        """save/load 往返：帧、期望、哈希一致；篡改哈希被拒绝。"""
        prefix = tmp_path / "golden"
        npz_path, json_path = golden_ds.save(prefix)
        assert npz_path.is_file() and json_path.is_file()
        loaded = GoldenDataset.load(prefix)
        assert loaded.hash == golden_ds.hash
        assert len(loaded.cases) == len(golden_ds.cases)
        for a, b in zip(golden_ds.cases, loaded.cases):
            assert np.array_equal(a.frame, b.frame)
            assert json.dumps(a.expected, sort_keys=True) == json.dumps(b.expected, sort_keys=True)
        # 篡改检测：哈希不匹配 → 拒绝加载
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        payload["hash"] = "0" * 64
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="哈希"):
            GoldenDataset.load(prefix)

    def test_full_chain_metrics_meet_gates(self, chain_report):
        """全链路指标（§7.2）：P/R ≥0.98、比例 MAE ≤0.03、P95 ≤0.05、定位准。"""
        _ds, _detectors, results, expected = chain_report
        report = metrics([r.results for r in results], expected)
        presence = report["presence"]
        assert presence["precision"] >= 0.98, f"precision={presence['precision']}"
        assert presence["recall"] >= 0.98, f"recall={presence['recall']}"
        ratio = report["ratio"]
        assert ratio["count"] >= 20
        assert ratio["mae"] <= 0.03, f"MAE {ratio['mae']:.4f}"
        assert ratio["p95_abs_error"] <= 0.05, f"P95 {ratio['p95_abs_error']:.4f}"
        loc = report["localization"]
        assert loc["center_error_p95"] <= 2.0
        assert loc["iou_mean"] >= 0.8

    def test_metrics_synthetic_counts(self):
        """指标器单元语义：tp/fp/fn、定位误差、MAE、耗时 p50。"""
        results = [
            {"a": _res(True, 0.9, value=0.50, bbox=(10, 10, 10, 10), elapsed_ms=1.0)},
            {"a": _res(True, 0.8, value=0.60, elapsed_ms=3.0)},
            {"a": _res(True, 0.9, value=0.9, bbox=(0, 0, 5, 5), elapsed_ms=2.0)},
        ]
        expected = [
            {"a": {"kind": "ratio", "present": True, "value": 0.5, "bbox": (10, 10, 10, 10)}},
            {"a": {"kind": "ratio", "present": True, "value": 0.4, "bbox": None}},
            {"a": {"kind": "ratio", "present": False, "value": None, "bbox": None}},
        ]
        m = metrics(results, expected)
        p = m["presence"]
        assert (p["tp"], p["fp"], p["fn"]) == (2, 1, 0)
        assert p["precision"] == pytest.approx(2 / 3)
        assert p["recall"] == pytest.approx(1.0)
        # 比例误差只统计双方有数值的对：|0.5-0.5|=0、|0.6-0.4|=0.2
        assert m["ratio"]["count"] == 2
        assert m["ratio"]["mae"] == pytest.approx(0.1)
        # 定位只在期望带框且判真的样本上：case0 完全重合
        assert m["localization"]["count"] == 1
        assert m["localization"]["iou_mean"] == pytest.approx(1.0)
        assert m["timing"]["p50_ms"] == pytest.approx(2.0)

    def test_dataset_version_and_meta(self, golden_ds):
        """数据集版本与元信息可追溯。"""
        assert golden_ds.version == "1"
        assert golden_ds.meta["scenario"] == "happy_path"
        assert golden_ds.meta["seed"] == SEED
        assert len(golden_ds.hash) == 64


# ---------------------------------------------------------------------------
# VIS-012 离线批量运行与差异报告
# ---------------------------------------------------------------------------


class TestOfflineBatchAndDiff:
    def test_batch_run_covers_all_cases(self, golden_ds, renderer, template_pair):
        """批量运行：样本全覆盖、序号对齐、错误不中断批次。"""
        detectors = _chain_detectors(renderer, template_pair)
        results = batch_run(golden_ds, detectors)
        assert len(results) == len(golden_ds.cases)
        assert [r.case_index for r in results] == list(range(len(golden_ds.cases)))
        assert all(len(r.results) == len(detectors) for r in results)

    def test_batch_run_deterministic(self, chain_report):
        """确定性：同数据集两次运行，非时序输出完全一致（忽略耗时）。"""
        dataset, detectors, results, _expected = chain_report
        rerun = batch_run(dataset, detectors)
        assert len(rerun) == len(results)
        for a, b in zip(results, rerun):
            assert a.case_index == b.case_index
            for field, res in a.results.items():
                other = b.results[field]
                assert (res.present, res.value, res.confidence, res.bbox, res.error) == (
                    other.present,
                    other.value,
                    other.confidence,
                    other.bbox,
                    other.error,
                )

    def test_diff_identical_runs_have_no_diffs(self):
        """两次完全一致的输入：无样本差异、无退化、出现率/置信度零变化。"""
        base = [CaseResult(0, {"f": _res(True, 0.9, value=1.0)})]
        new = [CaseResult(0, {"f": _res(True, 0.9, value=1.0)})]
        report = diff_reports(base, new)
        assert report.sample_diffs == ()
        assert report.regressed_cases == ()
        assert report.metric_changes["f.presence_rate"] == 0.0
        assert report.metric_changes["f.mean_confidence"] == 0.0

    def test_diff_detects_regression_and_value_drift(self):
        """退化标记：命中丢失与错误新增标 regressed；数值/框漂移进样本差异。"""
        base = [
            CaseResult(0, {"f": _res(True, 0.9, value=0.5, bbox=(1, 1, 2, 2))}),
            CaseResult(1, {"f": _res(False, 0.0)}),
        ]
        new = [
            CaseResult(0, {"f": _res(False, 0.2, value=None, bbox=(1, 1, 2, 2))}),
            CaseResult(1, {"f": _res(False, 0.0, error="engine_unavailable")}),
        ]
        report = diff_reports(base, new)
        kinds = {(d.case_index, d.field, d.kind, d.regressed) for d in report.sample_diffs}
        assert (0, "f", "present_flip", True) in kinds
        assert (0, "f", "value", False) in kinds
        assert (1, "f", "error", True) in kinds
        assert report.regressed_cases == (0, 1)
        assert report.metric_changes["f.presence_rate"] == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# 等价字段配置（非 domain_model.Detector 也可构造）
# ---------------------------------------------------------------------------


class TestDuckTypedConfig:
    def test_simple_namespace_config_accepted(self, renderer):
        """等价字段对象（SimpleNamespace）与 domain 配置行为一致。"""
        cfg = SimpleNamespace(
            detector_id="health-ns",
            type="color_bar_ratio",
            roi=list(_norm(renderer.health_bar_inner_rect())),
            threshold=0.5,
            stable_frames=1,
            field_name="health_ratio",
        )
        det = ColorBarRatioDetector(cfg)
        state = SceneState(health_ratio=0.6, frame_index=1)
        frame = Renderer(SceneConfig(seed=1, resolution=RES)).render(state)
        result = det.detect(_frame(frame))
        assert abs(result.value - 0.6) <= 0.01
