# 检测器调参指南

| 项 | 内容 |
|---|---|
| 状态 | 已发布（M3 评审基线） |
| 日期 | 2026-09-25 |
| 关联任务 ID | DOC-001 配套、VIS-009/011/012、TST-005/006、VER-004、§7.2 量化门槛 |
| 里程碑 | M3 |
| 来源 | 《任务与测试验收方案》§7.2、§8.1；《GitHub 调研与安全架构》§7.1、§7.5 |
| 关联文档 | `docs/authoring/script-authoring-guide.md`（参数语法）、`tools/acceptance/run_m2_checks.py` [4] |

本指南回答三个问题：阈值/稳定帧/迟滞**如何影响**检测行为与运行安全；
如何用**黄金数据集**把调参从"手感"变成可版本化的指标；改完之后如何做
**变更影响分析**与**回放差异**确认没有退化。

---

## 1. 三个旋钮：阈值 / 稳定帧 / 迟滞

### 1.1 阈值（`threshold`）——命中强度的门限

检测器每帧输出 `confidence ∈ [0,1]`（模板匹配置信度、颜色占比、变化强度等），
`threshold` 是"单帧是否命中（`present`）"的门限。

- 调低 → recall 上升、precision 下降：容易误命中。误命中的迁移会驱动状态机
  做错误决策（例如在错误界面点击）。
- 调高 → precision 上升、recall 下降：容易漏检。漏检会让状态机停在等待态，
  直到超时退出——比误命中安全，但会拖慢甚至中断业务闭环。
- 原则：**关键路径（驱动真实输入的迁移条件）宁可漏检不可误检**；
  纯观察/统计类字段可适当放宽。

### 1.2 稳定帧（`stable_frames`）——抗抖动的时间滤波

`stable_frames: N` 表示连续 N 帧单帧命中才把字段置为 `present=true`
（`vision_core.stability.StableFrameAggregator`，`enter_frames=N`）。

- N=1：响应最快，画面闪一下（弹窗、转场）就可能触发迁移；
- N=2~3：UI 按钮/闸门类推荐值（arena_lab_demo 的 `gate_button` 用 2、
  `ready_button` 用 3）；
- 数值型字段（`color_bar_ratio` 的 `value`）不受 present 稳定门限约束，
  常配 `stable_frames: 1`（见 `health_bar`），由状态机侧用阈值表达式
  （`health_ratio.value < 0.20`）消费。

代价：稳定帧数会**增加状态进入延迟**（约 N 帧检测周期）。若状态机同时配了
`timeout_seconds`，要保证 `N × 检测周期 ≪ timeout_seconds`，否则会"稳定还没
攒够，超时先到"。

### 1.3 迟滞（hysteresis）——进入与退出分开门槛

`StabilityConfig` 支持 `enter_threshold` / `exit_threshold`（约定
`exit_threshold ≤ enter_threshold`）与 `exit_frames`：进入用高标准，
退出用低标准并要求连续多帧未命中，避免置信度在阈值附近抖动导致
`present` 反复翻转（状态机来回横跳）。

| 参数 | 默认 | 建议用法 |
|---|---|---|
| `enter_frames` | 1 | 等于检测器 `stable_frames` |
| `exit_frames` | 1 | 抖动严重时调到 2~3 |
| `enter_threshold` | 0.5 | 等于检测器 `threshold` 或略高 |
| `exit_threshold` | 0.5 | 比 enter 低 0.05~0.15 |
| `confidence_window` | 5 | 输出置信度滑动均值窗口，减少轨迹里的置信度毛刺 |

### 1.4 改动前的安全检查单

- [ ] 改的是**候选版分支**，基线版本已发布（回滚有锚点）；
- [ ] 该检测器的字段被哪些迁移/状态机引用（`release_kit.impact`，见 §4）；
- [ ] 黄金数据集指标仍达 §3 门槛；
- [ ] 回放差异无 `regressed` 样本（见 §5）。

---

## 2. 黄金数据集工作流（vision_core.golden）

