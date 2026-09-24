# Windows 前台视觉自动化工作台

## GitHub 方案调研、安全边界与落地架构建议

| 项 | 内容 |
|---|---|
| 文档日期 | 2026-09-25 |
| 基线文档 | 《Windows 前台游戏脚本工作台——需求描述》v0.1 |
| 建议版本 | v0.2 方案讨论稿 |
| 目标平台 | Windows 10/11 |
| 推荐产品定位 | 可观察、可回放、可测试的前台视觉自动化工作台 |
| 安全声明 | 本文不提供反作弊绕过、进程隐藏、驱动隐藏、内存读写、进程注入、封包操作或“拟人化逃避检测”方案 |

---

## 0. 结论先行

你的 v0.1 方向总体是对的：

- 用“视觉感知 → 条件决策 → 前台执行”替代纯键鼠录制；
- 用状态机组织长流程；
- 把识别预览、日志、急停、运行上限列为平台能力；
- 明确不注入、不读写内存、不碰封包、不做后台多开；
- 采用 Vue3 + Python 本地服务，与现有技术栈保持一致。

但当前文档把两个互相冲突的目标放在了一起：

1. 做一个通用、可调试的视觉自动化工作台；
2. 面向受保护的在线游戏执行无人值守自动刷怪，并希望加入“反作弊策略”。

这两个目标必须拆开。对在线游戏而言，前台运行、不注入、不读内存，只能减少客户端侵入面，不能把自动化变成安全或合规行为。服务端仍可根据持续在线、操作周期、路径重复、收益模式和异常一致性识别自动化。工程上不存在一个可以承诺“不封号”的客户端方案。

因此，建议把产品定位改为：

> **Windows 前台视觉自动化与回归测试工作台**：支持屏幕采集、视觉识别、状态机、前台输入、调试回放和安全策略；完整执行能力优先用于本地模拟器、离线软件、测试环境和明确允许自动化的目标。受保护在线游戏配置默认只开放观察、标注、回放与人工确认能力。

### 推荐技术决策

| 决策项 | 推荐结论 |
|---|---|
| 截屏 | Windows Graphics Capture / DXGI Desktop Duplication 为主，DXcam 封装；python-mss 作为兼容回退 |
| 视觉 | OpenCV 为第一优先；OCR 仅在裁剪区域按事件触发；YOLO 延后 |
| 决策 | 先用声明式有限状态机；Python `transitions` 可作为运行时实现；复杂到一定程度再评估行为树 |
| 输入 | 自建薄层 `Input Broker` 封装 Win32 `SendInput`；PyAutoGUI / PyDirectInput 仅做原型或兼容适配，不作为核心引擎 |
| 热键 | Win32 全局热键或 AHK v2 辅助；AHK 不承担主要业务运行时 |
| 脚本 | YAML/JSON 声明式脚本，编译为状态机；默认不允许任意 Python 代码 |
| UI | Vue3 保留；MVP 可用 pywebview 承载，后续需要签名、自动更新和更强桌面能力时再换 Tauri 壳 |
| 可视化编辑 | 先做步骤列表 + 属性面板 + 状态图；成熟后用 Rete.js 做节点编辑器 |
| 第一个目标 | 不是 PoE、D4 或 WoW，而是本地可控的 `ArenaLab` 测试模拟器 |
| “反作弊” | 改名为“安全与合规策略”；不做检测绕过，只做目标白名单、前台锁、限额、急停、审计和默认拒绝 |

---

## 1. 对 v0.1 的锐评

### 1.1 做对了什么

#### 1. “前台视觉闭环”比传统录制宏高一个层级

传统按键精灵只记录“何时按什么”，环境稍有变化就失效。你定义的闭环把动作建立在当前画面条件上，工程上更接近视觉 RPA、黑盒 UI 测试和机器人控制，而不是宏录制器。

#### 2. 识别预览是刚需，不是锦上添花

没有检测框、置信度、ROI、模板命中热力图和状态迁移记录，视觉脚本几乎不可调试。把实时预览列为必需能力是正确的。

#### 3. 状态机比“长脚本 + 大量 if/else”更适合

状态机天然适合表达等待、重试、超时、错误恢复和流程复位，也更容易做静态检查、回放和覆盖率统计。

#### 4. 已主动写出不可跨越的边界

不注入、不读内存、不碰封包、不做驱动隐藏和检测绕过，是合理的产品红线。这个边界要从“文档约定”升级为“代码级能力约束”。

### 1.2 当前最危险的五个问题

#### 问题一：产品名和真实能力不一致

你写的是“类似按键精灵”，实际需要的是：

- 视觉资产管理；
- 标定系统；
- 感知流水线；
- 状态机运行时；
- 输入安全代理；
- 事件溯源与回放；
- 测试与版本治理。

继续按“宏工具”思路开发，最后会得到一个难以复现、难以测试、脚本互相污染的 Python 脚本集合。

