; Inno Setup script for PW OngoingRec.
;
; This installer is the only time a counsellor is ever asked anything
; (PRD sections 3.1 and 26). It collects the Email ID and Employee ID,
; writes them via `OngoingRec.exe configure`, registers the Windows service
; for automatic start, and starts it. From then on the service loads what was
; written here and never prompts again.
;
; Build with:  iscc installer\ongoingrec.iss   (from agent/)
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
; Skipped on a silent install, where the wizard pages were never filled in.
; Deployment supplies the identity afterwards with an explicit `configure`.
Filename: "{app}\{#ExeName}"; \
  Parameters: "configure --email ""{code:GetEmail}"" --employee-id ""{code:GetEmployeeId}"" --backend-url ""{code:GetBackendUrl}"" --enrollment-key ""{code:GetEnrollmentKey}"" --register --force"; \
  StatusMsg: "Saving configuration and enrolling..."; Flags: runhidden waituntilterminated; Check: NotSilent

Filename: "{app}\{#ExeName}"; Parameters: "--startup auto install"; \
  StatusMsg: "Registering the Windows service..."; Flags: runhidden waituntilterminated

; Recover automatically from a crash rather than leaving a laptop silently
; not recording until someone notices. Windows resets the failure count daily.
Filename: "{sys}\sc.exe"; Parameters: "failure {#ServiceName} reset= 86400 actions= restart/60000/restart/60000/restart/300000"; \
  Flags: runhidden waituntilterminated

Filename: "{sys}\sc.exe"; Parameters: "description {#ServiceName} ""Records microphone audio in 30-minute segments for PW quality review."""; \
  Flags: runhidden waituntilterminated

; Also skipped when silent: an unconfigured service would only fail to start
; and leave a misleading error in the event log. Deployment starts it after
; configuring, with `Restart-Service OngoingRec`.
Filename: "{app}\{#ExeName}"; Parameters: "start"; \
  StatusMsg: "Starting the recording service..."; Flags: runhidden waituntilterminated; \
  Check: NotSilent

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

{ Inno Setup's Pascal Script has no LastDelimiter, so the last dot is found
  by hand. Scanning backwards matters: the dot that has to sit after the @ and
  before the end is the one in the top-level domain, not the first dot in
  something like first.last@pw.live. }
function LastDotPos(S: String): Integer;
var
  I: Integer;
begin
  Result := 0;
  for I := Length(S) downto 1 do
    if S[I] = '.' then
    begin
      Result := I;
      Exit;
    end;
end;

{ The Backend URL field is pre-filled with an example address so the expected
  shape is obvious. Letting that example through is the one mistake that
  produces a laptop which records perfectly and delivers nothing -- and it
  stays invisible until somebody asks for a clip weeks later. }
function IsPlaceholderUrl(S: String): Boolean;
var
  L: String;
begin
  L := Lowercase(S);
  Result := (Pos('example.com', L) > 0) or (Pos('example.org', L) > 0)
         or (Pos('your-backend', L) > 0) or (Pos('<', L) > 0);
end;

function LooksLikeEmail(S: String): Boolean;
var
  AtPos, DotPos: Integer;
begin
  AtPos := Pos('@', S);
  DotPos := LastDotPos(S);
  Result := (AtPos > 1) and (DotPos > AtPos + 1) and (DotPos < Length(S)) and (Pos(' ', S) = 0);
end;

{ Used to skip the steps that only make sense with someone at the keyboard.
  Inno still walks the wizard pages during a silent install, so without this
  the validation below would fail a page nobody could fill in and abort the
  whole deployment. }
function NotSilent: Boolean;
begin
  Result := not WizardSilent();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;

  { A silent install has no one to answer these pages. Its identity arrives
    afterwards from MDM via `OngoingRec.exe configure` (see section 4 of
    docs/windows-setup.md), so validating empty fields here would abort the
    very deployment path that documents them. }
  if NotSilent then
  begin
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
      if IsPlaceholderUrl(GetBackendUrl('')) then
      begin
        MsgBox('That is the example address, not a real backend.' + #13#10#13#10 +
               'Enter the URL your IT team gave you, or clear the field entirely ' +
               'to install this laptop without a backend.', mbError, MB_OK);
        Result := False;
        Exit;
      end;
      { A backend with no key cannot be talked to at all, so this is an error
        rather than a warning someone clicks past. Clearing the URL is the
        supported way to say "record locally only". }
      if (GetBackendUrl('') <> '') and (GetEnrollmentKey('') = '') then
      begin
        MsgBox('An enrollment key is required to enrol with the backend.' + #13#10#13#10 +
               'Without it this laptop would record but never be able to deliver ' +
               'clips. Enter the key, or clear the Backend URL to install without ' +
               'a backend.', mbError, MB_OK);
        Result := False;
        Exit;
      end;
    end;
  end;

  { A service already installed from a previous version would block the file
    copy, so it is stopped and removed before anything is overwritten. This
    has to happen on a silent upgrade too. }
  if CurPageID = wpReady then
  begin
    Exec(ExpandConstant('{sys}\sc.exe'), 'stop {#ServiceName}', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\sc.exe'), 'delete {#ServiceName}', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(2000);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  TmpFile: String;
  Output: AnsiString;
  ResultCode: Integer;
begin
  { The enrolment above runs hidden, so a rejected URL or key would otherwise
    leave no trace at install time -- and the symptom appears weeks later as
    "why has this laptop never sent a clip?". Ask the installed agent what it
    thinks its own state is, and say so plainly while somebody is still here. }
  if (CurStep = ssDone) and NotSilent and (GetBackendUrl('') <> '') then
  begin
    TmpFile := ExpandConstant('{tmp}\ongoingrec-status.txt');
    Exec(ExpandConstant('{cmd}'),
         '/C ""' + ExpandConstant('{app}\{#ExeName}') + '" status > "' + TmpFile + '" 2>&1"',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if LoadStringFromFile(TmpFile, Output) then
    begin
      if Pos('"registered": true', Output) = 0 then
        MsgBox('This laptop could NOT enrol with the backend.' + #13#10#13#10 +
               'Recording is running and audio is being kept on this machine, but ' +
               'clips cannot be delivered until it enrols successfully.' + #13#10#13#10 +
               'Check the backend URL and enrollment key, then run this from an ' +
               'administrator PowerShell:' + #13#10#13#10 +
               '  & "' + ExpandConstant('{app}\{#ExeName}') + '" configure --email ' +
               GetEmail('') + ' --employee-id ' + GetEmployeeId('') +
               ' --backend-url <url> --enrollment-key <key> --register --force' + #13#10 +
               '  Restart-Service OngoingRec',
               mbError, MB_OK);
    end;
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
      { A silent uninstall has nobody to ask, and /SUPPRESSMSGBOXES answers
        the prompt below with its default -- which is Yes. Left to that, an
        MDM-driven uninstall or upgrade would erase every laptop's uncollected
        audio without a word. Keeping it is the only safe reading of silence. }
      if UninstallSilent() then
        Exit;
      if MsgBox('Delete stored recordings and configuration in' + #13#10 +
                DataDir + '?' + #13#10#13#10 +
                'Choose No to keep any audio that has not yet been collected.',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
