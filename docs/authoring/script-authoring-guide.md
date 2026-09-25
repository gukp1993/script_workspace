# 脚本/检测器作者指南（DOC-001）

| 项 | 内容 |
|---|---|
| 状态 | 已发布（M3 评审基线） |
| 日期 | 2026-09-25 |
| 关联任务 ID | DOC-001（主责）、DOM-002/003/005/007/009、VIS-001/012、FSM-007/008/010 |
| 里程碑 | M3 |
| 来源 | 《任务与测试验收方案》§4 E02/E06/E07；《GitHub 调研与安全架构》§4、§5、§6.2 |
| 关联文档 | `docs/safety/safety-boundaries.md`、`docs/authoring/detector-tuning.md`、`schemas/*.schema.json` |

本指南面向**脚本/检测器作者**（含非开发人员）：如何组织一个项目包、如何编写
检测器与状态机、静态检查会拦下什么、以及能力授权模型如何"默认拒绝"。
所有示例均取自仓库内真实可跑的示例项目 `examples/arena_lab_demo/`，
写完后可用 `python -m domain_model.validate examples/arena_lab_demo` 自检（应通过）。

---

## 1. 项目包结构

一个"项目"不是一个 Python 文件，而是一个**声明式目录包**（任务文档附录 B）。
固定结构如下（与 `examples/arena_lab_demo/` 一致）：

```text
my_project/
├── project.yaml          # 项目清单：schema_version / name / description / notes
├── targets/              # 目标档案（每目标一个 yaml）
│   └── arena-lab.yaml
├── policies/             # 运行策略（模式、预算、失焦行为）
│   └── default.yaml
├── detectors/            # 检测器定义（五类，见 §3）
│   ├── ready_button.yaml
│   ├── gate_button.yaml
│   └── health_bar.yaml
├── machines/             # 状态机定义（*.machine.yaml，见 §4）
│   └── main.machine.yaml
├── assets/
│   ├── assets.yaml       # 资产清单：asset_id / path / kind / version / sha256
│   └── templates/        # 模板图片（.png）
└── calibrations/         # 分辨率+DPI 标定
    └── 1920x1080-100.json
```

要点：

- **ID 约定**：`project.yaml` 的 `name`、`target_id`、`policy_id`、`detector_id`、
  `machine_id` 必须匹配 `^[a-z][a-z0-9_-]*$`；感知字段名 `field_name`
  匹配 `^[a-z][a-z0-9_]*$`。
- **模板路径**：`template` 必须是项目内相对路径，不得以 `/`、`\` 或盘符开头，
  不得包含 `..`（越界会被 Schema 与静态检查双重拒绝）。
- **资产清单是强制的**：每个模板必须在 `assets/assets.yaml` 登记
  `asset_id / path / kind / version / sha256`；SHA-256 与实际文件不一致
  会在静态检查中报 `reference_missing_asset`。
- **校验入口**：

  ```bash
  python -m domain_model.validate examples/arena_lab_demo   # 正例，应通过
  python -m domain_model.validate examples/protected_online_demo  # 反例，应拒绝并给出规则 ID
  ```

`project.yaml` 最小示例（摘自 arena_lab_demo）：

```yaml
schema_version: 1
name: arena_lab_demo
description: ArenaLab 本地测试模拟器示例项目（观察 + Shadow Mode）
notes: 全部感知与状态机仅针对本地 ArenaLab.exe；真实输入保持关闭。
```

目标档案（`targets/arena-lab.yaml`）：

```yaml
schema_version: 1
target_id: arena-lab
executable: ArenaLab.exe
title_regex: '^ArenaLab - Training$'
window_class: null
protected_online: false          # 受保护在线目标必须显式 true（触发硬锁规则集）
allowed_display_modes:
  - windowed
  - borderless
