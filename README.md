# Windows 前台视觉自动化工作台

可观察、可回放、可测试的 Windows 前台视觉自动化工作台（M0 开发中）。

> 安全边界：不实现进程隐藏、驱动隐藏、反作弊探测、内存读写、进程注入、
> 封包截获/修改、硬件伪装、拟人化逃避检测、多账号集群或后台多开。
> 真实输入仅用于本地/离线/自有/明确允许自动化的目标；受保护在线目标
> 默认只允许观察、标注、回放与 Shadow Mode。详见 `docs/safety/`。

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

| 里程碑 | 状态 |
|---|---|
| M0 边界与测试场 | 进行中 |
| M1 安全闭环 | 未开始 |
| M2 可测试自动化 | 未开始 |
| M3 可发布工作台 | 未开始 |

规划与验收依据见仓库根目录两份设计文档。
