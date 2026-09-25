"""事件触发式 ROI OCR（VIS-007，M2 为接口 + 确定性 mock 引擎）。

- :class:`OcrEngine`：OCR 引擎协议（输入 ROI 图像，输出文本 + 置信度）；
- :class:`MockOcrEngine`：确定性 mock——按注册的"图像内容哈希 -> 文本"
  返回，不依赖任何真实 OCR 库，同输入永远同输出；
- :class:`RoiOcrDetector`：仅当触发条件满足时才调用引擎（调用计数可
  观测），带超时保护与空结果处理；引擎缺失/不可用返回
  ``error="engine_unavailable"``（AdapterUnavailable 语义）而非抛异常；
- :class:`TesseractOcrEngine`：真实 OCR 占位——只有安装了 ``pytesseract``
  才可构造；缺库时构造抛 :class:`EngineUnavailableError`，运行期检测器
  会以错误结果兜底，**不引入硬依赖**。

结果约定：触发条件不满足 → 不调用引擎，返回 present=False、error=None；
识别空文本 → present=False、error=None；超时/引擎不可用 →
present=False、error="ocr_timeout"/"engine_unavailable"，绝不阻塞主循环。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from capture_api.frames import Frame
from detector_opencv._common import crop_roi, now_ms, resolve_roi
from vision_core.base import Detector, DetectorResult, NormRoi, clamp01, result_error

__all__ = [
    "OcrReading",
    "OcrEngine",
    "EngineUnavailableError",
    "MockOcrEngine",
    "TesseractOcrEngine",
    "RoiOcrDetector",
]


@dataclass(frozen=True, slots=True)
class OcrReading:
    """一次 OCR 识别结果。"""

    text: str
    confidence: float


class EngineUnavailableError(RuntimeError):
    """OCR 引擎不可用（依赖缺失、初始化失败）——AdapterUnavailable 语义。

    与单次识别失败区分：前者在构造阶段抛出（携带结构化原因），由调用方
    决定是否降级；运行期检测器将其转为 ``error="engine_unavailable"``。
    """

    def __init__(self, reason: str, *, engine: str = "unknown") -> None:
        super().__init__(f"[{engine}] OCR 引擎不可用: {reason}")
        self.reason = reason
        self.engine = engine


@runtime_checkable
class OcrEngine(Protocol):
    """OCR 引擎协议（VIS-007）。"""

    def recognize(self, image: np.ndarray) -> OcrReading:
        """对 ROI 图像（RGB ndarray）识别文本。"""
        ...


class MockOcrEngine:
    """确定性 mock OCR 引擎：按"图像内容哈希 -> 文本"注册与返回。

    - ``register(image, text)``：注册一段 ROI 图像（RGB ndarray）对应的
      期望文本（sha256 字节哈希为键，与缩放/无关字节无关）；
    - ``recognize(image)``：命中注册表 → 返回注册文本 + 置信度 1.0；
      未命中 → 空文本 + 置信度 0.0；
    - ``simulated_latency_s`` > 0 时按给定时长阻塞（模拟慢引擎，用于
      超时路径测试）；``calls`` 计数器记录真实调用次数。
    """

    def __init__(self, *, simulated_latency_s: float = 0.0) -> None:
        self._registry: dict[str, str] = {}
        self._latency = max(0.0, float(simulated_latency_s))
        #: ``recognize`` 被真实调用的次数（触发条件测试的观测点）。
        self.calls: int = 0

    def register(self, image: np.ndarray, text: str) -> str:
        """注册 图像内容哈希 -> 文本；返回内容哈希（诊断用）。"""
        key = content_hash(image)
        self._registry[key] = str(text)
        return key

    @property
    def registered_count(self) -> int:
        """已注册条目数。"""
        return len(self._registry)

    def recognize(self, image: np.ndarray) -> OcrReading:
        """查注册表返回文本（完全确定性）；模拟延迟在识别前发生。"""
        self.calls += 1
        if self._latency > 0.0:
            time.sleep(self._latency)
        key = content_hash(image)
        text = self._registry.get(key)
        if text is None:
            return OcrReading(text="", confidence=0.0)
        return OcrReading(text=text, confidence=1.0)


def content_hash(image: np.ndarray) -> str:
    """图像内容哈希（sha256，RGB 字节序，确定性）。"""
    return hashlib.sha256(np.ascontiguousarray(image).tobytes()).hexdigest()


class TesseractOcrEngine:
    """真实 OCR 占位实现（Tesseract）。

    **不引入硬依赖**：仅当运行环境已安装 ``pytesseract``（及系统
    tesseract 可执行文件）时可用；缺库时构造抛
    :class:`EngineUnavailableError`。M2 的功能与验收只依赖
    :class:`MockOcrEngine`，本类仅为生产接入预留接口形状。
    """

    def __init__(self, *, language: str = "eng") -> None:
        try:
            import pytesseract  # noqa: PLC0415 - 惰性导入，缺库不阻塞包加载
        except ImportError as exc:
            raise EngineUnavailableError(
                f"pytesseract 未安装（{exc}）", engine="tesseract"
            ) from exc
        self._pytesseract = pytesseract
        self._language = str(language)

    def recognize(self, image: np.ndarray) -> OcrReading:
        """调用 Tesseract 识别（RGB ndarray -> 文本）。"""
        import pytesseract  # noqa: PLC0415 - 与构造一致的惰性导入

        text = pytesseract.image_to_string(image, lang=self._language).strip()
        try:
            data = pytesseract.image_to_data(image, lang=self._language, output_type=pytesseract.Output.DICT)
            confs = [float(c) for c in data.get("conf", []) if c not in ("-1", -1)]
            confidence = (sum(confs) / len(confs) / 100.0) if confs else 0.0
        except Exception:  # noqa: BLE001 - 置信度属增强信息，失败不阻断
            confidence = 0.0
        return OcrReading(text=text, confidence=clamp01(confidence))


class RoiOcrDetector:
    """事件触发式 ROI OCR 检测器（VIS-007）。

    触发条件（全部满足才调用引擎，节省算力预算）：

    - ``trigger_detector``（可选）：门控检测器（如模板命中/区域出现）
      对同一帧判定 present；
    - ``min_interval_frames``：两次 OCR 的最小帧间隔（内部帧计数）。

    保护路径：引擎调用计时超 ``timeout_s`` → ``error="ocr_timeout"``；
    引擎抛 :class:`EngineUnavailableError` → ``error="engine_unavailable"``；
    空文本 → present=False、error=None。``engine_calls`` 记录真实调用数。
    """

    version: str = "1.0.0"

    def __init__(
        self,
        config: object,
        engine: OcrEngine,
        *,
        trigger_detector: Detector | None = None,
        min_interval_frames: int = 1,
        timeout_s: float = 0.5,
    ) -> None:
        """``config`` 为 ``domain_model.Detector``（roi = 识别区域）。

        ``engine`` 不可用时**允许**先构造、后兜底：识别期以
        ``engine_unavailable`` 错误结果返回，绝不打断主循环。
        """
        self._detector_id = str(getattr(config, "detector_id"))
        self._field_name = str(getattr(config, "field_name", self._detector_id))
        self._roi = tuple(float(v) for v in getattr(config, "roi"))  # type: ignore[arg-type]
        self._engine = engine
        self._trigger = trigger_detector
        self._min_interval = max(1, int(min_interval_frames))
        self._timeout_s = float(timeout_s)
        if self._timeout_s <= 0.0:
            raise ValueError(f"timeout_s 必须为正数，收到 {timeout_s!r}")
        #: OCR 引擎被真实调用的次数（触发条件观测点）。
        self.engine_calls: int = 0
        self._frames_since_call = self._min_interval  # 允许首帧即触发
        self._engine_available = True  # 运行期探测；首次失败后置 False

    # ---- 属性 ------------------------------------------------------------

    @property
    def detector_id(self) -> str:
        """检测器 ID。"""
        return self._detector_id

    @property
    def field_name(self) -> str:
        """输出语义字段名。"""
        return self._field_name

    # ---- 检测 ------------------------------------------------------------

    def detect(self, frame: Frame, roi: NormRoi | None = None) -> DetectorResult:
        """按触发条件决定是否 OCR；未触发不消耗引擎调用与预算。"""
        start = now_ms()

        def elapsed() -> float:
            return now_ms() - start

        try:
            rect = resolve_roi(frame, roi, self._roi)  # type: ignore[arg-type]
        except ValueError:
            return result_error(self._detector_id, "invalid_roi", elapsed(), self.version)

        self._frames_since_call += 1
        # 触发条件 1：门控检测器未命中 → 不 OCR。
        if self._trigger is not None and not self._trigger.detect(frame).present:
            return DetectorResult(
                detector_id=self._detector_id,
                present=False,
                confidence=0.0,
                value=None,
                bbox=None,
                elapsed_ms=elapsed(),
                version=self.version,
                error=None,
            )
        # 触发条件 2：帧间隔未到 → 不 OCR。
        if self._frames_since_call < self._min_interval:
            return DetectorResult(
                detector_id=self._detector_id,
                present=False,
                confidence=0.0,
                value=None,
                bbox=None,
                elapsed_ms=elapsed(),
                version=self.version,
                error=None,
            )

        # 引擎此前已确认不可用 → 不再重复调用。
        if not self._engine_available:
            return result_error(
                self._detector_id, "engine_unavailable", elapsed(), self.version
            )

        image = crop_roi(frame, rect)
        call_start = now_ms()
        self.engine_calls += 1
        self._frames_since_call = 0
        try:
            reading = self._engine.recognize(image)
        except EngineUnavailableError:
            self._engine_available = False
            return result_error(
                self._detector_id, "engine_unavailable", now_ms() - start, self.version
            )
        except Exception:  # noqa: BLE001 - 引擎任意故障都不阻塞主循环
            return result_error(
                self._detector_id, "ocr_engine_failure", now_ms() - start, self.version
            )
        if (now_ms() - call_start) > self._timeout_s:
            return result_error(self._detector_id, "ocr_timeout", elapsed(), self.version)
        text = (reading.text or "").strip()
        if not text:
            # 空结果：可观测但不是错误（VIS-012 用例语义）。
            return DetectorResult(
                detector_id=self._detector_id,
                present=False,
                confidence=0.0,
                value=None,
                bbox=None,
                elapsed_ms=elapsed(),
                version=self.version,
                error=None,
            )
        return DetectorResult(
            detector_id=self._detector_id,
            present=True,
            confidence=clamp01(reading.confidence),
            value=text,
            bbox=(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]),
            elapsed_ms=elapsed(),
            version=self.version,
            error=None,
        )
