# 受保护在线目标 Shadow 模板（ADP-002）

为**受保护在线目标**（`protected_online: true`）提供的项目模板。
只允许四类活动：

> **观察 / 标注 / 回放 / Shadow Mode**

不承诺、不支持、也不作为验收目标的包括：无人值守运行、真实输入、
自动重连、账号保活、任何逃避检测的手段。

## 硬编码安全基线

| 项 | 值 | 语义 |
|---|---|---|
| targets/*.yaml `protected_online` | true | `real_input_allowed` 派生恒为 false（ADP-002 的 real_input=false） |
| policies/default.yaml `mode` | shadow | 硬编码；改 `real_input` 会被校验器硬锁拒绝 |
| policies/default.yaml `unattended_schedule` | disabled | 硬编码；改 `enabled` 会被校验器硬锁拒绝 |
| auto_reconnect | 无此字段 | 本工作台不实现自动重连/保活——等价 false（ADP-002） |

这些不是"默认值"而是**硬锁**：`domain_model` 校验器在检测到项目含
受保护在线目标时，对一切策略强制执行
`protected_online_no_real_input` / `protected_online_no_unattended`
两条规则，无论文件怎么改名、加多少个策略文件。

## 自验篡改必拒（模板自带的安全测试语义）

```bash
export PYTHONPATH="packages"   # Git Bash
python -m domain_model.validate examples/protected-online-shadow-template   # VALID

# 手工把 mode 改成 real_input 后必须拒绝：
#   INVALID ... protected_online_no_real_input
# 手工把 unattended_schedule 改成 enabled 后必须拒绝：
#   INVALID ... protected_online_no_unattended
```

`tests/unit/test_target_templates.py` 固化了上述断言。

## 使用方式

1. 复制本目录为新项目，把 `targets/protected-online-app.yaml` 的
   `executable` / `title_regex` 替换为真实目标。
2. 只用工作台的观察、标注、回放与 Shadow 会话功能。
3. 任何要求"改成 real_input 跑一次"的需求，都属于准入范围之外——
   请走 `examples/desktop-target-template` 的准入清单流程，并确认目标
   不是受保护在线目标。
