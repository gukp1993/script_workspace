"""依赖图与变更影响分析（VER-004，任务文档 §16.1）。

依赖链：模板资产 -> 检测器 -> 感知字段 -> 机器迁移（``when`` 表达式
引用的字段）-> 测试用例（项目 ``tests/`` 目录如存在则计入，否则仅到
迁移层）。回答"改了 X，哪些检测器/字段/迁移/机器/测试受影响、该重跑什么"。

字段提取：优先用 domain_model.dsl 的白名单解析器
（``parse_expression`` + ``collect_field_refs``）；表达式无法解析时
回退为**保守文本解析**（在表达式中匹配已知感知字段名的标识符），
并保留"未知表达式按全部已知字段处理"过保守策略——宁多报不漏报。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from domain_model.dsl import ParseError, collect_field_refs, parse_expression
from domain_model.parsing import ProjectBundle, load_project

from release_kit.errors import ReleaseError

#: 测试用例文件参与文本匹配的扩展名
_TEST_SUFFIXES = frozenset({".yaml", ".yml", ".json", ".txt", ".md"})


def _norm_rel(path: str | Path) -> str:
    """相对路径统一为 ``/`` 分隔并去掉前导斜杠。"""
    return str(path).replace("\\", "/").lstrip("/")


@dataclass(frozen=True)
class _TransitionSite:
    """一个迁移引用点：机器 / 状态 / 迁移序号 / 引用的感知字段。"""

    machine: str
    state: str
    index: int
    field_name: str

    @property
    def label(self) -> str:
        """展示标签：``main/exercise#0``。"""
        return f"{self.machine}/{self.state}#{self.index}"


@dataclass(frozen=True)
class ImpactReport:
    """变更影响报告。

    Attributes:
        trigger:         触发源（``asset:<路径>`` 或 ``detector:<id>``）。
        detectors:       受影响检测器（有序去重）。
        fields:          受影响感知字段。
        transitions:     受影响迁移（``机器/状态#序号``）。
        machines:        受影响状态机。
        test_cases:      项目 tests/ 下文本引用了受影响对象的测试用例。
        suggested_rerun: 建议重跑集：受影响检测器 + 状态机 + 测试用例。
    """

    trigger: str
    detectors: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    machines: list[str] = field(default_factory=list)
    test_cases: list[str] = field(default_factory=list)
    suggested_rerun: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """转可 JSON 化字典。"""
        return {
            "trigger": self.trigger,
            "detectors": list(self.detectors),
            "fields": list(self.fields),
            "transitions": list(self.transitions),
            "machines": list(self.machines),
            "test_cases": list(self.test_cases),
            "suggested_rerun": list(self.suggested_rerun),
        }


def _expression_fields(expression: str, known_fields: set[str]) -> set[str]:
    """提取 ``when`` 表达式引用的感知字段名。

    白名单解析器优先；解析失败时保守文本回退（只接受已知字段名的
    完整标识符匹配，注释：如 domain_model 缺少所需 API 可用此路径）。
    """
    try:
        return set(collect_field_refs(parse_expression(expression)))
    except ParseError:
        tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expression))
        return tokens & known_fields


