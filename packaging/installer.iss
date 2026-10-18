; Inno Setup 6 安装脚本：把 PyInstaller 打出来的 dist\AutoSlice 做成安装包。
; 用 packaging\build_windows.ps1 -Installer 调用；单独编译时：ISCC.exe /DAppVersion=0.1.0 packaging\installer.iss

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{8F3A6C1E-4B7D-4E92-A5C0-2D9E71B3F640}
AppName=AutoSlice
AppVersion={#AppVersion}
AppPublisher=AutoSlice
; 装到当前用户目录，不要管理员权限，也不会碰系统目录
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\AutoSlice
DefaultGroupName=AutoSlice
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=AutoSlice-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayName=AutoSlice
#if FileExists(SourcePath + "autoslice.ico")
SetupIconFile=autoslice.ico
#endif

; 官方安装包不带简体中文语言文件（非官方翻译），装了才启用，没有就用默认的英文界面
#if FileExists(CompilerPath + "Languages\ChineseSimplified.isl")
[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "..\dist\AutoSlice\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\AutoSlice"; Filename: "{app}\AutoSlice.exe"
Name: "{autodesktop}\AutoSlice"; Filename: "{app}\AutoSlice.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AutoSlice.exe"; Description: "运行 AutoSlice"; Flags: nowait postinstall skipifsilent

; 卸载只删程序目录；用户数据（%LOCALAPPDATA%\AutoSlice：草稿、缓存、设置、识别模型）一律保留
