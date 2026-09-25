# 运行与故障处理手册（Runbook，DOC-002）

| 项 | 内容 |
|---|---|
| 状态 | 已发布（M3 评审基线） |
| 日期 | 2026-09-25 |
| 关联任务 ID | DOC-002（主责）、INP-006/007/010、CAP-006/007、CTL-009、REL-004、SEC-005/006/007、VER-008/009 |
| 里程碑 | M3 |
| 来源 | 《任务与测试验收方案》§4 E04/E05/E08/E10/E14、§13 UAT；《GitHub 调研与安全架构》§6.2 |
| 关联文档 | `docs/safety/safety-boundaries.md`（风险边界）、`docs/safety/threat-model.md`、`docs/operations/install-upgrade.md` |

本手册面向操作者/运维：怎么启动、怎么选目标、出事时按什么顺序处置、
怎么取证（诊断包）、怎么回滚。**安全优先级永远高于任务完成**：
任何拿不准的情况，先急停（§3），再排查。

---

## 1. 启动与停止

### 1.1 正常启动（桌面壳）

```bash
python -m desktop_shell --project-root <工作区目录>
```

- 桌面壳启动后端（control_plane，仅绑定本机回环地址）并打开 UI 窗口；
- 后端地址与访问令牌打印在日志中：`API: http://127.0.0.1:<port>/api/v1`、
  `令牌: <随机 hex>`——**令牌等同本机控制权，请勿泄露**；
- 无 GUI 环境（服务器/远端会话）或前端开发时改用后端-only 模式：

```bash
python -m desktop_shell --backend-only --project-root <工作区目录>
# 输出 [backend-only] API: ... 令牌: ...（请勿泄露）
```

### 1.2 启动自检

首次启动或升级后首启，先跑首次启动检查（REL-005）：

```bash
python tools/release_packager/first_run_check.py
```

输出逐项 PASS/FAIL（目录可写、Python 版本、依赖可导入、显示器可用、
旧版本迁移状态）。任何 FAIL 先按 §5 处置再启动会话。

### 1.3 正常停止

1. UI「停止」按钮（或 `POST /api/v1/sessions/{id}/stop`）；
2. 停止语义固定为：**轨迹 flush → 采集 stop → Broker stop**；
   最后一步会释放所有仍按下的键（补偿 key_up，不经过策略评估）；
3. 会话落终态 `stopped`，运行摘要（visited states / intents 总数 /
   丢帧数）写入轨迹与控制面。

不要用强杀进程代替正常停止：强杀由父进程看门狗兜底（§3.4），但仍会
丢失最后的轨迹 flush。

---

## 2. 目标选择与人工闸门

1. **目标白名单**：只能选择项目 `targets/*.yaml` 登记的目标；控制面按
   `executable + title_regex` 匹配前台窗口并核验 HWND/PID。匹配失败会话
   不启动真实输入。
2. **模式选择**：`observe / shadow / dry_run` 不产生真实输入；
   `real_input` 仅允许本地/离线/自有/明确允许自动化的目标
   （`protected_online: true` 的目标被静态硬锁，见 §7）。
3. **人工闸门（manual gate）**：`require_manual_start: true` 或状态机
   `manual_gate` 动作都会挂起等待人工确认：
   - 未确认即调 start：控制面返回 **409 `manual_gate_required`**；
   - 确认入口：UI 确认按钮或 `POST /api/v1/sessions/{id}/confirm`；
   - 确认前控制面会展示目标身份（窗口标题/PID）、模式、预算上限。
4. **运行期窗口核验**：每批输入执行前重新核验前台窗口、目标实例与会话
   TTL；核验不过该批拒绝，连续失败触发停止。

---

## 3. 急停（E-Stop）

### 3.1 语义（两步，ADR-0004）

- **第一步（立即，不等任何策略评估）**：cancel InputBroker——拒绝一切新
  批次，并生成补偿 `key_up` 序列释放所有仍按下的键（KeyLedger 幂等）；
- **第二步（清理）**：结束会话占用 + 审计入链（`estop` + `keys_released`
  事件，含 source/reason/时间戳/释放键列表）。

急停与 UI 完全解耦：**UI 卡死不影响急停**。触发是幂等的（重复触发只执行
一次）。

