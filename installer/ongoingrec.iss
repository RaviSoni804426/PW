; Inno Setup script for PW OngoingRec.
;
; This installer is the only time a counsellor is ever asked anything
; (PRD sections 3.1 and 26). It collects the Email ID and Employee ID,
; writes them via `OngoingRec.exe configure`, registers the Windows service
; for automatic start, and starts it. From then on the service loads what was
; written here and never prompts again.
;
; Build with:  iscc installer\ongoingrec.iss
; (build.ps1 produces dist\OngoingRec\ first)

#define AppName "PW OngoingRec"
#define AppVersion "0.1.0"
#define AppPublisher "PW"
#define ServiceName "OngoingRec"
#define ExeName "OngoingRec.exe"

[Setup]
AppId={{8F3A6C21-4E5B-4C7E-9C1D-6B2E9A7F4D10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\PW\OngoingRec
DefaultGroupName=PW
DisableProgramGroupPage=yes
OutputBaseFilename=OngoingRec-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The service runs as LocalSystem and writes under ProgramData, so setup must
; be elevated. It also lets the installer register the service directly.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\OngoingRec\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\OngoingRec Status"; Filename: "{cmd}"; \
  Parameters: "/k ""{app}\{#ExeName}"" status"; Comment: "Show recording service status"

[Run]
; Order matters. Configure first so the service has an identity to load;
; install second; start last. A service started before it is configured would
; log an error and stop, which looks like a broken install.
Filename: "{app}\{#ExeName}"; \
  Parameters: "configure --email ""{code:GetEmail}"" --employee-id ""{code:GetEmployeeId}"" --backend-url ""{code:GetBackendUrl}"" --enrollment-key ""{code:GetEnrollmentKey}"" --force"; \
  StatusMsg: "Saving configuration..."; Flags: runhidden waituntilterminated

Filename: "{app}\{#ExeName}"; Parameters: "--startup auto install"; \
  StatusMsg: "Registering the Windows service..."; Flags: runhidden waituntilterminated

; Recover automatically from a crash rather than leaving a laptop silently
; not recording until someone notices. Windows resets the failure count daily.
Filename: "{sys}\sc.exe"; Parameters: "failure {#ServiceName} reset= 86400 actions= restart/60000/restart/60000/restart/300000"; \
  Flags: runhidden waituntilterminated

Filename: "{sys}\sc.exe"; Parameters: "description {#ServiceName} ""Records microphone audio in 30-minute segments for PW quality review."""; \
  Flags: runhidden waituntilterminated

Filename: "{app}\{#ExeName}"; Parameters: "start"; \
  StatusMsg: "Starting the recording service..."; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "{app}\{#ExeName}"; Parameters: "stop"; Flags: runhidden; RunOnceId: "StopService"
Filename: "{app}\{#ExeName}"; Parameters: "remove"; Flags: runhidden; RunOnceId: "RemoveService"

[Code]
var
  IdentityPage: TInputQueryWizardPage;
  BackendPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  IdentityPage := CreateInputQueryPage(wpSelectDir,
    'Counsellor Details',
    'Who is this laptop assigned to?',
    'These are recorded once. The recording service will not ask again, ' +
    'and they are how the central system identifies audio from this laptop.');
  IdentityPage.Add('Email ID:', False);
  IdentityPage.Add('Employee ID:', False);

  BackendPage := CreateInputQueryPage(IdentityPage.ID,
    'Central Backend',
    'Where should this laptop report to?',
    'Supplied by your IT team. The laptop connects outward to this address; ' +
    'nothing connects inward to the laptop.');
  BackendPage.Add('Backend URL:', False);
  BackendPage.Add('Enrollment key:', True);

  BackendPage.Values[0] := 'https://backend.example.com';
end;

function GetEmail(Param: String): String;
begin
  Result := Trim(IdentityPage.Values[0]);
end;

function GetEmployeeId(Param: String): String;
begin
  Result := Trim(IdentityPage.Values[1]);
end;

function GetBackendUrl(Param: String): String;
begin
  Result := Trim(BackendPage.Values[0]);
end;

function GetEnrollmentKey(Param: String): String;
begin
  Result := Trim(BackendPage.Values[1]);
end;

function LooksLikeEmail(S: String): Boolean;
var
  AtPos, DotPos: Integer;
begin
  AtPos := Pos('@', S);
  DotPos := LastDelimiter('.', S);
  Result := (AtPos > 1) and (DotPos > AtPos + 1) and (DotPos < Length(S)) and (Pos(' ', S) = 0);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;

  { Catching a typo here costs one dialog. Catching it later means a laptop
    that records for weeks and cannot be matched to a counsellor. }
  if CurPageID = IdentityPage.ID then
  begin
    if not LooksLikeEmail(GetEmail('')) then
    begin
      MsgBox('Please enter a valid email address, for example abc@example.com.',
        mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if GetEmployeeId('') = '' then
    begin
      MsgBox('Please enter the Employee ID.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;

  if CurPageID = BackendPage.ID then
  begin
    if (GetBackendUrl('') <> '') and (Pos('http', GetBackendUrl('')) <> 1) then
    begin
      MsgBox('The backend URL should start with http:// or https://.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if (GetBackendUrl('') <> '') and (GetEnrollmentKey('') = '') then
    begin
      if MsgBox('No enrollment key was entered. This laptop will record but ' +
                'will not be able to deliver clips to the backend.' + #13#10#13#10 +
                'Continue anyway?', mbConfirmation, MB_YESNO) = IDNO then
      begin
        Result := False;
        Exit;
      end;
    end;
  end;

  { A service already installed from a previous version would block the file
    copy, so it is stopped and removed before anything is overwritten. }
  if CurPageID = wpReady then
  begin
    Exec(ExpandConstant('{sys}\sc.exe'), 'stop {#ServiceName}', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\sc.exe'), 'delete {#ServiceName}', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(2000);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  { Recordings are deliberately not deleted silently. They may be the only
    copy of audio the backend has not collected yet. }
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{commonappdata}\PW\OngoingRec');
    if DirExists(DataDir) then
    begin
      if MsgBox('Delete stored recordings and configuration in' + #13#10 +
                DataDir + '?' + #13#10#13#10 +
                'Choose No to keep any audio that has not yet been collected.',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