#### 问题二：“随机延时用于可靠性”容易演化为“拟人化”

可靠性真正需要的是：

- 等待可观察条件，而不是盲等；
- 有上限的超时；
- 指数或固定退避；
- 帧稳定判定；
- 操作前后断言。

随机化不应该成为脚本的默认概念，更不能宣传为降低检测概率。建议从 DSL 中删除通用 `humanize`、`random_path`、`random_click` 一类能力，只保留有边界的调度抖动，并在产品文档中明确它只用于避免多个内部任务争抢资源。

#### 问题三：定时启动、自动重连和长时间自动恢复会把工具推向无人值守 Bot

普通桌面自动化可以有调度器，但在线游戏配置中应默认：

- 禁止无人值守定时启动；
- 掉线后停止并通知，而不是自动重连；
- 连续异常停止，而不是无限重试；
- 每次运行必须人工确认目标窗口；
- 运行时间和动作预算必须有硬上限。

#### 问题四：把 PyAutoGUI / PyDirectInput 当执行层主力不够稳

它们适合原型，不适合作为平台边界。核心引擎应拥有自己的强类型输入意图、窗口校验、按键状态跟踪、焦点丢失保护和按键释放逻辑，底层再调用 `SendInput`。这样才能测试“将要执行什么”，并在 Dry Run 中完全替换真实输入。

#### 问题五：第一个目标直接选真实在线游戏，测试路线倒置

没有本地可控环境时，你无法稳定复现：

- 分辨率与 DPI 变化；
- 加载画面；
- 帧率骤降；
- 焦点丢失；
- 遮挡；
- 弹窗；
- 误识别；
- 输入延迟；
- 按键卡住。

先造一个小型本地模拟器，比直接在 PoE 或 D4 上“边跑边调”更快，也更安全。

---

## 2. GitHub 项目调研与取舍

> 原则：参考通用视觉自动化、桌面自动化、截屏、状态机和节点编辑器项目；不以游戏专用 Bot、注入器、内存工具或反作弊绕过项目为参考。

### 2.1 最值得看的工作台与交互形态

| 项目 | GitHub | 可借鉴点 | 不建议直接照搬的部分 | 结论 |
|---|---|---|---|---|
| OculiX | https://github.com/oculix-org/Oculix | “截图即脚本资产”、在编辑动作时直接框选目标、图片库、识别调试、视觉脚本包 | JVM/Jython 运行时较重；不必复制其跨平台包袱 | **最值得参考的产品交互**，借 UX 与资产模型，不照搬内核 |
| SikuliX1 | https://github.com/oculix-org/SikuliX1 | 基于 OpenCV 的屏幕元素定位、图像驱动脚本范式、成熟案例 | 老架构与 Java 生态；新项目应优先看 OculiX | 用作历史设计参考 |
| Actiona | https://github.com/Jmgr/actiona | 动作目录、步骤列表、条件与循环、参数面板、运行日志、可视化流程 | Qt/C++ 老架构、脚本自由度高、许可证需评估 | **最适合参考步骤式编辑器** |
| Pulover's Macro Creator | https://github.com/Pulover/Pulovers-Macro-Creator | 宏录制、动作列表、AHK 导出、面向非开发者的操作流 | 适配目标较老；录制驱动不应成为本项目核心 | 只借交互，不借技术架构 |
| AutoHotkey v2 | https://github.com/AutoHotkey/AutoHotkey | 全局热键、窗口管理、键鼠原型、快速验证 | 不适合承载视觉管线、状态追踪和大规模测试 | 可做可选适配器或开发辅助 |

### 2.2 截屏与视觉基础设施

| 项目 | GitHub | 适用位置 | 建议 |
|---|---|---|---|
| DXcam | https://github.com/ra1nty/DXcam | Windows 高帧率采集，支持 DXGI/WGC 路径 | 作为 Windows 主采集适配器；封装接口，不让业务直接依赖 |
| python-mss | https://github.com/BoboTiG/python-mss | 简单、跨平台、与 NumPy/OpenCV 容易衔接 | 作为兼容回退和测试基线，不作为唯一高性能路径 |
| OpenCV | https://github.com/opencv/opencv | 模板匹配、颜色检测、形态学、特征匹配、几何变换 | M1-M3 的视觉核心 |
| PaddleOCR | https://github.com/PaddlePaddle/PaddleOCR | 文本区域识别、多语言 OCR | 只在小 ROI、低频事件中调用，避免每帧全屏 OCR |
| Ultralytics | https://github.com/ultralytics/ultralytics | YOLO 目标检测、数据训练和部署 | 延后到有数据集、基线指标和明确必要性之后；先不要用模型掩盖规则设计问题 |

### 2.3 决策、编辑与桌面壳

