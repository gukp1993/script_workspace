# 安装 / 升级 / 卸载指南（REL-002/005 文档面）

| 项 | 内容 |
|---|---|
| 状态 | 已发布（M3 评审基线） |
| 日期 | 2026-09-25 |
| 关联任务 ID | REL-002、REL-005、VER-009、SEC-006、UAT-010 |
| 里程碑 | M3 |
| 来源 | 《任务与测试验收方案》§4 E14、§12.4、§13 UAT-010 |
| 关联文档 | `docs/operations/runbook.md`（运行与回滚）、`tools/release_packager/installer.iss`、`tools/release_packager/first_run_check.py` |

本指南覆盖标准用户权限下的安装、升级（含失败恢复）、备份/恢复与卸载。
安装器脚本为 Inno Setup 模板 `tools/release_packager/installer.iss`
（发布机上用 Inno Setup 6 编译，开发机无需持有证书）。

---

## 1. 安装

### 1.1 安装器方式（推荐）

1. 获取候选版安装包（`tools/release_packager/build.py --out dist/` 产出的
   源码 zip 与 SHA256SUMS，或由其构建的 Inno Setup 安装器）；
2. **先校验**：对照 `SHA256SUMS` 核对安装包哈希；导入项目包前安装器/工作台
   会再做 manifest 签名与哈希预检（未知签名默认不启用 real_input）；
3. 以标准用户身份运行安装器：
   - 默认安装到 `%LOCALAPPDATA%\Programs\VisionAutoWorkbench`（无需管理员）；
   - **用户项目目录默认保留且不迁移**（见 §4 备份策略）；
   - 可勾选"创建桌面快捷方式"与"开机不自启"（默认不自启）；
4. 安装完成 → 首次启动检查（§2）。

### 1.2 ISS 脚本用法（发布工程师）

```bat
:: 编译安装器（本机需安装 Inno Setup 6；开发机编译无需代码签名证书）
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" tools\release_packager\installer.iss
```

脚本要点（与 REL-002 验收口径一致）：

- 安装 `apps/services/packages/schemas/examples/docs` 与入口启动器；
- 卸载时：提示确认 → 释放全局热键与残留进程（列出 `desktop_shell` /
  `control_plane` 检查项）→ 清理临时文件；**用户项目目录默认保留**并弹窗
  询问（默认"保留"）；
- `CloseApplications` 关闭提示对运行中的工作台进程生效，避免文件占用导致
  半安装状态。

---

## 2. 首次启动检查（REL-005）

安装/升级后首启前执行：

```bash
python tools/release_packager/first_run_check.py
```

检查项与处置：

| 检查项 | FAIL 处置 |
|---|---|
| 工作区目录可写 | 检查磁盘空间/只读属性/杀软拦截；或用 `--workspace` 指定可写目录 |
| Python 版本 | 需 3.12.x；安装对应版本后重跑 |
| 依赖可导入 | 按 `requirements.txt` 补齐（见 README 开发环境节） |
| 显示器可用 | 无真实显示器的会话只能 `--backend-only`；E2E/真输入需真实显示器 |
| 旧版本迁移状态 | 迁移未完成/失败时先按 §3.3 恢复，不要强行启动 |

全部 PASS 再启动 `python -m desktop_shell`。

---

## 3. 升级与失败恢复（VER-009，AC-P0-14）

### 3.1 标准升级流程

1. 停止所有运行中会话并正常退出工作台（§1.3 of runbook）；
2. 运行新版安装器——安装器/升级流程自动执行"升级前备份"
   （等价 `ReleaseRollback.create_backup(label="pre-upgrade")`，
   落在 `<项目>/backups/<时刻>-pre-upgrade/`）；
3. 执行迁移（版本兼容矩阵不通过会禁止启动并给出迁移建议，VER-010）；
4. 首启检查（§2）→ 打开既有项目验证（UAT-010：旧项目可正常打开、不丢数据）。

### 3.2 迁移失败自动恢复

迁移按步骤执行（`MigrationStepError` 携带步骤名）；任一步失败：

- 工作区被**自动恢复为升级前状态**（`ReleaseRollback.upgrade_with_backup`
  逐文件同步回备份），并记录失败步骤；
- 恢复后的旧版本可正常启动（UAT-010 验收口径）。

### 3.3 手工恢复（升级器本身也坏了的时候）

1. 关闭全部工作台进程；
2. 找到最近备份：`backups/<时间戳>-pre-upgrade/`（含
   `backup_meta.json`，记录原内容哈希与文件清单）；
3. 将备份内文件整体复制回项目目录（覆盖），删除新增的未知文件；
4. 回退安装：卸载新版后重装旧版安装包（哈希对照当时发布的 SHA256SUMS）；
5. `first_run_check.py` → 启动验证。

---

## 4. 备份与恢复

| 对象 | 位置 | 说明 |
|---|---|---|
| 升级/回滚自动备份 | `<项目>/backups/<时刻>-<label>/` | 冻结规则自动排除，不进入发布单元 |
| 发布历史 | `<项目>/releases/v<序号>-<短哈希>/` | 只增不删（回滚锚点，勿手工改动） |
| 用户项目 | 工作区项目目录 | 卸载默认保留 |

恢复操作即"把对应目录内容复制回项目目录 + 完整性校验"；整体回滚到某个
发布用 runbook §9 的 `rollback_to`（含校验，优先于手工复制）。

---

## 5. 卸载

标准用户"设置 → 应用"中卸载，或运行安装器自带卸载入口。清单：

1. 进程提示：若工作台仍在运行，安装器会提示关闭（`CloseApplications`）；
   手工卸载先确认 `desktop_shell` / `control_plane` 进程已退出；
2. 热键释放：全局急停热键（Ctrl+Alt+F12）随进程退出自动注销；若卸载后
   热键仍无响应（被其他程序占用），无需处理——工作台从未"抢注"系统热键；
3. 临时文件清理：安装目录与 `%TEMP%` 下本应用临时文件随卸载删除；
4. **用户项目默认保留**：卸载弹窗中默认勾选"保留用户项目目录"；只有显式
   选择"删除"才会清理项目数据——删除前请先导出需要的轨迹/诊断包；
5. 残留检查（可选）：`%LOCALAPPDATA%\Programs\VisionAutoWorkbench` 应为空
   或不存在；项目目录按用户选择保留或删除。

---

## 6. 常见问题

| 现象 | 处置 |
|---|---|
| 安装器报"文件被占用" | 关闭工作台进程后重试；安装器会先提示 CloseApplications |
| 升级后旧项目打不开 | 兼容矩阵拦截（VER-010），按提示迁移；迁移失败走 §3.2/3.3 |
| 卸载后急停热键失效 | 正常——热键由工作台注册，卸载即释放；确认没有其他程序依赖它 |
| 首启检查"显示器不可用" | 无头环境仅支持 `--backend-only`，见 runbook §1.1 |
