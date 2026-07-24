#define MyAppName "Ownkey"
#define MyAppVersion "0.3.0"
#define MyAppPublisher "Ownkey"
#define MyAppExeName "backend\\Ownkey.exe"

[Setup]
AppId={{F42503CB-EDB7-4BCB-B739-123F4B75DE6A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://ownkey.bvdm.ai
AppSupportURL=https://github.com/MajesteitBart/ownkey-windows/issues
AppUpdatesURL=https://github.com/MajesteitBart/ownkey-windows/releases
DefaultDirName={autopf}\Ownkey
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist-installer
OutputBaseFilename=Ownkey-Setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no
SetupIconFile=assets\ownkey.ico
WizardImageFile=assets\wizard-large-100.bmp,assets\wizard-large-150.bmp,assets\wizard-large-200.bmp
WizardSmallImageFile=assets\wizard-small-100.bmp,assets\wizard-small-150.bmp,assets\wizard-small-200.bmp
UninstallDisplayIcon={app}\ownkey.ico
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion=0.3.0.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=Ownkey for Windows installer
VersionInfoCopyright=Copyright (C) 2026 Ownkey

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "..\dist\Ownkey\*"; DestDir: "{app}\backend"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\overlay-ui\src-tauri\target\release\ownkey-overlay.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\ownkey.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\USER_INSTALLATION.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\assets\readme\*.png"; DestDir: "{app}\assets\readme"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\ownkey.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\ownkey.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