### 3.2 触发途径

| 途径 | 操作 | source |
|---|---|---|
| 全局热键 | **Ctrl+Alt+F12**（默认组合，系统级注册，任意前台生效） | `hotkey` |
| UI 按钮 | 工作台急停按钮 | `ui` |
| 语义/API | 控制面停止/急停接口 | `api` |
| 失焦看护 | 焦点离开目标窗口自动触发（`on_focus_lost: stop`） | `focus_lost` |
| 父进程看门狗 | 宿主进程死亡自动触发 | `watchdog` |

热键注册失败（被其他程序占用）会在日志报
`hotkey_register_failed_in_use` 并标记 failed——此时**先改用 UI 按钮或
API 途径**，并按 §5.4 处理热键冲突。

### 3.3 急停后的恢复流程

1. 确认目标窗口状态（无残留按键：日志应有 `keys_released` 且列表覆盖
   此前按下的键）；
2. 查看急停审计事件（source/reason）定位触发原因；
3. 排除原因后**重新走完整启动流程**：新建会话 → 人工闸门确认 → 启动。
   系统不会自动续跑被急停的会话（防无人值守误恢复）。

### 3.4 崩溃兜底

父进程（控制面/宿主）崩溃时，看门狗线程探测到即触发同语义急停——
先停输入、释放按键，再清理。恢复后启动时控制面会把上次遗留的非终态会话
标记为 `interrupted`（§6）。

---

## 4. 失焦 / 锁屏 / 权限错误

| 症状 | 系统行为 | 操作者处置 |
|---|---|---|
| 运行中切走窗口（目标失焦） | 立即停止输入并释放按键；不自动续跑 | 回到目标窗口后**重新确认并启动**新会话（UAT-005） |
| 锁屏 / 会话切换 / UAC 安全桌面 | 采集失败或前台核验失败 → 停止输入 | 解锁后重建会话；核对标定分辨率未变 |
| 目标窗口关闭 / PID 消失 | 窗口核验拒绝后续批次，会话停止 | 重开目标窗口，新会话 |
| 热键注册失败 `hotkey_register_failed_in_use` | 急停热键不可用（其余途径正常） | 关闭占用程序或修改组合后重启壳 |
| 控制面 401/403 | 令牌缺失或不符 | 用启动时打印的令牌重新接入 |
| 端口占用 / 后端起不来 | `BackendStartupError` | 换 `--port` 或释放端口；见 §5.2 |

权限类错误共性原则：**一切不确定 → 先急停**。系统被设计为"宁可停错，
不可多按"。

---

## 5. 采集故障：黑帧降级（needs_reconfirm）

### 5.1 判定与降级

采集链（主源如 DXcam，备源如 mss）在以下情况切换/降级，并置
`needs_reconfirm = true`（AC-P0-13）：

| reason | 含义 |
|---|---|
| `black_frame` | 整帧方差 ≤1 且平均亮度 ≤8（纯黑画面）连续出现 |
| `size_anomaly` | 帧尺寸偏离该源基线 |
| `grab_error` | 抓帧调用异常 |
| `start_error` | 主源启动即不可用（直接切备源） |

### 5.2 降级后的标准流程

1. 真实输入**立即停止**（采集不可信时不允许基于坏帧决策）；
2. 查看采集事件（`CaptureChain.events()`，控制面诊断页同源展示）确认
   reason 与切换情况；
3. 人工重新确认目标与标定（分辨率/DPI 是否变了、窗口是否被遮挡/最小化、
   显卡驱动是否异常）；
4. 确认无误后调用 `acknowledge_reconfirm()` 清除标志（UI 上是"重新确认"
   按钮），再恢复会话；**清除标志必须由人工确认动作触发，无自动恢复**。

---

## 6. 会话状态机与恢复（interrupted）

```text
created ──start──▶ running ──pause──▶ paused ──resume──▶ resumed
  running/paused/resumed ──stop──▶ stopped
  running/paused/resumed ──（异常）──▶ failed
  任意非终态 ──（启动恢复）──▶ interrupted
终态：stopped / failed / interrupted（不可再迁移）
```

