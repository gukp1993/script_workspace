# 架构总览（Architecture Overview）

| 项 | 内容 |
|---|---|
| 状态 | 已接受（M0 评审基线） |
| 日期 | 2026-09-25 |
| 关联任务 ID | GOV-002、GOV-001、ENG-001 |
| 里程碑 | M0 |
| 上游依据 | 仓库根目录《Windows 前台视觉自动化工作台_GitHub调研与安全架构_20260925.md》《Windows前台视觉自动化工作台_完整开发任务与测试验收方案_20260925.md》 |

## 1. 产品定位与架构目标

> **可观察、可回放、可测试的 Windows 前台视觉自动化工作台。**

架构围绕"屏幕采集 → 视觉感知 → 状态机决策 → 策略守卫 → 前台输入"闭环组织；所有真实输入都必须经过目标白名单、前台复核、模式、时限、配额、急停和审计控制。八条架构不变量与禁止能力清单见 `docs/safety/safety-boundaries.md`。

## 2. 逻辑架构图

来源：《GitHub 调研与安全架构》§3.1（原样引用）。

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

进程划分（桌面壳 / 运行时引擎 / 输入代理）与 IPC 安全方案分别见 ADR-0001、ADR-0002；输入链路分层见 ADR-0004；采集选型见 ADR-0008。

## 3. 里程碑目标与退出条件（M0-M3）

来源：《任务与测试验收方案》§3（M4/M5 为增强或受控适配范围，不是首版发布阻塞项，详见源文档）。

| 里程碑 | 目标 | 主要交付 | 退出条件 |
|---|---|---|---|
| M0 边界与测试场 | 证明架构可落地且不依赖真实游戏 | ADR、Schema、领域模型、ArenaLab、轨迹格式、工程底座 | 示例项目可校验；ArenaLab 可确定重放；所有动作先形成 InputIntent；在线目标默认无真实输入 |
| M1 安全闭环 | 建立桌面壳、采集、目标锁和安全输入 | pywebview 壳、目标选择、采集、Dry/Shadow、Input Broker、急停、失焦停止、基础时间轴 | 错目标/失焦/急停/进程退出真实输入均为 0 或立即停止；无卡键；P0 安全用例 100% 通过 |
| M2 可测试自动化 | 完成视觉、状态机、回放和测试自动化 | 模板/颜色/变化/OCR、DSL 编译、静态检查、固定感知与原始帧回放、测试中心基础 | 同轨迹确定性；视觉指标达标；变更可生成差异；ArenaLab E2E 通过 |
| M3 可发布工作台 | 完成资产、版本、发布、回滚、隐私和安装 | 资产库、编辑器、发布包、影响分析、签名、回滚、安装器、诊断包、完整测试报告 | 候选版通过全量回归、性能、长稳、安全与 UAT；无 P0/P1 缺陷；可完整回滚 |

M0 验收清单（任务文档 §12.1）要求：架构不变量、禁止能力和威胁模型完成评审；核心 Schema、示例项目、能力清单和 RunTrace 格式通过正反样例；无模块直接调用输入库，InputIntent 契约已冻结；protected_online 默认策略明确为无真实输入。M0 证据包括 ADR、Schema 包、CI 链接、ArenaLab 构建、示例项目、威胁模型、评审纪要。

## 4. 目录结构说明

与仓库根目录 `README.md`（ENG-001）保持一致：

```text
apps/          应用：arena_lab（本地测试模拟器）、desktop_shell、workbench_ui
services/      服务：input_broker、control_plane、runtime_engine
packages/      公共包：common、domain_model、policy_engine、trace_format、test_kit …
schemas/       项目/目标/检测器/状态机/策略 JSON Schema
examples/      arena_lab_demo（有效示例）、protected_online_demo（必须被拒绝的反例）
tests/         unit / contract / replay / security / e2e_windows
docs/          adr / architecture / safety / governance / authoring / operations
tools/         acceptance（M0 验收脚本）等
```

各目录与架构组件、进程划分的对应关系：

| 目录 | 架构组件 / 进程 | 说明 |
|---|---|---|
| `apps/desktop_shell`、`apps/workbench_ui` | 进程 A：桌面壳与 UI | 展示层，不直接发送输入（ADR-0001） |
| `apps/arena_lab` | 本地测试模拟器 | 不依赖真实游戏的可确定测试场（E03） |
| `services/control_plane`、`services/runtime_engine` | 进程 B：运行时引擎（含 Control Plane / Runtime Coordinator / Store） | 产生 InputIntent，单会话单目标（ADR-0001） |
| `services/input_broker` | 进程 C：输入代理 | 唯一 OS 输入能力持有者，二次核验与按键释放（ADR-0001/0004） |
| `packages/policy_engine` | Policy Guard | 默认拒绝、protected_online 硬锁、模式机 |
| `packages/trace_format` | Asset / Trace / Test Store（轨迹部分） | RunTrace 版本化格式（ADR-0005） |
| `packages/capture_api` 等 | Capture | 统一采集接口与适配器（ADR-0008） |
| `packages/domain_model`、`schemas/` | 领域模型与契约 | 核心对象与 JSON Schema、静态检查基础（ADR-0003） |
| `examples/protected_online_demo` | 反例夹具 | 必须被 Schema/Policy 拒绝，验证硬锁 |
| `docs/adr`、`docs/architecture`、`docs/safety`、`docs/governance` | 治理文档 | 架构决策、安全边界、威胁模型、流程模板 |
| `tools/acceptance` | 验收工具 | M0 验收门（`run_m0_checks.py`） |

## 5. 关联文档

- `docs/safety/safety-boundaries.md`（八条不变量、禁止能力、风险分级、一票否决项）
- `docs/safety/threat-model.md`（TM-01 ~ TM-10）
- `docs/adr/ADR-0001` ~ `ADR-0008`（进程、IPC、DSL、输入、轨迹、壳、发布、采集）
- `docs/governance/dor-dod.md`、`docs/governance/review-template.md`
- 仓库根目录两份设计文档为唯一上游依据；本文与其不一致时以上游为准并回改本文。