| 项目 | GitHub | 适用位置 | 建议 |
|---|---|---|---|
| transitions | https://github.com/pytransitions/transitions | 有限状态机、层次状态机、异步状态机 | M1/M2 首选，接口简单、易测 |
| py_trees | https://github.com/splintered-reality/py_trees | 行为树、黑板、复杂决策组合 | 暂缓；只有状态机出现大量并发子行为后再评估 |
| Rete.js | https://github.com/retejs/rete | Vue/TypeScript 节点编辑器、数据流和控制流图 | M3 以后使用；先稳定 DSL 与执行语义，再画节点 |
| pywebview | https://github.com/r0x0r/pywebview | Vue 页面嵌入原生窗口、Python-JS 桥接、MVP 打包 | 最符合当前 Vue3 + Python 技术栈的快速方案 |
| Tauri | https://github.com/tauri-apps/tauri | 桌面打包、系统 WebView、托盘、通知、更新、签名能力 | 产品化阶段候选；Python 运行时作为 sidecar |

### 2.4 输入层项目的正确定位

| 项目 | GitHub | 适合 | 不适合 |
|---|---|---|---|
| PyAutoGUI | https://github.com/asweigart/pyautogui | 快速原型、普通桌面应用测试 | 作为游戏/高可靠自动化平台的安全边界 |
| PyDirectInput | https://github.com/learncodebygaming/pydirectinput | 在部分 DirectX 程序上补充 PyAutoGUI 输入兼容性 | 独立运行时、窗口策略、状态追踪、完整测试体系 |

推荐的输入栈不是“换一个库就完成”，而是：

```text
Decision Engine
    ↓ 产生 InputIntent，不直接按键
Policy Guard
    ↓ 检查目标、焦点、预算、运行模式
Input Broker
    ↓ 跟踪按下/释放、批次、超时、取消
Win32 SendInput Adapter
    ↓
Windows
```

---

## 3. 推荐总体架构

### 3.1 逻辑架构

```text
┌──────────────────────────────────────────────────────────────┐
│ Vue3 Workbench                                               │
│ 项目 / 资产库 / 步骤编辑 / 状态图 / 调试器 / 测试 / 运行记录 │
└───────────────────────┬──────────────────────────────────────┘
                        │ 本地桥接或受保护的 loopback API
┌───────────────────────▼──────────────────────────────────────┐
│ Control Plane                                                │
│ 项目加载、版本、会话、权限、配置校验、WebSocket 事件流       │
└──────────┬──────────────────────────┬─────────────────────────┘
           │                          │
┌──────────▼──────────┐    ┌──────────▼────────────────────────┐
│ Runtime Coordinator │    │ Asset / Trace / Test Store         │
│ 时钟、取消、预算     │    │ 模板、ROI、标定、运行轨迹、快照   │
└──────┬──────────────┘    └───────────────────────────────────┘
       │
┌──────▼───────────────────────────────────────────────────────┐
│ Policy Guard                                                 │
│ 目标白名单、前台校验、模式约束、动作配额、急停、能力检查     │
└──────┬───────────────────────────────────────────────────────┘
       │
       ├─────────────┬───────────────────┬─────────────────────┐
       │             │                   │                     │
┌──────▼──────┐ ┌────▼─────────┐  ┌──────▼──────────┐  ┌──────▼───────┐
│ Capture     │ │ Vision Graph │  │ State Machine   │  │ Input Broker │
│ WGC/DXGI    │ │ OpenCV/OCR   │  │ Intent only     │  │ SendInput    │
└──────┬──────┘ └────┬─────────┘  └──────┬──────────┘  └──────┬───────┘
       └──────────────► Perception Snapshot ───────────► Action Intent
```

### 3.2 建议的进程划分

#### 进程 A：桌面壳与 UI

- 展示项目、图像、状态图、运行日志；
- 不直接发送输入；
- 崩溃时不能留下按键按下状态；
- 默认普通用户权限运行。

#### 进程 B：运行时引擎

- 截屏、视觉、状态机、测试、回放；
- 产生抽象的 `InputIntent`；
- 不直接信任脚本提供的可执行代码；
- 单会话、单目标窗口。

#### 进程 C：输入代理（可先合并，后拆分）

- 只接受有限动作：按下、释放、点击、移动、滚轮；
- 每批动作都带目标窗口标识、过期时间、会话 ID；
- 执行前再次核验前台窗口；
- 失焦、超时、急停、父进程退出时释放所有按键；
- 不提供读内存、注入、驱动、封包等接口。

### 3.3 IPC 方案

MVP 可以使用：

- 仅绑定 `127.0.0.1`；
- 每次启动生成随机会话令牌；
- 禁止任意 Origin；
- WebSocket 只传事件与低帧率调试缩略图；
- 原始高帧率画面留在运行时进程，不通过 JSON/Base64 传输。

产品化后可考虑：

- Windows Named Pipe；
- Tauri `invoke` + Python sidecar；
- 共享内存传递预览帧；
- 明确的消息版本和向后兼容策略。

