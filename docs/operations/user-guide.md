# VAW 前台视觉自动化工作台——使用指南

| 项 | 内容 |
|---|---|
| 状态 | 已发布（对应 M0–M5 交付） |
| 日期 | 2026-09-25 |
| 关联任务 | UI-001~017、REL-002/005、DOC-002 |
| 适用读者 | 使用工作台配置与运行自动化会话的用户（非开发者亦可） |

---

## 1. 它是什么

一个**可观察、可回放、可测试**的 Windows 前台视觉自动化工作台：
屏幕采集 → 视觉识别 → 状态机决策 → 策略守卫 → 前台输入，全程留痕、可回放、可整体回滚。

安全底线（系统强制，非约定）：

- 所有动作先成为 `InputIntent`，经策略评估与输入代理双重把关；
- 错误窗口、Shadow/DryRun、失焦、急停、父进程退出 → **真实输入恒为 0**；
- 受保护在线目标（`protected_online: true`）被三层硬锁，**无法**启用真实输入与无人值守；
- 不做注入、读内存、封包、反作弊对抗、后台多开。

---

## 2. 启动与访问

前置：Windows 10/11 + Python 3.12 + `pip install -r requirements.txt`。

### 方式一：桌面窗口（推荐）

```bash
cd C:/Users/顾柯鹏/Desktop/zcode/script_workspace
export PYTHONPATH="packages;services;apps"     # CMD: set；PowerShell: $env:
python -m desktop_shell
```

弹出「VAW 前台视觉自动化工作台」窗口，令牌自动注入；**关闭窗口即退出后端**。

### 方式二：浏览器

```bash
python -m desktop_shell --backend-only --port 17653
# 浏览器打开控制台打印的：http://127.0.0.1:17653/?token=<令牌>
```

### 方式三：前端开发（热更新）

```bash
cd apps/workbench_ui && npm install && npm run dev    # 终端 A
python -m desktop_shell --dev http://localhost:5173   # 终端 B
```

要点：

- API 只绑定 127.0.0.1；令牌经 `?token=` 注入后存 localStorage，后续请求自动携带；
- 每次不带 `--token` 启动会生成新令牌；**要固定地址**，用 `--token <hex> --port 17653` 启动，并把令牌存起来；
- `--project-root <dir>` 指定工作区（默认当前目录）；项目放在 `<工作区>/projects/<项目名>/`。

---

## 3. 界面导航（左侧对象树）

| 页面 | 用途 |
|---|---|
| ProjectNav | 项目/目标/标定/资产/检测器/状态机/测试/运行记录导航 |
| TargetSelect | 目标与会话：窗口身份（PID/HWND）、四种模式、RealInput 两步人工确认 |
| ControlBar | 启动/暂停/恢复/停止；停止永远可点，显示清理进度 |
| LivePreview | 实时画面（帧率/状态显示） |
| AssetLibrary | 模板/掩码资产：缩略图、哈希、引用关系、删除保护 |
| DetectorEditor | 检测器参数（类型/阈值/ROI/稳定帧）与离线校验 |
| CalibrationWizard | 标定三步向导（分辨率/DPI/缩放 → 锚点 → 生成并保存） |
| MachineEditor | 状态机表单编辑，YAML 实时同步（解析失败就地标红、不覆盖表单） |
| StateGraph / 节点编辑 | 只读状态图（不可达红色）与可编辑节点图（与 DSL 同一语义） |
| Inspector | 实时检查器：感知字段/当前状态时长/预算余量/策略拒绝原因/模式色带 |
| TimelineView | 运行时间轴四泳道（感知/状态/意图/执行），点事件看快照与帧 |
| TestCenter | 回放与测试报告、差异报告查看 |
| SettingsView | 默认模式/保留期；高风险项需勾选二次确认 |

---

## 4. 核心概念（5 分钟）

- **项目（Project）**：一个自动化单元的全部配置——目标、标定、资产、检测器、状态机、策略。
- **目标（TargetProfile）**：允许操作的程序。`protected_online: true` 的目标只许观察。
- **策略（PolicyProfile）**：模式与限额——运行时长、每分钟动作数、总动作数、失焦行为、无人值守开关。脚本**不能**自行放宽。
- **检测器（Detector）**：五类——`template_match`（模板）、`color_bar_ratio`（血条/资源条比例）、`color_region`（颜色区域）、`change_stability`（变化/卡死）、`ocr_roi`（事件触发 OCR）。
- **状态机（Machine）**：状态 + 迁移（when 条件表达式）+ 超时 + 有界重试 + 人工闸门；产出的是"意图"而非按键。
- **四种运行模式（风险从低到高）**：
  - `observe` 只采集观察；
  - `shadow` 完整决策并记录"本来会做什么"，零真实输入；
  - `dry_run` 同上，用于调试单步；
  - `real_input` 真实键鼠——**必须人工两步确认**，且全局只允许一个此类会话。
- **轨迹（Trace）**：每次运行的帧引用/感知/迁移/意图/策略/执行事件链（哈希链防篡改），是回放与差异分析的依据。

---

## 5. 标准工作流

### 5.1 第一次跑通（以 ArenaLab 为例）

