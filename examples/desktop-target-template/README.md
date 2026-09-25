# 普通桌面应用目标模板（ADP-001）

面向**本地 / 离线 / 自有 / 明确允许自动化**的 Windows 桌面程序的目标适配模板。
包含窗口匹配、标定占位、策略、检测器示例（`color_region` + `template_match`）
与一个可校验的测试状态机（等待就绪 → 单击 → 断言变化 → 停止）。

## 使用步骤

1. 复制本目录为新项目目录，改名 `project.yaml` 中的 `name`。
2. 替换 `targets/desktop-app.yaml` 的 `executable` 与 `title_regex`
   （executable 只能是文件名，不允许路径分隔符）。
3. 在标定向导中对真实目标重新标定，替换 `calibrations/` 占位锚点。
4. 替换检测器 ROI/阈值与 `assets/templates/ready-button.png`（真实截图），
   并用新截图的 SHA-256 更新 `assets/assets.yaml`。
5. 校验通过后再运行：

   ```bash
   export PYTHONPATH="packages"   # Git Bash；CMD 用 set，PowerShell 用 $env:
   python -m domain_model.validate examples/desktop-target-template
   ```

## 适配准入清单（ADP-003，逐项勾选后方可启用 real_input）

启用真实输入（`mode: real_input`）之前，以下五项必须全部确认并留档：

- [ ] **授权依据**：目标程序归本人/本单位所有，或获得书面自动化授权；
        在线服务的用户协议不禁止该类自动化。
- [ ] **风险等级**：评估误操作影响（数据丢失、资金、发布动作等）；
        高风险动作必须有二次确认状态。
- [ ] **可测试环境**：存在可反复重置的测试环境（测试账号/沙箱/本地实例），
        首次接入只在该环境验证。
- [ ] **回滚**：说明停止手段（急停/失焦即停/超时）与失败后的人工恢复步骤。
- [ ] **数据处理**：说明截图/轨迹中包含的数据、保留期限与脱敏方式
        （含窗口标题、账号等个人信息）。

五项中任一项无法确认时，保持 `mode: shadow` 或 `observe`，
不要启用真实输入。

## 保守默认（不得随意放宽）

| 项 | 值 | 说明 |
|---|---|---|
| require_manual_start | true | 每次运行必须人工启动 |
| max_runtime_minutes | 15 | 单次运行时长上限 |
| max_actions_per_minute | 60 | 动作速率上限 |
| max_total_actions | 200 | 单次运行总动作上限 |
| on_focus_lost | stop | 失焦立即停止 |
| unattended_schedule | disabled | 禁止无人值守 |

## 边界

- 本模板不适用于受保护在线目标（`protected_online: true`）——
  那类目标请使用 `examples/protected-online-shadow-template`，
  真实输入会被校验器硬锁拒绝。
- 状态机中 `entry` 的 `mouse_click` 是动作占位（仅结构），
  实际发送受策略闸门与运行时约束。