---

## 4. 核心领域模型

### 4.1 项目不是一个 Python 文件

建议一个项目至少包含：

```text
project/
├─ project.yaml                 # 项目元数据、schema 版本
├─ targets/
│  └─ arena-lab.yaml            # 允许的目标程序与窗口匹配
├─ policies/
│  └─ default.yaml              # 运行上限、模式、动作预算
├─ calibrations/
│  ├─ 1920x1080-100.json        # 分辨率、DPI、UI 缩放标定
│  └─ 2560x1440-125.json
├─ assets/
│  ├─ templates/                # 模板图片
│  ├─ masks/                    # 忽略区与透明掩码
│  └─ labels/                   # 可选标注数据
├─ detectors/
│  ├─ ready_button.yaml
│  ├─ health_bar.yaml
│  └─ loading_screen.yaml
├─ machines/
│  └─ main.machine.yaml         # 状态机
├─ tests/
│  ├─ perception/
│  ├─ machine/
│  ├─ replay/
│  └─ e2e/
└─ traces/
```

### 4.2 关键对象

| 对象 | 职责 |
|---|---|
| `TargetProfile` | 可执行文件、签名、窗口标题规则、允许的显示模式、是否允许真实输入 |
| `CalibrationProfile` | 分辨率、DPI、UI 缩放、锚点、ROI 变换 |
| `VisualAsset` | 模板图、掩码、来源截图、裁剪区域、版本、哈希 |
| `Detector` | 输入 ROI、算法、阈值、输出语义、置信度、稳定帧要求 |
| `PerceptionSnapshot` | 某一时刻所有可观察事实，不包含动作 |
| `StateMachine` | 状态、迁移、守卫条件、超时、错误出口 |
| `InputIntent` | 抽象动作意图，不直接绑定底层库 |
| `PolicyProfile` | 模式、时间上限、动作配额、人工闸门、目标限制 |
| `RunTrace` | 输入帧引用、感知结果、状态迁移、意图、执行结果 |
| `TestCase` | 给定帧/事件序列，断言感知、状态与意图 |

### 4.3 为什么默认不允许任意 Python 脚本

任意 Python 代码会带来：

- 无法静态分析脚本会做什么；
- 无法准确列出能力；
- 难以回放和确定性测试；
- 可以绕过策略层直接操作系统；
- 分享脚本等于分享可执行代码，供应链风险很高；
- 同一脚本在不同机器上的依赖不可控。

推荐两层模型：

1. **普通脚本**：纯声明式 YAML/JSON，只能使用注册动作和检测器；
2. **开发插件**：单独安装、显式授权、能力声明、独立进程；后期可考虑 WASM 插件，而不是在主进程中 `exec()` Python。

---

## 5. 脚本 DSL 建议

下面示例只针对本地 `ArenaLab.exe` 测试程序，展示结构，不是任何在线游戏刷怪脚本。

```yaml
schema_version: 1
project: arena_lab_demo

target:
  executable: ArenaLab.exe
  title_regex: '^ArenaLab - Training$'
  input_mode: foreground

policy:
  require_manual_start: true
  require_foreground_before_every_batch: true
  max_runtime_minutes: 20
  max_actions_per_minute: 120
  on_focus_lost: stop
  on_disconnect_like_dialog: stop
  unattended_schedule: disabled

perception:
  - id: ready
    type: template_match
    roi: [0.35, 0.70, 0.30, 0.20]
    template: assets/templates/ready.png
    threshold: 0.91
    stable_frames: 3

  - id: health_ratio
    type: color_bar_ratio
    roi_anchor: player_panel
    hsv_ranges:
      - [0, 110, 70, 12, 255, 255]

machine:
  initial: idle
  states:
    idle:
      transitions:
        - when: ready.present
          to: awaiting_manual_gate

    awaiting_manual_gate:
      entry:
        - manual_gate: '确认开始本地测试'
      transitions:
        - when: manual_gate.approved
          to: exercise
        - on_timeout: 30s
          to: stopped

    exercise:
      actions:
        - press_key: space
      assert_after:
        - within: 2s
          condition: health_ratio.changed
      transitions:
        - when: health_ratio.value < 0.20
          to: stopped

    stopped:
      terminal: true
```

### 5.1 DSL 必须具备的静态检查

- 所有状态都能到达；
- 所有非终态都有超时或退出条件；
- 不允许无限循环且无预算；
- 所有动作都能映射到已声明能力；
- 真实输入只能出现在允许的目标配置；
- 在线目标配置不能启用无人值守调度；
- 模板、ROI、标定引用必须存在且有哈希；
- 迁移条件必须引用已定义的感知字段；
- 脚本 schema 必须可迁移、可锁版本。

---

## 6. “反作弊策略”应改成安全与合规策略

### 6.1 明确不做