1. **准备目标画面**：`python -m arena_lab.view happy_path`（弹出 ArenaLab 模拟器窗口）；
2. **复制示例项目**到工作区：把 `examples/arena_lab_demo` 拷进 `<工作区>/projects/`（或界面新建后对照配置）；
3. **TargetSelect**：创建会话，模式选 `shadow`，启动——Inspector 里看感知字段与状态迁移，LivePreview 看画面；
4. **TimelineView**：回看这次运行：哪个字段在哪一帧触发迁移、意图是否产生、策略是否放行；
5. 满意后：把模式切到 `real_input` → 两步人工确认 → ControlBar 启动（仅对允许真实输入的目标可用）。

### 5.2 调一个检测器

1. AssetLibrary 上传/截图模板 → 记下引用关系；
2. DetectorEditor 调阈值/ROI/稳定帧 → 用离线校验与 LivePreview 观察；
3. 借助黄金数据集（命令行，见 §7）量化 precision/recall/MAE，不要凭感觉调参；
4. 修改后先跑回放差异（TestCenter / §7 命令），确认没有回归再发布。

### 5.3 发布与回滚（版本化）

- 发布 = 整体冻结（状态机+资产+阈值+标定+策略一起），签名后不可变；
- 回滚 = 整体恢复到任一历史版本，运行记录仍引用当时的版本；
- 升级失败自动恢复并保留诊断。
- 命令行入口见 §7（release_kit）。

---

## 6. 安全机制速查

| 机制 | 行为 |
|---|---|
| 全局急停 | **Ctrl+Alt+F12**（独立于 UI，UI 卡死也生效）：先停输入、释放全部按键，再清理 |
| 失焦/锁屏/切会话 | 立即停止，不自动抢回焦点；恢复需人工重新确认 |
| 目标复核 | 每批输入前核对前台 HWND/PID/实例，不符即拒（`foreground_mismatch`） |
| 预算 | 每分钟动作数/总动作数/运行时长硬上限，到限不可自动恢复 |
| 人工闸门 | real_input 会话必须显式确认；未确认 start 会被拒绝 |
| 意图 TTL | 过期意图直接丢弃，不补发 |
| 受保护目标 | 三层硬锁（Schema/ModeGate/评估器），改配置文件也绕不过 |
| 看门狗 | 父进程退出/心跳中断 → 自动释放全部按键并拒绝旧会话 |

---

## 7. 命令行工具箱

所有命令在仓库根执行；直跑需 `PYTHONPATH="packages;services;apps"`。

```bash
# —— 演示与自检 ——
python -m arena_lab.view happy_path          # ArenaLab 模拟器窗口
python -m arena_lab.selfcheck                # 渲染确定性自检
python -m arena_lab.e2e_smoke                # 真窗口采集 E2E 冒烟
python -m policy_engine.demo                 # 策略安全五场景演示

# —— 校验 ——
python -m domain_model.validate <项目目录>    # Schema+交叉引用+硬锁校验

# —— 验收门（分级递归）——
python tools/acceptance/run_m0_checks.py     # 6/6
python tools/acceptance/run_m1_checks.py     # 8/8（含 SAFE 矩阵）
python tools/acceptance/run_m2_checks.py     # 9/9（含回放/契约）
python tools/acceptance/run_m3_checks.py     # 8/8（含发布链/恶意包/可重复构建）
python tools/acceptance/run_m45_checks.py    # 8/8（递归全链，约 30 分钟）

# —— 发布/运维（详见 docs/operations/runbook.md）——
python tools/release_packager/build.py --out dist/            # 可重复构建
python tools/release_packager/diagnostics.py --project <dir> --out <zip>   # 脱敏诊断包
python tools/release_packager/regression_pack.py              # 分级回归+报告
python tools/release_packager/first_run_check.py              # 环境检查
```

---

## 8. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 页面 401 | 令牌不对/后端重启换了令牌。用启动时打印的 `?token=` 重新打开，或用 `--token` 固定 |
| 端口被占 | `--port` 换一个；或结束旧 python 进程 |
| `ModuleNotFoundError` | 没注入 PYTHONPATH，见 §2 |
| 桌面窗口打不开 | 需要 WebView2 运行时（Win11 自带）；或先用 `--backend-only` + 浏览器 |
| 创建 real_input 会话被拒 | 目标是受保护在线目标，或已有另一 real_input 会话，或未走人工闸门 |
| 意图被拒 `foreground_mismatch` | 前台窗口不是会话绑定的目标——把目标窗口置前，或重新确认会话 |
| 预算耗尽会话停了 | 策略限额生效，属预期；调大需改项目 policies（不可由运行中会话放宽） |
| 采集黑帧后要重新确认 | 主采集器故障已降级（needs_reconfirm），重新确认目标/标定后才能真实输入 |

---

## 9. 延伸阅读

- `docs/authoring/script-authoring-guide.md`——脚本/检测器/状态机编写权威指南
- `docs/authoring/detector-tuning.md`——调参、黄金数据集与指标门槛
- `docs/operations/runbook.md`——运行与故障处理手册（急停/失焦/降级/恢复）
- `docs/operations/install-upgrade.md`——安装/升级/卸载与备份
- `docs/safety/safety-boundaries.md`、`docs/safety/threat-model.md`——安全边界与威胁模型
- `docs/adr/`——八项架构决策记录
