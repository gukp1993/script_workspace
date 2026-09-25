"""视觉黄金数据集与指标计算器（VIS-011）。

- :class:`GoldenCase`：一帧 + 期望（由 arena_lab 场景 + Oracle 真值自动
  标注，而非人工标注）；
- :class:`GoldenDataset`：样本集 + 元信息（版本、内容哈希）；``save/load``
  以 npz（帧像素）+ json（期望与元信息）落盘，读取时校验哈希；
- :func:`build_from_scenario`：用 arena_lab 的确定性场景渲染帧，并按
  场景真值自动生成期望——health/resource 用比例（color_bar）期望、
  target/loot/popup 用带 bbox 的出现期望、loading 用出现+进度期望、
  ``screen_changed`` 用相邻帧状态差期望（变化检测语义）；
- :func:`metrics`：presence 精度/召回、bbox 定位误差（中心距/IoU）、
  比例 MAE 与 P95 绝对误差、耗时 p50/p95。

期望条目 schema（每个 case 的 ``expected`` 映射值）::

    {"kind": "ratio" | "presence", "present": bool,
     "value": float | None, "bbox": (x, y, w, h) | None}

确定性约定：``build_from_scenario`` 只依赖 (name, seed, resolution, fps,
duration, ui_scale, 采样参数)，两次构建的帧、期望与哈希完全一致。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from arena_lab.render import Renderer, SceneState
from arena_lab.scenario import run_scenario
from vision_core.base import DetectorResult

__all__ = [
    "GOLDEN_FORMAT_VERSION",
    "GoldenCase",
    "GoldenDataset",
    "build_from_scenario",
    "metrics",
]

#: 黄金数据集格式版本（save/load 往返与兼容性判断依据）。
GOLDEN_FORMAT_VERSION = 1

#: 场景状态中不参与"画面变化"判定的字段（帧号是逐帧必然变化的标签）。
_STATE_CHANGE_IGNORE = frozenset({"frame_index"})


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """一个黄金样本：一帧 + 该帧的期望（自动标注真值）。

    Attributes:
        frame:         (H, W, 3) uint8 RGB 帧像素。
        expected:      语义字段名 -> 期望条目（schema 见模块 docstring）。
        frame_seq:     帧序号（回溯场景帧序）。
        ts_monotonic:  场景单调时间（秒，frame_index / fps）。
        source:        来源描述（场景名/种子等，诊断用）。
    """

    frame: np.ndarray
    expected: dict[str, dict[str, Any]]
    frame_seq: int = 0
    ts_monotonic: float = 0.0
    source: str = ""


def _entry(
    kind: str,
    present: bool,
    value: float | None = None,
    bbox: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """构造一个规范化期望条目。"""
    return {"kind": kind, "present": bool(present), "value": value, "bbox": bbox}


def _rect_to_bbox(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """渲染器 (x0, y0, x1, y1) 半开矩形 -> bbox (x, y, w, h)。"""
    x0, y0, x1, y1 = rect
    return (int(x0), int(y0), int(x1 - x0), int(y1 - y0))


def _state_payload(state: SceneState) -> tuple[Any, ...]:
    """提取参与画面变化判定的状态字段（忽略帧号）。"""
    return tuple(
        getattr(state, name)
        for name in SceneState.__dataclass_fields__
        if name not in _STATE_CHANGE_IGNORE
    )


def build_from_scenario(
    name: str,
    seed: int,
    *,
    resolution: tuple[int, int] = (1280, 720),
    fps: float = 30.0,
    duration_s: float = 10.0,
    ui_scale: float = 1.0,
    frame_stride: int = 1,
    max_frames: int | None = None,
    dataset_version: str = "1",
    extra_meta: Mapping[str, Any] | None = None,
) -> GoldenDataset:
    """用 arena_lab 内置场景构建黄金数据集（场景 + Oracle 真值自动标注）。

    期望生成规则（与渲染器布局访问器一致，跨分辨率成立）：

    - 加载帧：``loading_present``（出现 + 进度值）；
    - 常规帧：``health_ratio``/``resource_ratio``（ratio 期望）、
      ``target_present``/``loot_present``/``popup_present``（presence 期望，
      出现时带渲染器给出的真值 bbox）；
    - ``screen_changed``：相邻采样帧的场景状态（除帧号外）是否变化
      （变化检测器语义的真值；首帧无前一帧时不生成该期望）。

    ``frame_stride``/``max_frames`` 控制采样密度；采样不改变确定性——
    同参数两次构建逐字节一致。
    """
    if int(frame_stride) < 1:
        raise ValueError(f"frame_stride 必须 >=1，收到 {frame_stride!r}")
    run = run_scenario(
        name,
        seed,
        resolution=resolution,
        fps=fps,
        duration_s=duration_s,
        ui_scale=ui_scale,
        with_frames=True,
    )
    renderer = Renderer(run.config)
    target_bbox = _rect_to_bbox(renderer.target_rect())
    loot_bbox = _rect_to_bbox(renderer.loot_rect())
    popup_bbox = _rect_to_bbox(renderer.popup_rect())

    cases: list[GoldenCase] = []
    prev_payload: tuple[Any, ...] | None = None
    picked = 0
    states = run.states
    frames = run.frames
    assert frames is not None  # with_frames=True，run_scenario 保证非 None
    for index in range(0, len(states), int(frame_stride)):
        if max_frames is not None and picked >= int(max_frames):
            break
        state = states[index]
        expected: dict[str, dict[str, Any]] = {}
        if state.loading_active:
            expected["loading_present"] = _entry(
                "presence", True, value=float(state.loading_progress)
            )
        else:
            expected["health_ratio"] = _entry(
                "ratio", True, value=float(state.health_ratio)
            )
            expected["resource_ratio"] = _entry(
                "ratio", True, value=float(state.resource_ratio)
            )
            expected["target_present"] = _entry(
                "presence", state.target_present, bbox=target_bbox if state.target_present else None
            )
            expected["loot_present"] = _entry(
                "presence", state.loot_present, bbox=loot_bbox if state.loot_present else None
            )
            expected["popup_present"] = _entry(
                "presence", state.popup_open, bbox=popup_bbox if state.popup_open else None
            )
        payload = _state_payload(state)
        if prev_payload is not None:
            expected["screen_changed"] = _entry("presence", payload != prev_payload)
        prev_payload = payload
        cases.append(
            GoldenCase(
                frame=frames[index],
                expected=expected,
                frame_seq=index,
                ts_monotonic=index / run.fps,
                source=f"{name}/seed={seed}#frame{index}",
            )
        )
        picked += 1

    meta: dict[str, Any] = {
        "scenario": run.name,
        "seed": run.seed,
        "fps": run.fps,
        "duration_s": run.duration_s,
        "resolution": [run.config.resolution[0], run.config.resolution[1]],
        "ui_scale": run.config.ui_scale,
        "frame_stride": int(frame_stride),
        "dataset_version": str(dataset_version),
    }
    if extra_meta:
        meta.update(dict(extra_meta))
    return GoldenDataset(cases=tuple(cases), meta=meta)


def _canonical_expected(expected: Mapping[str, Mapping[str, Any]]) -> str:
    """期望的规范化 JSON（排序键），用于哈希与落盘。"""
    return json.dumps(
        {k: dict(v) for k, v in sorted(expected.items())},
        ensure_ascii=False,
        sort_keys=True,
    )


def _dataset_hash(cases: Sequence[GoldenCase], meta: Mapping[str, Any]) -> str:
    """数据集内容哈希：版本 + 元信息 + 逐帧像素 + 期望（确定性）。"""
    digest = hashlib.sha256()
    digest.update(
        json.dumps(dict(sorted(meta.items())), ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    for case in cases:
        digest.update(np.ascontiguousarray(case.frame).tobytes())
        digest.update(_canonical_expected(case.expected).encode("utf-8"))
        digest.update(f"|{case.frame_seq}|{case.ts_monotonic:.6f}|{case.source}".encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class GoldenDataset:
    """黄金数据集：样本集 + 元信息（含版本与内容哈希）。

    构造时自动计算内容哈希（``hash``）；``save``/``load`` 以
    ``<prefix>.npz``（帧像素）+ ``<prefix>.json``（期望与元信息）落盘，
    读取时重算哈希校验完整性。
    """

    cases: tuple[GoldenCase, ...]
    meta: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.cases, tuple):
            object.__setattr__(self, "cases", tuple(self.cases))
        if not self.cases:
            raise ValueError("黄金数据集至少需要 1 个样本")

    @property
    def version(self) -> str:
        """数据集版本（meta.dataset_version）。"""
        return str(self.meta.get("dataset_version", "0"))

    @property
    def hash(self) -> str:
        """内容哈希（sha256 hex，帧像素 + 期望 + 元信息）。"""
        return _dataset_hash(self.cases, self.meta)

    # ---- 序列化 -----------------------------------------------------------

    def save(self, prefix: str | Path) -> tuple[Path, Path]:
        """保存为 ``<prefix>.npz`` + ``<prefix>.json``；返回两个文件路径。"""
        npz_path = Path(f"{prefix}.npz")
        json_path = Path(f"{prefix}.json")
        for path in (npz_path, json_path):
            if path.parent != Path(""):
                path.parent.mkdir(parents=True, exist_ok=True)
        frames = np.stack([np.ascontiguousarray(c.frame) for c in self.cases], axis=0)
        np.savez_compressed(npz_path, frames=frames)
        payload = {
            "format": f"vision_core.golden/{GOLDEN_FORMAT_VERSION}",
            "hash": self.hash,
            "meta": self.meta,
            "expected": [_canonical_expected(c.expected) for c in self.cases],
            "frame_seq": [int(c.frame_seq) for c in self.cases],
            "ts_monotonic": [float(c.ts_monotonic) for c in self.cases],
            "source": [c.source for c in self.cases],
        }
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return npz_path, json_path

    @classmethod
    def load(cls, prefix: str | Path, *, verify_hash: bool = True) -> "GoldenDataset":
        """从 :meth:`save` 的两个文件还原数据集；哈希不符抛 ``ValueError``。"""
        npz_path = Path(f"{prefix}.npz")
        json_path = Path(f"{prefix}.json")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        fmt = str(payload.get("format", ""))
        if fmt != f"vision_core.golden/{GOLDEN_FORMAT_VERSION}":
            raise ValueError(f"不支持的黄金数据集格式：{fmt!r}")
        frames = np.load(npz_path, allow_pickle=False)["frames"]
        cases = tuple(
            GoldenCase(
                frame=frames[i],
                expected=json.loads(payload["expected"][i]),
                frame_seq=int(payload["frame_seq"][i]),
                ts_monotonic=float(payload["ts_monotonic"][i]),
                source=str(payload["source"][i]),
            )
            for i in range(frames.shape[0])
        )
        meta = dict(payload["meta"])
        dataset = cls(cases=cases, meta=meta)
        if verify_hash and dataset.hash != str(payload.get("hash")):
            raise ValueError(
                f"黄金数据集哈希不匹配：期望 {payload.get('hash')!r}，实算 {dataset.hash!r}"
            )
        return dataset


def _percentile(values: Sequence[float], q: float) -> float:
    """确定性百分位：排序后线性插值（与 numpy 默认 linear 一致）。"""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def metrics(
    results: Sequence[Mapping[str, DetectorResult]],
    expected: Sequence[Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """计算检测批次指标（VIS-011 契约）。

    ``results[i]``/``expected[i]`` 为第 i 个样本的 字段名 -> 检测结果/期望。
    返回字典分组：

    - ``presence``：tp/fp/fn/tn 与 precision/recall/accuracy（期望条目的
      ``present`` 为真值；缺失的检测结果按未命中计）；
    - ``localization``：期望带 bbox 且判真的样本上，中心误差
      mean/p95（像素）与 IoU mean；
    - ``ratio``：``kind == "ratio"`` 且双方有数值的字段上，
      MAE 与 P95 绝对误差；
    - ``timing``：全部结果的耗时 p50/p95/mean（毫秒）。

    除 timing 外全部取值由 (present, value, bbox) 决定——同输入两次计算
    完全一致（确定性门禁用）。
    """
    if len(results) != len(expected):
        raise ValueError(
            f"results 与 expected 数量不一致：{len(results)} != {len(expected)}"
        )
    tp = fp = fn = tn = 0
    center_errors: list[float] = []
    ious: list[float] = []
    ratio_errors: list[float] = []
    elapsed: list[float] = []

    def _center(bbox: tuple[int, int, int, int]) -> tuple[float, float]:
        x, y, w, h = bbox
        return (x + w / 2.0, y + h / 2.0)

    def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        ix = max(ax, bx)
        iy = max(ay, by)
        ix1 = min(ax + aw, bx + bw)
        iy1 = min(ay + ah, by + bh)
        inter = max(0, ix1 - ix) * max(0, iy1 - iy)
        union = aw * ah + bw * bh - inter
        return inter / union if union > 0 else 0.0

    for case_results, case_expected in zip(results, expected):
        for field, exp in case_expected.items():
            want = bool(exp.get("present", False))
            result = case_results.get(field)
            got = bool(result.present) if result is not None else False
            if want and got:
                tp += 1
            elif got and not want:
                fp += 1
            elif want and not got:
                fn += 1
            else:
                tn += 1
            if result is not None:
                elapsed.append(float(result.elapsed_ms))
            # 定位误差：期望有 bbox 且双方都判真。
            exp_bbox = exp.get("bbox")
            if want and got and exp_bbox is not None and result is not None and result.bbox is not None:
                cx, cy = _center(tuple(result.bbox))  # type: ignore[arg-type]
                gx, gy = _center(tuple(exp_bbox))  # type: ignore[arg-type]
                center_errors.append(((cx - gx) ** 2 + (cy - gy) ** 2) ** 0.5)
                ious.append(_iou(tuple(result.bbox), tuple(exp_bbox)))  # type: ignore[arg-type]
            # 比例误差：ratio 期望且双方有数值。
            if (
                str(exp.get("kind", "")) == "ratio"
                and exp.get("value") is not None
                and result is not None
                and isinstance(result.value, (int, float))
                and not isinstance(result.value, bool)
            ):
                ratio_errors.append(abs(float(result.value) - float(exp["value"])))

    def _pr(p: int, q: int) -> float:
        return p / (p + q) if (p + q) > 0 else 0.0

    return {
        "presence": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": _pr(tp, fp),
            "recall": _pr(tp, fn),
            "accuracy": (tp + tn) / (tp + fp + fn + tn) if (tp + fp + fn + tn) else 0.0,
        },
        "localization": {
            "count": len(center_errors),
            "center_error_mean": sum(center_errors) / len(center_errors) if center_errors else 0.0,
            "center_error_p95": _percentile(center_errors, 0.95),
            "iou_mean": sum(ious) / len(ious) if ious else 0.0,
        },
        "ratio": {
            "count": len(ratio_errors),
            "mae": sum(ratio_errors) / len(ratio_errors) if ratio_errors else 0.0,
            "p95_abs_error": _percentile(ratio_errors, 0.95),
        },
        "timing": {
            "p50_ms": _percentile(elapsed, 0.50),
            "p95_ms": _percentile(elapsed, 0.95),
            "mean_ms": sum(elapsed) / len(elapsed) if elapsed else 0.0,
        },
    }