平台代码、插件 API 和文档同时禁止：

- 检测反作弊进程或驱动并据此改变行为；
- 驱动隐藏、进程隐藏、窗口伪装、模块擦除；
- 内存读取、内存写入、DLL 注入、远程线程；
- 网络封包截获、修改、重放；
- 硬件标识伪装；
- 为逃避检测设计的“拟人化”轨迹、噪声和行为随机化；
- 多账号、多开、集群调度和自动账号轮换；
- 自动交易、拍卖、经济套利。

### 6.2 应该做的十二项保护

#### 1. 目标白名单

项目必须绑定允许的可执行文件、发布者签名、窗口类或标题规则。目标不匹配时拒绝运行，而不是“尽量找一个相似窗口”。

#### 2. 每批动作前核验前台窗口

不是启动时检查一次，而是在每个输入批次前检查：

- 当前前台 HWND；
- 进程 ID；
- 目标实例；
- 会话是否仍有效；
- 意图是否过期。

#### 3. 失焦即停并释放按键

失焦后不尝试自动抢回焦点。立即取消队列、释放所有已按下键，并进入需要人工恢复的状态。

#### 4. 全局急停

- 独立于 UI 线程；
- 即使预览卡死仍能触发；
- 首次触发立即停止输入，第二步再做清理；
- 可配置双热键，但必须有不可取消的默认组合；
- 记录急停原因和当时状态。

#### 5. 动作预算

同时限制：

- 每分钟动作数；
- 连续按键时长；
- 鼠标移动距离；
- 单次会话总动作数；
- 连续失败重试次数。

这不是模仿人，而是防止错误脚本失控。

#### 6. 运行时间硬上限

上限由策略层执行，脚本不能自行提高。达到上限后进入不可自动恢复的停止态。

#### 7. 在线配置默认禁止无人值守能力

- 不允许定时自动开始；
- 不允许自动重连；
- 不允许自动关闭安全提示后继续；
- 不允许系统启动后自动运行；
- 不允许在锁屏或远程会话切换后继续。

#### 8. Dry Run / Shadow Mode

决策引擎照常运行，但输入代理只记录“本来会做什么”。所有新脚本必须先通过 Shadow Mode 和回放测试，才能申请真实输入权限。

#### 9. 能力清单

每个脚本包声明：

```text
capture.window
vision.template_match
vision.ocr.roi
input.keyboard.foreground
input.mouse.foreground
notify.desktop
```

没有显式授权的能力不可调用。脚本包不能直接导入系统库绕过能力层。

#### 10. 审计与不可抵赖运行记录

至少记录：

- 脚本版本、资产哈希、模型版本；
- 目标窗口和标定配置；
- 感知快照；
- 状态迁移；
- 动作意图；
- 策略拒绝；
- 急停、失焦和异常。

建议按会话形成哈希链，方便判断日志是否被修改。

#### 11. 隐私保护

运行截图可能包含聊天、账号名、好友信息和通知：

- 默认只保存 ROI 和触发异常前后的少量帧；
- 提供固定遮罩区；
- 日志自动脱敏窗口标题、路径和用户名；
- 设置留存期限；
- 导出包默认不带原始截图。

#### 12. 扩展安全

MVP 不开放第三方任意 Python 插件。后续插件必须：

- 独立进程；
- 明确能力；
- 可撤销权限；
- 有版本锁和哈希；
- 无管理员权限；
- 崩溃不影响输入代理释放按键。

### 6.3 风险分级

| 能力 | 风险 | 默认策略 |
|---|---:|---|
| 截屏预览、画框、日志 | 低 | 允许 |
| 离线截图识别与回放 | 低 | 允许 |
| 本地测试程序真实输入 | 中 | 人工确认后允许 |
| 普通桌面应用前台自动化 | 中 | 目标白名单 + 限额 |
| 在线游戏观察/Shadow Mode | 中 | 允许，但不执行输入 |
| 在线游戏连续自动操作 | 高 | 默认禁用，不作为产品承诺能力 |
| 内存、注入、封包、驱动隐藏 | 不接受 | 架构级禁止 |

---

## 7. 测试体系：这是项目成败关键

### 7.1 感知单元测试

每个 `Detector` 都必须有独立测试集：

- 正样本；
- 近似但不应命中的负样本；
- 不同分辨率、DPI、UI 缩放；
- 亮度、色彩、压缩和运动模糊；
- 遮挡、粒子特效和字幕重叠；
- 语言变化；
- 模板过期样本。

输出至少包括：

- 是否命中；
- 置信度；
- 框位置误差；
- 处理耗时；
- 稳定帧结果。

### 7.2 状态机测试

状态机测试不需要真实屏幕。直接输入 `PerceptionSnapshot` 序列：

```text
frame 1: ready=false
frame 2: ready=true, stable=1
frame 3: ready=true, stable=2
frame 4: ready=true, stable=3
```

断言：

