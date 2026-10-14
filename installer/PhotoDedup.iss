; 사진 정리 도우미 — Windows 설치 프로그램 (Inno Setup 6)
; GitHub Actions 에서 자동으로 빌드됩니다. 직접 빌드하려면:
;   iscc /DAppVersion=1.0.0 installer\PhotoDedup.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "사진 정리 도우미"
#define AppExe "PhotoDedup.exe"

[Setup]
AppId={{21F14000-7ABD-4040-85AF-BDE90336FF0E}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=HSH88888
AppPublisherURL=https://github.com/HSH88888/file
DefaultDirName={autopf}\PhotoDedup
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 관리자 권한 없이 내 계정에만 설치 (설치 화면에서 '모든 사용자'로 바꿀 수 있음)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=PhotoDedup-Setup-{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force

[Languages]
#if FileExists(AddBackslash(CompilerPath) + "Languages/Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#else
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "바로가기:"

[Files]
Source: "..\dist\PhotoDedup\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "지금 {#AppName} 실행"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "KillApp"

; 분석 결과·삭제 기록(%LOCALAPPDATA%\PhotoDedup)은 제거해도 남겨 둡니다.
