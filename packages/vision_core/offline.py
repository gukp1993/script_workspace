"""检测器离线批量运行与差异报告（VIS-012）。

- :func:`batch_run`：在黄金数据集上按数据集顺序逐样本运行一批检测器，
  输出 :class:`CaseResult`（字段名 -> 检测结果）；不含任何并发与时序
  依赖，同数据集 + 同检测器两次运行的非时序输出完全一致；
- :func:`diff_reports`：比较基线与新版结果——样本级差异列表（出现翻转、
  数值漂移、置信度漂移、bbox 变化、错误新增）+ 总体指标变化 + 退化样本
  标记（用于资产/阈值变更后的自动回归）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from capture_api.frames import Frame, FrameMeta
from vision_core.base import Detector, DetectorResult
from vision_core.golden import GoldenCase, GoldenDataset

__all__ = ["CaseResult", "SampleDiff", "DiffReport", "batch_run", "diff_reports"]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """一个样本的批量运行结果。

    Attributes:
        case_index: 样本在数据集中的序号。
        results:    语义字段名（检测器 ``field_name``，缺省回落
                    ``detector_id``）-> 检测结果。
    """

    case_index: int
    results: dict[str, DetectorResult]


def _field_name(detector: Detector) -> str:
    """取检测器的输出语义字段名（协议未强制时回落 detector_id）。"""
    name = getattr(detector, "field_name", None)
    return str(name) if name else str(detector.detector_id)


def _case_frame(case: GoldenCase) -> Frame:
    """把黄金样本的像素包装为 :class:`Frame`（元信息来自样本记录）。"""
    meta = FrameMeta(
        seq=case.frame_seq,
        ts_monotonic=case.ts_monotonic,
        adapter="golden",
        source_width=case.frame.shape[1],
        source_height=case.frame.shape[0],
    )
    return Frame(case.frame, meta)


def batch_run(
    dataset: GoldenDataset, detectors: Sequence[Detector]
) -> list[CaseResult]:
    """在数据集上离线批量运行检测器（顺序与数据集一致，确定性）。

    每个样本、每个检测器各调用一次 ``detect(frame)``（使用检测器自身配置
    的 ROI）；错误以 ``DetectorResult.error`` 表达，不中断批次。
    """
    if not detectors:
        raise ValueError("detectors 不能为空")
    out: list[CaseResult] = []
    for index, case in enumerate(dataset.cases):
        results: dict[str, DetectorResult] = {}
        frame = _case_frame(case)
        for detector in detectors:
            results[_field_name(detector)] = detector.detect(frame)
        out.append(CaseResult(case_index=index, results=results))
    return out


def _present(res: DetectorResult | None) -> bool:
    return bool(res.present) if res is not None else False


def _value_equal(
    a: object, b: object, value_tol: float
) -> bool:
    """值相等判断：数值按容差比较，其余按类型一致 + 相等。"""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= value_tol
    return type(a) is type(b) and a == b


def _bbox_equal(
    a: tuple[int, int, int, int] | None, b: tuple[int, int, int, int] | None
) -> bool:
    return a == b


@dataclass(frozen=True, slots=True)
class SampleDiff:
    """一条样本级差异。

    Attributes:
        case_index:  样本序号。
        field:       语义字段名。
        kind:        差异类别：present_flip / value / confidence / bbox / error。
        description: 人类可读描述。
        regressed:   是否为退化（新版更差：命中丢失或错误新增）。
    """

    case_index: int
    field: str
    kind: str
    description: str
    regressed: bool = False


@dataclass(frozen=True)
class DiffReport:
    """基线 vs 新版的差异报告。

    Attributes:
        sample_diffs:   样本级差异列表（按样本序号、字段名排序，确定性）。
        metric_changes: 总体指标变化（新版 - 基线）：逐字段出现率、平均
                        置信度、p95 耗时。
        regressed_cases: 出现退化差异的样本序号（去重升序）。
        base_summary:   基线逐字段汇总。
        new_summary:    新版逐字段汇总。
    """

    sample_diffs: tuple[SampleDiff, ...]
    metric_changes: dict[str, float]
    regressed_cases: tuple[int, ...]
    base_summary: dict[str, dict[str, float]] = field(default_factory=dict)
    new_summary: dict[str, dict[str, float]] = field(default_factory=dict)


def _summarize(results: Sequence[CaseResult]) -> dict[str, dict[str, float]]:
    """逐字段汇总：出现率 / 平均置信度 / p95 耗时（供指标变化比较）。"""
    fields: set[str] = set()
    for case in results:
        fields.update(case.results)
    summary: dict[str, dict[str, float]] = {}
    for name in sorted(fields):
        present = 0
        confs: list[float] = []
        elapsed: list[float] = []
        for case in results:
            res = case.results.get(name)
            if res is None:
                continue
            if res.present:
                present += 1
            confs.append(float(res.confidence))
            elapsed.append(float(res.elapsed_ms))
        elapsed_sorted = sorted(elapsed)
        p95 = (
            elapsed_sorted[min(len(elapsed_sorted) - 1, int(0.95 * (len(elapsed_sorted) - 1)))]
            if elapsed_sorted
            else 0.0
        )
        summary[name] = {
            "presence_rate": present / len(results) if results else 0.0,
            "mean_confidence": sum(confs) / len(confs) if confs else 0.0,
            "p95_elapsed_ms": p95,
        }
    return summary


def diff_reports(
    base_results: Sequence[CaseResult],
    new_results: Sequence[CaseResult],
    *,
    value_tol: float = 1e-9,
    confidence_tol: float = 1e-6,
) -> DiffReport:
    """比较基线与新版批量结果，生成差异报告。

    - 样本级：present 翻转、value 漂移（超 ``value_tol``）、confidence
      漂移（超 ``confidence_tol``）、bbox 变化、error 新增/消除；
    - ``regressed``：命中 True->False 或 error None->非 None 视为退化；
    - 总体：逐字段出现率/平均置信度/p95 耗时的变化量（新版 - 基线）。

    耗时（elapsed_ms）天然波动，不参与样本级差异判定，仅以 p95 变化量
    报告供参考。
    """
    if len(base_results) != len(new_results):
        raise ValueError(
            f"基线与新版样本数不一致：{len(base_results)} != {len(new_results)}"
        )
    diffs: list[SampleDiff] = []
    regressed: set[int] = set()
    for base_case, new_case in zip(base_results, new_results):
        if base_case.case_index != new_case.case_index:
            raise ValueError(
                f"样本序号不一致：{base_case.case_index} != {new_case.case_index}"
            )
        idx = base_case.case_index
        names = sorted(set(base_case.results) | set(new_case.results))
        for name in names:
            base = base_case.results.get(name)
            new = new_case.results.get(name)
            base_present = _present(base)
            new_present = _present(new)
            if base_present != new_present:
                flipped = base_present and not new_present  # 命中丢失 = 退化
                diffs.append(
                    SampleDiff(
                        case_index=idx,
                        field=name,
                        kind="present_flip",
                        description=f"{base_present} -> {new_present}",
                        regressed=flipped,
                    )
                )
                if flipped:
                    regressed.add(idx)
            if base is not None and new is not None:
                if not _value_equal(base.value, new.value, value_tol):
                    diffs.append(
                        SampleDiff(
                            case_index=idx,
                            field=name,
                            kind="value",
                            description=f"{base.value!r} -> {new.value!r}",
                        )
                    )
                if abs(base.confidence - new.confidence) > confidence_tol:
                    diffs.append(
                        SampleDiff(
                            case_index=idx,
                            field=name,
                            kind="confidence",
                            description=f"{base.confidence:.6f} -> {new.confidence:.6f}",
                        )
                    )
                if not _bbox_equal(base.bbox, new.bbox):
                    diffs.append(
                        SampleDiff(
                            case_index=idx,
                            field=name,
                            kind="bbox",
                            description=f"{base.bbox} -> {new.bbox}",
                        )
                    )
                base_err = base.error
                new_err = new.error
                if base_err != new_err:
                    new_error = base_err is None and new_err is not None
                    diffs.append(
                        SampleDiff(
                            case_index=idx,
                            field=name,
                            kind="error",
                            description=f"{base_err!r} -> {new_err!r}",
                            regressed=new_error,
                        )
                    )
                    if new_error:
                        regressed.add(idx)
    diffs.sort(key=lambda d: (d.case_index, d.field, d.kind))
    base_summary = _summarize(base_results)
    new_summary = _summarize(new_results)
    changes: dict[str, float] = {}
    for name in sorted(set(base_summary) | set(new_summary)):
        b = base_summary.get(name, {})
        n = new_summary.get(name, {})
        for key in ("presence_rate", "mean_confidence", "p95_elapsed_ms"):
            changes[f"{name}.{key}"] = n.get(key, 0.0) - b.get(key, 0.0)
    return DiffReport(
        sample_diffs=tuple(diffs),
        metric_changes=changes,
        regressed_cases=tuple(sorted(regressed)),
        base_summary=base_summary,
        new_summary=new_summary,
    )