- 第几帧发生迁移；
- 产生哪些 `InputIntent`；
- 超时时进入哪个状态；
- 异常是否停止；
- 预算耗尽是否被策略层拒绝。

### 7.3 输入代理测试

使用 `FakeInputSink` 替代真实 `SendInput`：

- 验证顺序；
- 验证按下/释放配对；
- 验证取消后不再执行；
- 验证焦点不匹配时零输入；
- 验证父进程退出后释放所有键；
- 验证过期意图被丢弃。

### 7.4 轨迹回放

一次运行应能导出：

```text
时间戳 + 帧引用 + 感知结果 + 状态 + 意图 + 策略结果
```

回放分两种：

1. **固定感知回放**：跳过视觉，直接重放事实，测试状态机；
2. **原始帧回放**：重新执行视觉管线，检查算法或阈值变更造成的差异。

这能解决“昨天能跑、今天改了一个模板就不行，但无法复现”的问题。

### 7.5 变更影响分析

维护依赖图：

```text
模板图片
  → Detector
    → Perception 字段
      → 状态迁移
        → 测试用例
          → 项目/脚本包
```

修改模板、阈值或 ROI 后，自动列出受影响状态机和需要重跑的测试，而不是把全库都跑一遍或完全不测。

### 7.6 本地 ArenaLab 测试程序

建议单独建立一个小型本地程序，模拟：

- 血条变化；
- 冷却图标；
- 目标出现/消失；
- 掉落标记；
- 加载画面；
- 随机弹窗；
- 分辨率与 UI 缩放；
- 帧率下降；
- 窗口失焦；
- 输入延迟；
- 画面遮挡。

它的价值不是像某个具体游戏，而是让安全机制和回归测试可确定、可重复。

### 7.7 首版质量闸门

建议至少满足：

- 错误目标窗口时，真实输入次数为 0；
- Shadow Mode 下，真实输入次数为 0；
- 失焦或急停后不再接受新意图；
- 所有按下动作最终都有释放；
- 所有非终态都有超时或可证明的退出条件；
- 脚本与资产均有版本和哈希；
- 感知变更可以通过回放产生差异报告；
- 同一轨迹、同一版本、同一随机种子得到相同决策结果。

---

## 8. 各目标游戏的产品边界与工程判断

> 以下只做风险和工程适配判断，不提供这些游戏的自动刷怪流程、识别模板、技能循环或反检测方法。

| 游戏 | 工程特点 | 建议能力边界 |
|---|---|---|
| 暗黑破坏神系列 | 粒子效果密集、动态地图、加载与事件变化多，纯模板匹配容易脆弱 | 截屏标注、画面诊断、Shadow Mode；不承诺无人值守自动刷怪 |
| 流放之路 / PoE2 | 场景变化、技能特效和实例加载复杂；版本迭代会频繁破坏视觉资产 | 重点验证采集、ROI、回放和资产版本；真实输入默认关闭 |
| 魔兽世界 | UI 可配置，理论上有利于区域化识别；但外部自动输入风险高 | 只使用当前规则允许的游戏内宏、插件和辅助机制；工作台以观察/测试为主 |
| 冒险岛 | 2D 场景使部分视觉检测更简单，但自动化性质和账号风险不因此改变 | 只做通用视觉与本地测试能力，不提供挂机逻辑 |
| 梦幻西游 | 回合与固定 UI 使状态识别相对可结构化，但仍是受保护在线游戏 | 同上；不把画面易识别误当成合规或低风险 |

### 推荐接入顺序

1. `ArenaLab` 本地模拟器；
2. 一个自有普通桌面测试程序；
3. 离线截图数据集与录制轨迹；
4. 受保护在线游戏的观察/标注/Shadow Mode；
5. 只有在目标明确允许自动化时，才启用真实输入适配。

---

## 9. 工作台 UI 与交互方案

### 9.1 左侧：项目与资产

```text
项目
├─ 目标配置
├─ 标定
├─ 模板
├─ 检测器
├─ 状态机
├─ 测试
└─ 运行记录
```

核心交互：

- 从当前帧框选 ROI；
- 截图后自动加入资产库；
- 支持模板命名、掩码、阈值预览；
- 展示“这个资产被哪些检测器和状态引用”；
- 资产修改前显示影响范围。

### 9.2 中间：编辑器

首版不要一上来做自由节点画布。推荐：

- 状态列表；
- 当前状态的进入动作、守卫和迁移；
- 右侧属性面板；
- 下方自动生成只读状态图；
- YAML 与表单双向同步；
- 错误直接定位到字段。

等 DSL 稳定后再使用 Rete.js 做节点编辑，否则会把大量时间耗在连线、布局、撤销和序列化上，而执行语义仍在变化。

### 9.3 右侧：实时检查器