```

运行策略（`policies/default.yaml`）：

```yaml
schema_version: 1
policy_id: default
mode: shadow                     # observe | shadow | dry_run | real_input
require_manual_start: true
max_runtime_minutes: 20
max_actions_per_minute: 120
max_total_actions: 600
on_focus_lost: stop
unattended_schedule: disabled    # real_input 模式下必须 disabled（默认拒绝）
```

---

## 2. 感知字段：检测器如何变成状态机的输入

每个检测器把一帧画面归约为一个**语义字段**（`field_name`）。运行时的感知快照
按字段聚合 `present / confidence / value / bbox`，状态机条件表达式只能引用
这些字段。字段引用有三种属性形式：

| 写法 | 含义 | 缺失/未命中时的取值 |
|---|---|---|
| `field.present`（或裸写 `field`） | 本帧（含稳定帧聚合后）是否命中 | `False` |
| `field.value` | 数值输出（比例/数值） | `0` |
| `field.confidence` | 置信度 0~1 | `0` |
| `field.changed` | 相邻帧画面是否变化（变化检测语义） | `False` |

安全默认：字段缺失或未命中一律按 `0 / False` 处理并记录，**不会抛异常、
不会中断状态机**——作者无须（也无法）在条件里写"字段不存在"的分支。

---

## 3. 五类检测器：参数与真实示例

公共参数（`schemas/detector.schema.json`，五类通用，`additionalProperties: false`）：

| 参数 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `schema_version` | int | 恒为 `1` | Schema 版本 |
| `detector_id` | string | `^[a-z][a-z0-9_-]*$` | 项目内唯一 |
| `type` | enum | 见下表 | 算法类型 |
| `roi` | [x,y,w,h] | 各分量 0~1，`x+w≤1`、`y+h≤1` | **归一化** ROI（相对帧宽高），跨分辨率成立 |
| `threshold` | number | 0~1 | 判定阈值（置信度门限或比例门限，按类型语义） |
| `stable_frames` | int | ≥1 | 连续稳定帧数（`StableFrameAggregator` 的进入帧数） |
| `field_name` | string | `^[a-z][a-z0-9_]*$` | 输出语义字段名（状态机 `when` 引用它） |
| `template` | string | 仅 `template_match` 允许 | 模板资产相对路径 |

五类检测器与 `threshold` 语义：

| type | threshold 语义 | value 输出 | 典型用途 |
|---|---|---|---|
| `template_match` | 模板匹配得分门限 | 匹配得分 | 固定 UI 按钮/图标（Ready、确认框） |
| `color_bar_ratio` | 有效比例门限（≥ 记命中） | 血条/资源条占空比 0~1 | 血条、能量条 |
| `color_region` | 区域内目标色占比门限 | 占比 0~1 | 状态区域染色（红/绿提示区） |
| `change_stability` | 变化判定门限 | 变化强度 | 加载条推进、画面切换检测 |
| `ocr_roi` | 识别置信度门限 | 识别文本 | ROI 内数字/短文本 |

### 3.1 模板匹配（arena_lab_demo 真实配置）

`detectors/ready_button.yaml`：

```yaml
schema_version: 1
detector_id: ready_button
type: template_match
roi: [0.40, 0.78, 0.20, 0.12]   # 帧宽高的 40%~60% 横向、78%~90% 纵向
threshold: 0.91                  # 匹配分 ≥0.91 记单帧命中
stable_frames: 3                 # 连续 3 帧命中才输出 ready.present=true
field_name: ready
template: assets/templates/ready.png
```

`detectors/gate_button.yaml`（人工闸门确认按钮）：

```yaml
schema_version: 1
detector_id: gate_button
type: template_match
roi: [0.42, 0.55, 0.16, 0.10]
threshold: 0.88
stable_frames: 2
field_name: manual_gate
template: assets/templates/gate.png
```

配套的 `assets/assets.yaml`（模板必须登记且哈希一致）：

```yaml
schema_version: 1
assets:
  - asset_id: asset-ready-button
    path: assets/templates/ready.png
    kind: template
    version: 1
    sha256: ff042736c954103f83bafde1090ed6d3fbc639e5102925f7255ac0e31ef08841
  - asset_id: asset-gate-button
    path: assets/templates/gate.png
    kind: template
    version: 1
    sha256: e84c16fc4f6f6c686e1934860184d3e4ba52957d5dea395b92fa00096a51a996