黄金数据集 = ArenaLab 确定性场景帧 + Oracle 自动标注真值，落盘为
`<prefix>.npz`（帧像素）+ `<prefix>.json`（期望/元信息/内容哈希）。
同参数构建两次逐字节一致；读取时重算哈希，篡改即失败。

内置场景（`apps/arena_lab`）：`happy_path`（正向主流程）、
`loading_timeout`（加载停滞负向路径）、`popup_random`（种子驱动的
弹窗/目标/掉落）。

### 2.1 构建并冻结数据集（命令行示例）

```bash
export PYTHONPATH="packages;services;apps"   # CMD 用 set，PowerShell 用 $env:

python -c "
from vision_core.golden import build_from_scenario
ds = build_from_scenario('happy_path', 42, resolution=(1280, 720),
                         fps=30.0, duration_s=10.0, dataset_version='1')
print('cases =', len(ds.cases), 'hash =', ds.hash)
ds.save('datasets/golden/happy_path-v1')
"
```

产出 `datasets/golden/happy_path-v1.npz` 与 `.json`。把两个文件连同
`hash` 一起入库版本化（TST-005：数据、标签、划分、指标、基线均版本化）。

### 2.2 校验基线指标（离线批量运行 + 指标）

```bash
python -c "
from vision_core.golden import GoldenDataset, metrics
from vision_core.offline import batch_run
from vision_core.registry import create_detector
from domain_model.parsing import load_project

ds = GoldenDataset.load('datasets/golden/happy_path-v1')
detectors = [create_detector(d) for d in load_project('examples/arena_lab_demo').detectors.values()]
results = batch_run(ds, detectors)
m = metrics([r.results for r in results], [c.expected for c in ds.cases])
print('precision =', m['presence']['precision'], 'recall =', m['presence']['recall'])
print('ratio MAE =', m['ratio']['mae'], ' p95 =', m['ratio']['p95_abs_error'])
print('timing p95_ms =', m['timing']['p95_ms'])
"
```

### 2.3 调参迭代闭环

1. 改检测器 YAML（阈值/ROI/stable_frames）；
2. 重跑 §2.2 命令，对照 §3 门槛与旧基线数字；
3. 不达标回到 1；达标则提交，并把新指标写入发布测试摘要
   （`release_kit.manifest.TestSummary`）。

---

## 3. 指标门槛（发布质量闸门 §7.2）

`vision_core.golden.metrics` 返回四组：`presence`（tp/fp/fn/tn 与
precision/recall/accuracy）、`localization`（中心误差 mean/p95、IoU mean）、
`ratio`（MAE、P95 绝对误差）、`timing`（p50/p95/mean 毫秒）。

| 指标 | 门槛 | 适用范围 |
|---|---|---|
| precision、recall | **≥ 0.98** | **关键字段**（驱动真实输入的迁移条件，如 `ready`、`manual_gate`） |
| precision、recall | **≥ 0.95** | 非关键字段（观察/统计类） |
| ratio MAE | **≤ 0.03** | 数值型字段（血条/资源条比例等） |
| localization | 报告并留痕 | 关键模板建议 center_error_p95 ≤ 2% 帧宽（指导值） |

口径说明：门槛按"字段分组"评定——把该字段的 (present, value) 列从全体样本
抽出统计（`metrics` 的总体 presence 是全字段汇总，逐字段门槛用按字段切片的
方式计算，见 §2.2 的循环改法）。任意关键字段任一指标不达标，发布闸门阻断。

---

## 4. 变更影响分析（release_kit.impact）

改一个模板/检测器之前，先弄清它会波及什么：模板 → 检测器 → 感知字段 →
迁移 → 状态机 → 项目测试用例。

```bash
python -c "
from release_kit.impact import ImpactGraph

g = ImpactGraph('examples/arena_lab_demo')
r = g.affected_by_asset('assets/templates/ready.png')     # 或 g.affected_by_detector('ready_button')
print(r.to_dict())
g.export_json('impact_ready.json')                        # 完整依赖图存档
"
```