- 当前目标窗口；
- 当前帧时间；
- 检测器结果；
- 当前状态和进入时长；
- 下一个可能迁移；
- 动作预算；
- 策略拒绝原因；
- Dry Run / Real Input 明显区分。

### 9.4 底部：时间轴

按时间排列：

```text
frame → detector → state transition → intent → policy → execution
```

点击任一事件可回到当时帧、检测框和状态变量。这应成为产品最有价值的调试能力。

---

## 10. 版本管理与发布

### 10.1 版本不只是脚本文件

一次可发布版本必须冻结：

- DSL schema 版本；
- 状态机；
- 视觉资产和哈希；
- 阈值；
- 标定配置；
- OCR/模型版本；
- 策略配置；
- 运行时兼容范围；
- 测试结果。

### 10.2 发布流程

```text
草稿
  → 静态检查
  → 感知测试
  → 状态机测试
  → 轨迹回放
  → ArenaLab E2E
  → Shadow Mode
  → 人工审核
  → 签名发布
```

### 10.3 回滚

回滚必须整体回滚脚本包，不能只退 YAML 而保留新模板或新阈值。运行记录必须能准确指出当时使用的是哪个完整包。

---

## 11. 建议代码仓库结构

```text
visual-automation-workbench/
├─ apps/
│  ├─ workbench-ui/             # Vue3
│  ├─ desktop-shell/            # pywebview，后续可替换 Tauri
│  └─ arena-lab/                # 本地测试模拟器
├─ services/
│  ├─ control-plane/            # 会话、项目、IPC、事件流
│  ├─ runtime-engine/           # 调度、状态机、取消、回放
│  └─ input-broker/             # 前台输入与安全释放
├─ packages/
│  ├─ capture-api/
│  ├─ capture-win-dxcam/
│  ├─ capture-mss/
│  ├─ vision-core/
│  ├─ detector-opencv/
│  ├─ detector-ocr/
│  ├─ state-machine/
│  ├─ script-schema/
│  ├─ policy-engine/
│  ├─ trace-format/
│  └─ test-kit/
├─ schemas/
│  ├─ project.schema.json
│  ├─ detector.schema.json
│  ├─ machine.schema.json
│  └─ policy.schema.json
├─ examples/
│  └─ arena-lab-demo/
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ replay/
│  └─ e2e-windows/
├─ docs/
│  ├─ architecture/
│  ├─ safety/
│  ├─ script-authoring/
│  └─ adr/
└─ tools/
   ├─ asset-audit/
   ├─ trace-diff/
   └─ release-packager/
```

---

## 12. 分阶段开发方案

### M0：边界、Schema 与本地测试场

交付：

- 安全边界和禁止能力清单；
- 项目、目标、检测器、状态机、策略 JSON Schema；
- `ArenaLab` 最小模拟器；
- 运行轨迹格式；
- 架构决策记录 ADR。

通过条件：

- 不依赖任何真实游戏即可演示完整闭环；
- 所有动作都先成为 `InputIntent`；
- 在线目标默认不能开启真实输入。

### M1：工作台壳、采集、预览与安全输入

交付：

- Vue3 + pywebview 桌面壳；
- 目标窗口选择；
- DXcam/WGC 或 DXGI 采集；
- MSS 回退；
- 实时预览与 ROI 框选；
- Input Broker；
- 全局急停、失焦停止、按键释放；
- Dry Run。

通过条件：

- 错窗口、失焦、急停、父进程退出均不会继续输入；
- 能看到完整事件时间轴。

### M2：感知、状态机与回放测试

交付：

- 模板匹配、颜色区域、变化检测；
- ROI OCR；
- `transitions` 状态机运行时；
- YAML/JSON 脚本编译与静态检查；
- FakeInputSink；
- 固定感知回放、原始帧回放；
- 测试报告和差异报告。

通过条件：

- 修改模板或阈值后能自动指出受影响测试；
- 同一轨迹可稳定重现状态迁移。

### M3：资产与可视化编辑体验

交付：

- 图片资产库；
- 模板掩码与阈值预览；
- 标定配置；
- 步骤/状态表单编辑器；
- 自动状态图；
- 版本打包、签名、整体回滚。

通过条件：

- 非开发者无需改 Python 即可完成本地演示流程；
- 每个资产都能追踪引用关系。

### M4：节点编辑器与插件机制

交付：

- Rete.js 节点视图；
- 显式能力系统；
- 独立插件进程或 WASM 插件实验；
- 通知、托盘、自动更新；
- Tauri 壳评估。

通过条件：

- 节点图只是 DSL 的一种编辑视图，不产生另一套语义；
- 插件无法直接绕过 Policy Guard 与 Input Broker。

### M5：受控目标适配

交付范围只包括：

- 本地/离线目标；
- 自有测试软件；
- 明确允许自动化的环境；
- 在线游戏观察、标注、回放和 Shadow Mode。

不把“在线游戏无人值守刷怪”作为验收目标，也不承诺账号安全。

