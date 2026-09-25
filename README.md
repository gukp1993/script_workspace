# Windows 前台视觉自动化工作台

可观察、可回放、可测试的 Windows 前台视觉自动化工作台（M0 开发中）。

> 安全边界：不实现进程隐藏、驱动隐藏、反作弊探测、内存读写、进程注入、
> 封包截获/修改、硬件伪装、拟人化逃避检测、多账号集群或后台多开。
> 真实输入仅用于本地/离线/自有/明确允许自动化的目标；受保护在线目标
> 默认只允许观察、标注、回放与 Shadow Mode。详见 `docs/safety/`。

## 快速启动

要求：Windows 10/11 + Python 3.12（已 `pip install -r requirements.txt`）。

```bash
# 1) 桌面模式：一条命令起后端 + 工作台窗口（推荐）
export PYTHONPATH="packages;services;apps"      # CMD: set PYTHONPATH=packages;services;apps
python -m desktop_shell
# 窗口标题「VAW 前台视觉自动化工作台」，令牌自动注入；关闭窗口即退出后端

# 2) 浏览器模式：只起后端，用浏览器打开打印的地址
python -m desktop_shell --backend-only --port 17653
# 按提示带 token 访问，如 http://127.0.0.1:17653/?token=<控制台打印的令牌>

# 3) 前端开发模式：Vite 热更新 + 后端
cd apps/workbench_ui && npm install && npm run dev   # 终端 A
python -m desktop_shell --dev http://localhost:5173  # 终端 B
```

配套演示：

```bash
python -m arena_lab.view happy_path      # ArenaLab 模拟器画面（本地测试目标）
python -m arena_lab.e2e_smoke            # 真窗口采集 E2E 冒烟
```

> 安全：API 仅绑定 127.0.0.1 且要求令牌（页面经 `?token=` 注入，静态资源不含数据）；
> 真实输入只在人工闸门确认后、且目标窗口/前台持续匹配时才会发出。

## 仓库结构（ENG-001）

```
apps/          应用：arena_lab（本地测试模拟器）、desktop_shell、workbench_ui
services/      服务：input_broker、control_plane、runtime_engine
packages/      公共包：common、domain_model、policy_engine、trace_format、test_kit …
schemas/       项目/目标/检测器/状态机/策略 JSON Schema
examples/      arena_lab_demo（有效示例）、protected_online_demo（必须被拒绝的反例）
tests/         unit / contract / replay / security / e2e_windows
docs/          adr / architecture / safety / governance / authoring / operations
tools/         acceptance（M0 验收脚本）等
```

## 开发环境（ENG-002）

要求：Windows 10/11 x64 + Python 3.12。

```bash
python -m pip install -r requirements.txt   # 锁定依赖
python -m pytest tests -q                   # 全量单测

# python -m 直跑需要注入包路径（pytest 已自动处理）
export PYTHONPATH="packages;services;apps"  # Git Bash 写法；CMD 用 set，PowerShell 用 $env:
python -m arena_lab.selfcheck               # ArenaLab 确定性自检
python -m policy_engine.demo                # 策略安全场景演示
python -m domain_model.validate examples/arena_lab_demo          # 示例项目校验（应通过）
python -m domain_model.validate examples/protected_online_demo   # 反例校验（应拒绝）
python tools/acceptance/run_m0_checks.py    # M0 验收门（已自动注入 PYTHONPATH）
```

## 导入约定

pytest 通过 `pythonpath = ["packages", "services", "apps"]` 注入路径：

- `packages/common/common/` → `import common`
- `packages/domain_model/…` → `import domain_model`
- `services/input_broker/…` → `import input_broker`
- `apps/arena_lab/…` → `import arena_lab`

## 里程碑

| 里程碑 | 状态 | 验收门 |
|---|---|---|
| M0 边界与测试场 | ✅ 完成 | `python tools/acceptance/run_m0_checks.py`（6/6） |
| M1 安全闭环 | ✅ 完成 | `python tools/acceptance/run_m1_checks.py`（8/8） |
| M2 可测试自动化 | ✅ 完成 | `python tools/acceptance/run_m2_checks.py`（9/9） |
| M3 可发布工作台 | ✅ 完成 | `python tools/acceptance/run_m3_checks.py`（8/8，递归含 M0-M2） |
| M4 易用性增强 | ✅ 核心完成* | `python tools/acceptance/run_m45_checks.py` |
| M5 受控适配 | ✅ 核心完成* | 同上 |

\* M4 节点编辑器为自研 SVG 实现（不引 Rete.js，ADR 级偏差见代码 docstring）；
扩展内存硬限额（Job Object）、PLG-003 撤销 UI、8 小时长稳与 UAT 签字属后续运维事项。

## 安全边界（摘要）

- 全平台唯一系统输入入口：`services/input_broker/win32_adapter.py`（SendInput）；
  静态守卫强制 `ctypes`/`win32*` 仅存在于白名单文件。
- 一切动作先成为 `InputIntent`，经 `policy_engine`（默认拒绝 + protected_online 硬锁）
  与 `InputBroker`（每批复核前台/TTL/预算/急停释放）才可执行。
- 错误窗口、Shadow/DryRun、失焦、急停、父进程退出 → 真实输入恒为 0（SAFE-001~022 矩阵）。