```

### 3.2 颜色条比例（数值型字段）

`detectors/health_bar.yaml`：

```yaml
schema_version: 1
detector_id: health_bar
type: color_bar_ratio
roi: [0.04, 0.04, 0.22, 0.025]
threshold: 0.5
stable_frames: 1                 # 数值型字段可只稳定 1 帧（状态机用 value 而非 present）
field_name: health_ratio
```

状态机侧消费数值：`when: "health_ratio.value < 0.20"`（见 §4.3）。
调参方法（阈值/稳定帧/迟滞的影响与黄金数据集验证）见
`docs/authoring/detector-tuning.md`。

---

## 4. 状态机 DSL 语法

文件放在 `machines/*.machine.yaml`（约定后缀 `.machine.yaml`），
Schema 见 `schemas/machine.schema.json`。

### 4.1 顶层结构

```yaml
schema_version: 1
machine_id: main
initial: idle
states:
  <状态名>:
    transitions:                 # 出边列表，按声明顺序求值
      - when: "<条件表达式>"
        to: <目标状态>
        on_timeout_to: <超时目标状态>   # 可选：该状态超时后的去处
    entry:                       # 可选：进入动作序列（见 §4.4）
      - kind: ...
    exit:                        # 可选：退出动作序列
      - kind: ...
    timeout_seconds: 30          # 可选：状态超时（>0；非终态的退出手段之一）
    terminal: true               # 可选：终态不要求出边
```

### 4.2 条件表达式：白名单运算符表

`when` 字符串由**白名单递归下降解析器**解析（`domain_model.dsl`）。
白名单外的任何字符、字符串字面量、函数调用都会被拒绝（`condition_parse_error`）。

| 运算符/语法元素 | 形式 | 优先级（低→高） | 说明 |
|---|---|---|---|
| 逻辑或 | `or` | 1 | 关键字（ident），大小写不敏感 |
| 逻辑与 | `and` | 2 | 同上 |
| 逻辑非 | `not` | 3 | 同上 |
| 比较 | `<` `<=` `>` `>=` `==` `!=` | 4 | 数值比较；bool 与数值 `==` 恒 False（防误写） |
| 加减 | `+` `-` | 5 | 数值运算 |
| 乘除 | `*` `/` | 6 | 除零安全归零（不抛异常） |
| 一元负号 | `-x` | 7 | 取负 |
| 括号 | `( … )` | 分组 | 显式分组，建议在混合 and/or 时必写 |
| 数字字面量 | `3` `0.20` | — | 十进制；小数点后必须有数字 |
| 字段引用 | `field` / `field.attr` | — | attr ∈ `present`（缺省）/ `value` / `confidence` / `changed` |

**明确不允许**：字符串字面量（`"..."` / `'...'` 直接语法错误）、函数调用、
取模、位运算、赋值、对 `bbox` 等非语义属性的引用。

推荐写法示例（均可在示例项目中找到同款）：

```yaml
when: "ready.present"                  # 裸字段 = present
when: "not ready.present"
when: "health_ratio.value < 0.20"
when: "ready.present and manual_gate.present"
when: "health_ratio.value < 0.20 or (not ready.present)"   # 混合逻辑加括号
```

### 4.3 完整示例（arena_lab_demo 的 main.machine.yaml）

```yaml
schema_version: 1
machine_id: main
initial: idle
states:
  idle:
    transitions:
      - when: "ready.present"
        to: awaiting_manual_gate

  awaiting_manual_gate:
    timeout_seconds: 30
    entry:
      - kind: manual_gate
        message: "确认开始本地测试"
    transitions:
      - when: "manual_gate.present"
        to: exercise
        on_timeout_to: stopped

  exercise:
    transitions:
      - when: "health_ratio.value < 0.20"
        to: stopped

  stopped:
    terminal: true
```

阅读要点：

- 迁移只允许引用**本项目 detectors 已定义的感知字段**
  （`ready` / `manual_gate` / `health_ratio`），引用未定义字段报
  `reference_missing_field`；
- 非终态必须有出路：至少一条迁移，或 `on_timeout_to`，或状态级
  `timeout_seconds`——否则 `state_no_exit` 阻断编译（AC-P0-10）；
- 迁移按声明顺序求值，先声明者优先。

### 4.4 动作：manual_gate / wait_until / assert_after / 有界重试

entry/exit 动作是受控的类型化占位（不是自由代码）。kind 与能力映射：

| kind | 类别 | 需要的能力 |
|---|---|---|
| `press_key` / `key_down` / `key_up` | 真实键盘 | `input.key`（受限） |
| `click` / `move` | 真实鼠标 | `input.mouse`（受限） |
| `wheel` | 真实滚轮 | `input.wheel`（受限） |
| `wait` | 运行控制 | 无 |
| `manual_gate` | 运行控制（人工闸门） | 无 |
| `assert_after` / `wait_until` | 断言 | 无 |
| `retry` / `on_error_to` | 状态配置（编译期消费） | 无 |

**manual_gate（人工闸门）**：状态进入即暂停，等待用户在工作台确认后才继续；
`real_input` 模式启动前也必须通过一次人工闸门（控制面 409
`manual_gate_required`）。

```yaml
entry:
  - kind: manual_gate
    message: "确认开始本地测试"
```

**wait_until（阻塞式等待断言）**：进入状态后等待条件成立；超时走 `to` 指定的
恢复/放弃路径。

```yaml
entry:
  - kind: wait_until
    when: "gate.present"
    timeout_seconds: 5
    to: gaveup
```

**assert_after（动作后断言）**：动作执行后校验条件；不成立按 `to` 路由。

```yaml
entry:
  - kind: assert_after
    when: "ready.present"
    timeout_seconds: 2
    to: recovery
```

**有界重试（bounded retry）**：`retry` 是状态配置动作（编译期提取为
`RetrySpec`，不进入运行期动作表）。`max_attempts` 必填——缺失即"无上限"，
静态检查以 `infinite_retry` 阻断（error）。

```yaml
entry:
  - kind: retry
    max_attempts: 3          # 必填正整数；缺失 -> infinite_retry 阻断
    backoff: exponential     # fixed | exponential（默认 fixed）
    base_ms: 200             # 基础退避毫秒（默认 100）
    max_ms: 5000             # 退避上限毫秒（默认 30000）
  - kind: on_error_to        # 异常路由目标（与 retry 同属状态配置）
    to: failed
```

安全约束（重复强调）：随机延时只用于**可靠性退避**，不允许用作拟人化；
`real_input` 与无人值守调度互斥（`real_input_no_unattended`）。

---

## 5. 静态检查规则表

`python -m domain_model validate <项目>`（以及发布流水线的质量闸门）会运行
`domain_model.static_analysis.analyze`。`severity=error` 阻断编译与发布；
`warning` 只提示。规则速查（修复提示与实现一一对应）：

| rule_id | 级别 | 含义 | 修复提示 |
|---|---|---|---|
| `state_no_exit` | error | 非终态无迁移、无 `on_timeout_to`、无 `timeout_seconds`（AC-P0-10） | 给该状态补迁移/超时出口，或标记 `terminal: true` |
| `state_unreachable` | error | 状态从 initial 不可达 | 删除死状态，或补一条能到达它的迁移 |
| `transition_conflict` | warning | 同状态两个迁移静态可判定恒可同时为真 | 收窄 `when` 条件，或调整声明顺序并确认优先级是有意的 |
| `reference_missing_field` | error | `when` 引用了未定义的感知字段 | 在 `detectors/` 定义该 `field_name`，或改正拼写 |
| `reference_missing_asset` | error | 检测器模板文件缺失 / 未登记清单 / sha256 与文件不一致 | 补文件并在 `assets/assets.yaml` 登记（`asset_id/path/kind/version/sha256`），或用真实 sha256 更新清单 |
| `action_unauthorized` | error | 动作 kind 未注册到能力表（DOM-005） | 只使用 §4.4 列出的 kind |
| `protected_online_unattended` | error | 受保护在线目标 + `unattended_schedule: enabled`（硬锁） | `unattended_schedule: disabled` |
| `protected_online_no_real_input` | error | 受保护在线目标 + `mode: real_input`（硬锁） | 受保护在线目标只允许 `observe`/`shadow`/`dry_run` |
| `real_input_no_unattended` | error | `real_input` 模式 + 无人值守（默认拒绝） | 真实输入必须有人在场：`unattended_schedule: disabled` |
| `infinite_retry` | error | `retry` 动作没有 `max_attempts` 上限 | 补 `max_attempts`（≥1 的正整数） |
| `state_target_missing` | error | 迁移/超时/断言/异常出口的目标状态不存在 | 修正 `to` / `on_timeout_to` 拼写或补定义该状态 |
| `condition_parse_error` | error | 条件表达式无法被白名单解析器解析 | 对照 §4.2 运算符表改写；不要用字符串/函数 |
| `assertion_invalid` | error | 断言动作缺 `when` / `timeout_seconds` 不合法 | 补 `when`；时长用正数或 `"30s"/"250ms"/"2m"` 字面量 |
| `initial_state_missing` | error | `initial` 指向的状态未定义 | 修正 `initial` 或补定义 |

反例验证：`examples/protected_online_demo` 应被拒绝且输出
`protected_online_no_real_input`——这是 M0 起每个验收门都在跑的硬锁回归。

---

## 6. 能力清单与默认拒绝

脚本运行在**能力授权模型**下（`domain_model.capabilities`，DOM-005）：

已注册能力（当前全集，只增不改语义）：

| 能力 ID | 含义 |
|---|---|
| `perception.read` | 读取感知快照 |
| `perception.record` | 记录/标注感知数据 |
| `trace.write` | 写入运行轨迹 |
| `machine.advance` | 推进状态机（不含真实输入） |
| `input.key` | 真实键盘输入（**受限**） |
| `input.mouse` | 真实鼠标输入（**受限**） |
| `input.wheel` | 真实滚轮输入（**受限**） |

三条铁律：

1. **默认拒绝**：能力必须已注册**且**被显式列入授权集合（`granted`），
   二者缺一即拒绝——未注册的能力不存在"申请"通道。
2. **输入类二次确认**：`input.*` 还要求目标 `real_input_allowed`
   （`authorize_input` 封装，调用方不得绕过）。
3. **没有任意代码通道**：YAML/JSON 声明式脚本之外，不存在 exec/eval/shell/
   插件入口（SEC-001）；M0-M3 不提供 Python/Shell/DLL 扩展点。

---

## 7. 禁止能力清单

脚本/检测器/项目包**不得**要求、变通或模拟以下能力（完整清单与威胁模型见
`docs/safety/safety-boundaries.md` 与 `docs/safety/threat-model.md`）：

进程隐藏、驱动隐藏、反作弊探测、内存读写、进程注入、封包截获/修改、
硬件伪装、拟人化逃避检测、多账号集群、后台多开。

导入预检（`release_kit.transfer`）与静态守卫会在包级别拦截可执行载荷
（`.py/.pyd/.dll/.exe/.bat` 等）、路径穿越与超限包；受保护在线目标被硬锁在
观察/Shadow/Dry Run。作者自检顺序建议：

```bash
python -m domain_model validate <项目目录>     # Schema + 静态规则
python -m pytest tests/unit/test_dsl_static_analysis.py -q   # 规则语义回归
```

发布前的完整闸门见 `docs/operations/runbook.md` §9 与
`python tools/release_packager/regression_pack.py --help`。
