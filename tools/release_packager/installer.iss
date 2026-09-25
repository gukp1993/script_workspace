; Inno Setup 6 脚本模板（REL-002：安装 / 修复 / 卸载）。
;
; 用法（发布机需安装 Inno Setup 6；开发机编译无需代码签名证书，
; 正式签发由 CI 受保护阶段执行 REL-003，本地产物标记为未签名）：
;
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" tools\release_packager\installer.iss
;
; 要点（与 REL-002 验收口径一致）：
;   - 标准用户权限安装（PrivilegesRequired=lowest），默认装到用户目录；
;   - 卸载：进程提示（CloseApplications）-> 热键随进程退出自动释放的说明
;     -> 临时文件清理；用户项目目录默认保留并弹窗询问（默认保留）。

#define AppName "Windows 前台视觉自动化工作台"
#define AppNameEn "VisionAutoWorkbench"
#define AppVersion "0.1.0"
#define AppPublisher "VisionAutoWorkbench Project"
#define AppExeName "VisionAutoWorkbench.exe"

[Setup]
AppId={{7C1A2E5F-4B8D-4E60-9A3C-VAW000000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppNameEn}
DefaultGroupName={#AppName}
; 标准用户可安装/卸载（REL-002：标准用户权限下安装）
PrivilegesRequired=lowest
OutputDir=..\..\dist\installer
OutputBaseFilename={#AppNameEn}-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 卸载/升级时提示关闭运行中的工作台进程，避免半安装状态
CloseApplications=yes
CloseApplicationsForce=no
; 卸载后保留用户项目目录（见 [Code] 与 UninstallDelete 注释）
UninstallFilesDir={localappdata}\Programs\{#AppNameEn}
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[CustomMessages]
; 卸载完成页附加说明（热键释放 + 项目保留提示）
ExitRunNote=立即启动 %1
UninstallHotkeyNote=全局急停热键（Ctrl+Alt+F12）由工作台进程注册，随进程退出自动释放，无需手工注销。
KeepProjectsQuestion=是否保留用户项目目录（项目、发布历史、备份与运行轨迹）？%n%n选择"是"保留（推荐，默认）；选择"否"删除用户数据（不可恢复，请先导出需要的轨迹/诊断包）。

[Files]
; 源码树与入口（Source 根为仓库根；ISCC 需以仓库根为工作目录或调整相对路径）
Source: "pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "packages\*"; DestDir: "{app}\packages"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc"
Source: "services\*"; DestDir: "{app}\services"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc"
Source: "apps\*"; DestDir: "{app}\apps"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc"
Source: "schemas\*"; DestDir: "{app}\schemas"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "examples\*"; DestDir: "{app}\examples"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc,traces,releases,backups"
Source: "docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "tools\*"; DestDir: "{app}\tools"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc,dist"
; 入口启动器（发布流水线生成：注入包路径后调 python -m desktop_shell；
; 此处仅为占位文件名，构建安装器前由 build 流水线放置到 staging 目录）
Source: "dist\staging\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion; Check: LauncherExists()

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
; 默认不注册任何开机自启/后台服务（安全边界：不允许无人值守形态）

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:ExitRunNote,{#AppName}}"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
; 仅清理应用自身的临时/缓存文件；用户项目目录（工作区）不在此列，默认保留。
; {userdocs} 下的工作区目录由 [Code] 的卸载确认页决定是否删除。
Type: filesandordirs; Name: "{localappdata}\Programs\{#AppNameEn}\__pycache__"

[Code]
// 启动器占位检查：staging 目录没有启动器时跳过该条目（模板可在无启动器时编译）
function LauncherExists(): Boolean;
begin
  Result := FileExists(ExpandConstant('{src}\dist\staging\VisionAutoWorkbench.exe'));
end;

// 卸载：询问是否删除用户项目目录（默认保留）。
// 工作区目录约定存于 {userdocs}\VisionAutoWorkbench（first_run_check --workspace 缺省）。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  WorkspaceDir: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // 热键说明（不弹框阻断，写入卸载日志页即可见）
    Log(ExpandConstant('{cm:UninstallHotkeyNote}'));

    WorkspaceDir := ExpandConstant('{userdocs}\VisionAutoWorkbench');
    if DirExists(WorkspaceDir) then
    begin
      if MsgBox(ExpandConstant('{cm:KeepProjectsQuestion}'),
                mbConfirmation, MB_YESNO, IDYES) = IDNO then
      begin
        // 用户显式选择删除：仅删除用户数据目录；发布历史随之移除前已再次确认
        DelTree(WorkspaceDir, True, True, True);
        Log('用户项目目录已按用户选择删除：' + WorkspaceDir);
      end
      else
        Log('用户项目目录已保留：' + WorkspaceDir);
    end;
  end;
end;

// 安装前检查：工作台进程仍在运行时给出明确指引（CloseApplications 处理常规情况）
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if CheckForMutexes('VisionAutoWorkbenchInstance') then
    Result := '检测到工作台正在运行。请先正常停止所有会话并退出程序（见 docs/operations/runbook.md §1.3），再重试安装。';
end;
