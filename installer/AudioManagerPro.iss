; ---------------------------------------------------------------------------
; Instalador de Audio Manager Pro (Inno Setup 6)
;
; No compilar a mano: usar build.ps1, que primero genera dist\AudioManagerPro
; con PyInstaller y le pasa a este script la versión/nombre de version.py.
;
; Qué hace:
;   * Instala por usuario (sin pedir administrador) en
;     %LOCALAPPDATA%\Programs\Audio Manager Pro. Con /ALLUSERS en la línea de
;     comandos instala para todos en Program Files.
;   * Acceso en el menú Inicio (y opcionalmente en el escritorio).
;   * Tarea "Iniciar con Windows": valor en HKCU\...\Run (el mismo que maneja
;     la app desde Configuración). En actualizaciones no se toca: se respeta
;     lo que el usuario haya elegido en la app.
;   * Cierra la app en ejecución (--quit) antes de actualizar/desinstalar.
;   * Al desinstalar quita el inicio automático y pregunta si borrar la
;     configuración del usuario.
; ---------------------------------------------------------------------------

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef MyAppName
  #define MyAppName "Audio Manager Pro"
#endif
#ifndef MyAppPublisher
  #define MyAppPublisher "Fama Roman"
#endif
#define MyAppExeName "AudioManagerPro.exe"
#define MyAppDistDir "..\dist\AudioManagerPro"
; ¡No cambiar nunca! Identifica la app para actualizar/desinstalar.
#define MyAppId "{{E8441EE5-2B9D-4F36-AD94-846E6A0D65F8}"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"
#define ApprovedKey "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
#define RunValue "AudioManagerPro"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription=Instalador de {#MyAppName}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
UsedUserAreasWarning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist\installer
OutputBaseFilename=AudioManagerPro-Setup-{#MyAppVersion}
SetupIconFile=..\resources\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
; Si --quit no alcanzara, usa el Administrador de reinicio de Windows.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
spanish.StartupGroup=Inicio:
english.StartupGroup=Startup:
spanish.AutoStartTask=Iniciar %1 con Windows (en la bandeja del sistema)
english.AutoStartTask=Start %1 with Windows (in the system tray)
spanish.DeleteUserData=¿Borrar también la configuración (reglas) y los registros de %1?
english.DeleteUserData=Also delete the %1 settings (rules) and logs?

[Tasks]
Name: "autostart"; Description: "{cm:AutoStartTask,{#MyAppName}}"; GroupDescription: "{cm:StartupGroup}"; Check: not IsUpgrade
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Una versión anterior puede dejar DLLs viejas en _internal.
Type: filesandordirs; Name: "{app}\_internal"
; Acceso directo manual de la carpeta Inicio que se usaba antes del
; instalador: si quedara, la app se abriría dos veces al iniciar sesión.
Type: files; Name: "{userstartup}\AudioManagerPro.lnk"; Tasks: autostart

[Files]
Source: "{#MyAppDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; La GPLv3 exige distribuir el texto de la licencia junto con los binarios.
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Inicio con Windows (mismo valor que maneja la app en Configuración).
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "{#RunValue}"; ValueData: """{app}\{#MyAppExeName}"" --minimized"; Tasks: autostart
; Si Windows lo tenía marcado como deshabilitado, se habilita.
Root: HKCU; Subkey: "{#ApprovedKey}"; ValueType: none; ValueName: "{#RunValue}"; Flags: deletevalue dontcreatekey; Tasks: autostart
; Al desinstalar se quita siempre (aunque se haya activado luego desde la app).
Root: HKCU; Subkey: "{#RunKey}"; ValueType: none; ValueName: "{#RunValue}"; Flags: uninsdeletevalue dontcreatekey
Root: HKCU; Subkey: "{#ApprovedKey}"; ValueType: none; ValueName: "{#RunValue}"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Cierra la app (si está en la bandeja) antes de borrar sus archivos.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--quit"; Flags: waituntilterminated skipifdoesntexist; RunOnceId: "QuitRunningApp"

[Code]
function IsUpgrade: Boolean;
var
  Key, S: String;
begin
  Key := ExpandConstant('Software\Microsoft\Windows\CurrentVersion\Uninstall\{#MyAppId}_is1');
  Result := RegQueryStringValue(HKCU, Key, 'UninstallString', S) or
            RegQueryStringValue(HKLM, Key, 'UninstallString', S);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  ExePath: String;
begin
  Result := '';
  // Actualización: se cierra la versión en ejecución para poder reemplazarla.
  ExePath := ExpandConstant('{app}\{#MyAppExeName}');
  if FileExists(ExePath) then
    Exec(ExePath, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\AudioManagerPro');
    if DirExists(DataDir) and not UninstallSilent then
      if MsgBox(FmtMessage(CustomMessage('DeleteUserData'), ['{#MyAppName}']),
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