---

## 13. 开发任务优先级

### P0：必须先做

- [ ] 项目 Schema 与版本策略
- [ ] TargetProfile 与目标白名单
- [ ] InputIntent / Input Broker 分层
- [ ] 前台窗口二次校验
- [ ] 全局急停与按键释放
- [ ] Dry Run / Shadow Mode
- [ ] Capture API 与 DXcam/MSS 适配
- [ ] 实时预览、ROI 框选
- [ ] RunTrace 与时间轴
- [ ] ArenaLab
- [ ] FakeInputSink 与安全测试

### P1：形成可用工作台

- [ ] 模板匹配检测器
- [ ] 颜色条/像素统计检测器
- [ ] 画面变化/稳定检测器
- [ ] ROI OCR
- [ ] 状态机运行时
- [ ] DSL 静态检查
- [ ] 轨迹回放与差异
- [ ] 图片资产库
- [ ] 标定配置
- [ ] 版本包与整体回滚

### P2：提升易用性

- [ ] 状态表单编辑器
- [ ] 自动状态图
- [ ] Rete.js 节点视图
- [ ] 测试集管理
- [ ] 变更影响图
- [ ] 运行报告
- [ ] 隐私遮罩与留存策略
- [ ] 桌面通知和托盘

### 暂不开发

- [ ] 在线游戏自动刷怪脚本包
- [ ] 反作弊检测或绕过
- [ ] 拟人化轨迹
- [ ] 后台多开
- [ ] 自动重连后继续无人值守
- [ ] 任意 Python 脚本市场
- [ ] 内存、注入、封包、驱动能力
- [ ] YOLO 训练平台

---

## 14. 对三项待决策事项的明确建议

### 决策 1：纯 Python，还是 AHK v2 + Python？

**结论：Python 作为唯一核心运行时；AHK v2 仅作为可选开发辅助。**

原因：

- 感知、状态、回放、测试都在 Python 更统一；
- AHK 与 Python 双运行时会增加取消、日志、异常和时序一致性问题；
- 热键、窗口管理和输入可以通过 Win32 API 封装；
- 如果某个兼容场景需要 AHK，可通过受限适配器调用，不让 AHK 脚本绕过策略层。

### 决策 2：第一个接入哪个游戏？

**结论：都不是。第一个接入 `ArenaLab`。**

第二阶段再拿真实游戏做只读采集、标注和 Shadow Mode，用来验证分辨率、画面和性能，不直接把“刷怪成功”当作里程碑。

### 决策 3：Python 脚本还是 JSON 状态机？

**结论：从第一版就使用 YAML/JSON 状态机；Python 只用于实现平台内置检测器与适配器。**

这样做会稍微增加前期 Schema 设计工作，但能换来：

- 静态检查；
- 可视化编辑；
- 确定性回放；
- 版本迁移；
- 能力约束；
- 安全分享；
- 自动测试。

---

## 15. 最终产品差异化

不要把差异化定义成：

> 比按键精灵更能刷、比现成 Bot 更不容易被检测。

应该定义成：

> **任何视觉判断和前台动作都可解释、可预览、可回放、可测试、可审计，并且在错误窗口、焦点丢失或策略不允许时保证零输入。**

这条路线既能支持你想要的“前台视觉工作台”能力，也能避免整个项目被在线游戏反作弊对抗拖进无法维护、无法承诺、风险不断升级的方向。

---

## 16. 参考项目

### 工作台与视觉自动化

- OculiX: https://github.com/oculix-org/Oculix
- SikuliX1: https://github.com/oculix-org/SikuliX1
- Actiona: https://github.com/Jmgr/actiona
- Pulover's Macro Creator: https://github.com/Pulover/Pulovers-Macro-Creator
- AutoHotkey: https://github.com/AutoHotkey/AutoHotkey

### 采集、视觉与 OCR

- DXcam: https://github.com/ra1nty/DXcam
- python-mss: https://github.com/BoboTiG/python-mss
- OpenCV: https://github.com/opencv/opencv
- PaddleOCR: https://github.com/PaddlePaddle/PaddleOCR
- Ultralytics: https://github.com/ultralytics/ultralytics

### 状态机、编辑器与桌面壳

- transitions: https://github.com/pytransitions/transitions
- py_trees: https://github.com/splintered-reality/py_trees
- Rete.js: https://github.com/retejs/rete
- pywebview: https://github.com/r0x0r/pywebview
- Tauri: https://github.com/tauri-apps/tauri

### 输入原型

- PyAutoGUI: https://github.com/asweigart/pyautogui
- PyDirectInput: https://github.com/learncodebygaming/pydirectinput

---

*本文基于用户 v0.1 需求文档扩展。GitHub 项目状态、许可证和兼容性应在正式选型与分发前再次核验；涉及在线游戏时，应以游戏运营方在用户所在地区的最新规则为准。*
