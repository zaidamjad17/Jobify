; Inno Setup script: Jobify-Setup.exe installs the PyInstaller folder for the current user (no admin rights) with a
; Start menu entry and a desktop shortcut. Build after PyInstaller:  iscc packaging\windows\installer.iss
#define AppVersion GetEnv("JOBAPPLY_VERSION")
#if AppVersion == ""
  #define AppVersion "0.4.2"
#endif

[Setup]
AppId={{6C1E7B0A-3D52-4E27-9C8B-5A0F1E0B7A11}
AppName=Jobify
AppVersion={#AppVersion}
AppPublisher=Jobify
DefaultDirName={localappdata}\Programs\Jobify
DefaultGroupName=Jobify
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=Jobify-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Jobify.exe
SetupIconFile=jobify.ico
WizardSmallImageFile=wizard-small-55.bmp,wizard-small-110.bmp
UsePreviousGroup=no
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; The app was called JobApply: an update installs into the same folder (same AppId), so drop the old exe and shortcuts
Type: files; Name: "{app}\JobApply.exe"
Type: filesandordirs; Name: "{userprograms}\JobApply"
Type: files; Name: "{userdesktop}\JobApply.lnk"

[Files]
Source: "..\..\dist\Jobify\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Jobify"; Filename: "{app}\Jobify.exe"
Name: "{group}\Uninstall Jobify"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Jobify"; Filename: "{app}\Jobify.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Jobify.exe"; Description: "Start Jobify"; Flags: nowait postinstall
