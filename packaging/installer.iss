; Установщик для Windows (Inno Setup 6). Сборка: ISCC.exe packaging\installer.iss
; Ставится без прав администратора в профиль пользователя.

#define AppVersion "0.1.1"

[Setup]
AppId={{6E1B3C52-7B0A-4C1E-9C55-5A7B1D0E4F21}
AppName=Splitter
AppVersion={#AppVersion}
AppPublisher=Splitter
DefaultDirName={autopf}\Splitter
DefaultGroupName=Splitter
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=SplitterSetup-{#AppVersion}
Compression=lzma2/normal
SolidCompression=no
; приложение с PyTorch/CUDA больше 2 ГБ — установщик делится на setup.exe и .bin-файлы
DiskSpanning=yes
DiskSliceSize=max
UninstallDisplayIcon={app}\Splitter.exe
WizardStyle=modern

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\Splitter\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Splitter"; Filename: "{app}\Splitter.exe"
Name: "{autodesktop}\Splitter"; Filename: "{app}\Splitter.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Splitter.exe"; Description: "{cm:LaunchProgram,Splitter}"; Flags: nowait postinstall skipifsilent