class ImpactGraph:
    """项目依赖图（VER-004）：资产/检测器/字段/迁移/机器/测试用例。"""

    def __init__(self, project_dir: str | Path) -> None:
        self._root = Path(project_dir)
        #: 影响分析要求项目可解析（坏项目请先修复，再做影响分析）
        self._bundle: ProjectBundle = load_project(self._root)
        self._known_fields = self._bundle.perception_field_names

        # 检测器 -> 模板资产路径 / 输出字段
        self._detector_template: dict[str, str] = {
            detector.detector_id: _norm_rel(detector.template)
            for detector in self._bundle.detectors.values()
            if detector.template is not None
        }
        self._detector_field: dict[str, str] = {
            detector.detector_id: detector.field_name for detector in self._bundle.detectors.values()
        }

        # 感知字段 -> 引用它的迁移点
        self._field_transitions: dict[str, list[_TransitionSite]] = {}
        for machine_id in sorted(self._bundle.machines):
            machine = self._bundle.machines[machine_id]
            for state_name, state_def in machine.states.items():
                for index, transition in enumerate(state_def.transitions):
                    for field_name in sorted(_expression_fields(transition.when, self._known_fields)):
                        self._field_transitions.setdefault(field_name, []).append(
                            _TransitionSite(machine=machine_id, state=state_name, index=index, field_name=field_name)
                        )

        # 项目测试用例（如存在）：文本引用受影响对象即视为相关
        self._tests: dict[str, str] = self._scan_tests()

    # -- 查询 ---------------------------------------------------------------

    def affected_by_asset(self, asset_path: str | Path) -> ImpactReport:
        """模板资产变更的影响：引用该资产的检测器及其下游。"""
        normalized = _norm_rel(asset_path)
        detectors = sorted(
            detector_id for detector_id, template in self._detector_template.items() if template == normalized
        )
        return self._build_report(f"asset:{normalized}", detectors)

    def affected_by_detector(self, detector_id: str) -> ImpactReport:
        """检测器变更的影响：其输出字段与全部下游迁移/机器/测试。"""
        if detector_id not in self._bundle.detectors:
            raise ReleaseError(f"未知检测器：{detector_id}")
        return self._build_report(f"detector:{detector_id}", [detector_id])

    # -- 图导出 -------------------------------------------------------------

    def graph_dict(self) -> dict[str, object]:
        """依赖图数据（节点 + 边），可 JSON 导出。"""
        nodes: list[dict[str, str]] = []
        edges: list[dict[str, str]] = []

        for asset_id in sorted(self._bundle.assets):
            nodes.append({"id": f"asset:{_norm_rel(self._bundle.assets[asset_id].path)}", "kind": "asset"})
        for detector_id in sorted(self._bundle.detectors):
            nodes.append({"id": f"detector:{detector_id}", "kind": "detector"})
        for field_name in sorted(self._known_fields):
            nodes.append({"id": f"field:{field_name}", "kind": "field"})
        for field_name, sites in self._field_transitions.items():
            for site in sites:
                transition_id = f"transition:{site.label}"
                if all(node["id"] != transition_id for node in nodes):
                    nodes.append({"id": transition_id, "kind": "transition"})
        for machine_id in sorted(self._bundle.machines):
            nodes.append({"id": f"machine:{machine_id}", "kind": "machine"})
        for test_name in sorted(self._tests):
            nodes.append({"id": f"test:{test_name}", "kind": "test"})

        for detector_id, template in sorted(self._detector_template.items()):
            edges.append({"from": f"asset:{template}", "to": f"detector:{detector_id}", "relation": "used_by"})
        for detector_id, field_name in sorted(self._detector_field.items()):
            edges.append({"from": f"detector:{detector_id}", "to": f"field:{field_name}", "relation": "produces"})
        for field_name, sites in self._field_transitions.items():
            for site in sites:
                edges.append(
                    {"from": f"field:{field_name}", "to": f"transition:{site.label}", "relation": "guards"}
                )
                edges.append(
                    {"from": f"transition:{site.label}", "to": f"machine:{site.machine}", "relation": "belongs_to"}
                )
        for test_name in sorted(self._tests):
            for machine_id in sorted(self._bundle.machines):
                if machine_id in self._tests[test_name]:
                    edges.append(
                        {"from": f"machine:{machine_id}", "to": f"test:{test_name}", "relation": "verified_by"}
                    )
        return {"nodes": nodes, "edges": edges}

    def export_json(self, path: str | Path) -> Path:
        """把依赖图导出为 JSON 文件。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.graph_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return target

    # -- 内部 ---------------------------------------------------------------

    def _build_report(self, trigger: str, detectors: list[str]) -> ImpactReport:
        """从受影响检测器集合沿依赖链收集下游影响。"""
        fields = sorted({self._detector_field[d] for d in detectors if d in self._detector_field})
        sites: list[_TransitionSite] = []
        for field_name in fields:
            sites.extend(self._field_transitions.get(field_name, []))
        transitions = sorted({site.label for site in sites})
        machines = sorted({site.machine for site in sites})
        tokens = [token for token in (*detectors, *fields, *machines) if token]
        test_cases = sorted(
            name for name, text in self._tests.items() if any(token in text for token in tokens)
        )
        suggested_rerun = [f"detector:{d}" for d in detectors] + [f"machine:{m}" for m in machines] + test_cases
        return ImpactReport(
            trigger=trigger,
            detectors=detectors,
            fields=fields,
            transitions=transitions,
            machines=machines,
            test_cases=test_cases,
            suggested_rerun=suggested_rerun,
        )

    def _scan_tests(self) -> dict[str, str]:
        """扫描项目 ``tests/`` 目录（如存在）：``相对路径 -> 文本``。

        项目没有 tests/ 目录时返回空（影响链到迁移/机器层为止）。
        """
        tests_dir = self._root / "tests"
        if not tests_dir.is_dir():
            return {}
        tests: dict[str, str] = {}
        for file_path in sorted(tests_dir.rglob("*")):
            if not file_path.is_file() or file_path.suffix.lower() not in _TEST_SUFFIXES:
                continue
            try:
                tests[file_path.relative_to(self._root).as_posix()] = file_path.read_text(encoding="utf-8")
            except OSError:
                continue
        return tests