`ImpactReport` 字段：`detectors`（受影响检测器）、`fields`（感知字段）、
`transitions`（`机器/状态#序号`）、`machines`、`test_cases`（项目 tests/ 下
文本引用了受影响对象的用例）、`suggested_rerun`（建议重跑集）。

流程要求：影响报告随候选版归档；`suggested_rerun` 中列出的检测器与用例
必须进入候选版回归（`tools/release_packager/regression_pack.py`）。

---

## 5. 回放差异确认（offline diff + trace diff）

### 5.1 检测器层：黄金集差异报告

```bash
python -c "
import json
from vision_core.golden import GoldenDataset
from vision_core.offline import batch_run, diff_reports
from vision_core.registry import create_detector
from domain_model.parsing import load_project

ds = GoldenDataset.load('datasets/golden/happy_path-v1')
det = load_project('examples/arena_lab_demo').detectors.values()
new = batch_run(ds, [create_detector(d) for d in det])

def dump(results, path):   # 结果留档（当前版本跑一遍，改动前执行）
    json.dump([{f: {'present': r.present, 'value': r.value,
                   'confidence': r.confidence} for f, r in c.results.items()}
               for c in results], open(path, 'w'))
def load(path):            # 还原基线结果
    return [type('C', (), {'case_index': i, 'results': {
        f: type('R', (), {'present': r['present'], 'value': r['value'],
                          'confidence': r['confidence'], 'bbox': None,
                          'error': None})() for f, r in c.items()}})()
            for i, c in enumerate(json.load(open(path)))]

# base.jsonl 为旧版本在同一数据集上的留档（改动前先 dump）
report = diff_reports(load('base.jsonl'), new)
print('regressed cases =', report.regressed_cases)
print('metric changes  =', report.metric_changes)
"
```

`SampleDiff.kind` 取值：`present_flip / value / confidence / bbox / error`；
`regressed=True` 表示退化（命中 True→False 或 error 新增）。
**任何 regressed 样本都必须解释或修复后才能进候选版。**

### 5.2 会话层：轨迹 diff（回放回归套件，TRC-007）

同版本 + 同轨迹输入 + 同随机种子必须得到同一决策结果（架构不变量 6，
AC-P0-08）。会话级 diff 目前通过**回放回归套件**执行——它内置"固定感知回放 +
原始帧回放 + 差异报告（定位首个分歧事件与受影响测试）"：

```bash
python -m pytest tests/replay -q          # TRC-005/006/007：回放回归 + 差异报告
```

手工比对两条轨迹时，读取 JSONL 后按 `trace_format.events.compute_event_hash`
逐事件对齐，首个哈希不一致的事件即分歧点：

```bash
python -c "
import json
from trace_format.events import compute_event_hash

def load(p):
    return [json.loads(l) for l in open(p, encoding='utf-8') if l.strip()]
a, b = load('traces/run-a.jsonl'), load('traces/run-b.jsonl')
for i, (ea, eb) in enumerate(zip(a, b)):
    if compute_event_hash(ea) != compute_event_hash(eb):
        print('首个分歧事件 #', i, ea.get('type'), 'vs', eb.get('type')); break
else:
    print('前', min(len(a), len(b)), '个事件哈希一致；长度', len(a), 'vs', len(b))
"
```

`tools/trace_diff/` 为预留的独立 CLI 位置（当前由回放回归套件与本节脚本
覆盖同一语义）。若检测器改动导致确定性被破坏，上述任一手段都会定位到首个
分歧事件——这是候选版前必须消除的问题（AC-P0-08）。

### 5.3 调参变更的完整收尾清单

1. §2 指标达标（关键 ≥0.98 / 非关键 ≥0.95 / MAE ≤0.03）；
2. §4 影响报告归档，`suggested_rerun` 进入回归；
3. §5.1 差异无 regressed；§5.2 确定性保持；
4. 更新 `assets/assets.yaml` 的模板 `version` 与 `sha256`（如改了模板图）；
5. 发布并留痕：测试摘要 + 影响报告 + 差异报告随候选版归档
   （见 `docs/operations/runbook.md` §9）。
