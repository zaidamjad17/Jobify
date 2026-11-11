; Inno Setup script: JobApply-Setup.exe installs the PyInstaller folder for the current user (no admin rights) with a
; Start menu entry and a desktop shortcut. Build after PyInstaller:  iscc packaging\windows\installer.iss
#define AppVersion GetEnv("JOBAPPLY_VERSION")
#if AppVersion == ""
  #define AppVersion "0.3.0"
#endif

[Setup]
AppId={{6C1E7B0A-3D52-4E27-9C8B-5A0F1E0B7A11}
AppName=JobApply
AppVersion={#AppVersion}
AppPublisher=JobApply
DefaultDirName={localappdata}\Programs\JobApply
DefaultGroupName=JobApply
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=JobApply-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\JobApply.exe
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\..\dist\JobApply\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\JobApply"; Filename: "{app}\JobApply.exe"
Name: "{group}\Uninstall JobApply"; Filename: "{uninstallexe}"
Name: "{autodesktop}\JobApply"; Filename: "{app}\JobApply.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\JobApply.exe"; Description: "Start JobApply"; Flags: nowait postinstall