- `real_input` 会话必须先 confirm 通过人工闸门（审计入会话文件）；未确认
  start 即 409 `manual_gate_required`；
- **全局只允许一个活跃 real_input 会话**（CTL-006/SAFE-019）：第二个
  real_input 会话创建即 409 `real_input_session_exists`；受保护在线目标创建
  real_input 会话直接 409 `protected_online_no_real_input`（与静态硬锁构成
  纵深防御）；
- pause/resume/stop 幂等：重复调用返回当前记录不报错；
- 启动时发现上次遗留的非终态会话（created/running/paused/resumed），控制面
  统一标记为 **`interrupted`**（reason=`startup_recovery`，审计事件
  `session_interrupted`）——这是 CTL-009 启动恢复路径，防止"以为还在跑"；
- `interrupted` 是终态：不可 resume。恢复 = 新建会话 + 重新人工确认；
- 所有状态迁移带审计事件（时间、from/to、原因），持久化在
  `sessions/<session_id>.json`，可用轨迹时间轴回放。

---

## 7. 风险边界（摘要，全文见 docs/safety）

- 受保护在线目标（`protected_online: true`）被静态硬锁：只允许
  `observe/shadow/dry_run`；`real_input` 与无人值守调度直接被拒绝
  （`protected_online_no_real_input` / `protected_online_unattended`）；
- `real_input` 模式必须 `unattended_schedule: disabled`（有人在场）；
- 不存在、也不会提供：进程隐藏、反作弊绕过、内存读写、封包操作、
  拟人化逃避检测、多开集群（八条架构不变量，见
  `docs/safety/safety-boundaries.md` §2）。

---

## 8. 诊断包、隐私与留存

### 8.1 生成诊断包（脱敏，REL-004）

```bash
python tools/release_packager/diagnostics.py --project <项目目录> --out diag.zip
```

（等价于 `security_kit.sanitize.diagnostic_bundle`：版本信息 + 脱敏配置 +
脱敏最近日志 + 成员 sha256 清单；**不含任何原图/截图**，写包后自检发现
图片成员会直接失败。）

### 8.2 隐私策略（SEC-005/007）

- 截图导出/缩略图统一应用隐私遮罩（`PrivacyMask`）；遮罩不可被预览缓存绕过；
- 诊断包与导出轨迹默认脱敏：本机用户名/主目录 → `<user>`，盘符路径 →
  `<path>/<文件名>`，窗口标题 → `<title>`（过删则过取向）；
- 给他人排障只发**脱敏轨迹**（UAT-009）与诊断包；不要发原始截图目录。

### 8.3 留存与清理（SEC-006）

默认最小留存：轨迹 7 天、截图快照 14 天、帧存储 3 天、日志 7 天。
清理由保留策略执行：发布基线引用的文件受保护不误删（`protected_paths`），
逐条清理审计可查。升级/回滚前自动备份见 `docs/operations/install-upgrade.md`。

---

## 9. 版本回滚操作（VER-008，AC-P0-12）

前置：目标版本存在于 `releases/`（发布历史只增不删）。

```bash
export PYTHONPATH="packages;services;apps"
python -c "
from release_kit.rollback import ReleaseRollback
from common.clock import MonotonicClock

rb = ReleaseRollback('examples/arena_lab_demo', 'examples/arena_lab_demo/releases',
                     clock=MonotonicClock())
result = rb.rollback_to('v1-<短哈希>')        # 五类对象+资产+策略一起回滚
print(result.release_id, result.backup_dir, result.files_restored)
"
```

系统保证的步骤（无需手工干预）：

1. 加载目标清单 → 兼容检查（可选传 `current_runtime`）；
2. **发布目录完整性校验**——发布目录被篡改时拒绝回滚；
3. **自动升级式备份**当前工作区到 `backups/<时刻>-pre-rollback/`；
4. 整体同步（复制 + 删除多余受管文件）；
5. **回滚后完整性校验**（哈希必须与目标版本一致，否则报错）。

回滚后：运行记录仍引用各自原始版本（审计不篡改）；如需回退"回滚"，
从第 3 步的备份目录恢复。候选版发布/回滚链路的自动化冒烟见
`python tools/acceptance/run_m3_checks.py`（AC-P0-12 最小复现）。
