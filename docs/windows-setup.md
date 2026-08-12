# Running OngoingRec on Windows

Everything except this document has been developed and tested on macOS. What
follows is the part that can only be verified on a real Windows machine, in
the order it should be done.

**Do step 1 before anything else.** It is the only thing that can invalidate
the architecture, and finding out at packaging time is far more expensive than
finding out now.

---

## 1. The session-0 microphone spike

### Why this matters

A Windows service runs in session 0, isolated from the interactive desktop.
Audio endpoints are associated with user sessions, and the "Allow desktop apps
to access your microphone" privacy setting governs capture. It is genuinely
uncertain whether a LocalSystem service can open a microphone on a given
Windows build and configuration — it works on some, returns silence on
others, and fails outright on others again.

Everything else in this project is independent of the answer. This one thing
is not, so it gets tested first.

### The test

On the target laptop, with Python 3.11+ installed:

```powershell
git clone <repo> C:\src\PW
cd C:\src\PW
python -m venv .venv
.venv\Scripts\pip install -e ".[windows,dev]"

# Configure a throwaway install
.venv\Scripts\ongoingrec configure --email test@pw.live --employee-id SPIKE001

# Confirm a microphone is visible at all, as your own user
.venv\Scripts\ongoingrec devices
```

Now run it as the service, which is the actual question:

```powershell
# From an ADMINISTRATOR PowerShell
.venv\Scripts\python -m ongoingrec.service.win_service --startup auto install
Start-Service OngoingRec
Start-Sleep -Seconds 90
Get-Service OngoingRec
```

Then check what it captured:

```powershell
Get-Content C:\ProgramData\PW\OngoingRec\logs\ongoingrec.log -Tail 30
.venv\Scripts\ongoingrec status
```

### Reading the result

**It works** if the log shows `capturing from <device name>` followed by
`segment 1 opened`, and files appear under
`C:\ProgramData\PW\OngoingRec\recordings\`. Play one back and confirm it
contains real audio, not silence — a file of the right size proves the
encoder ran, not that the microphone did.

**It does not work** if the log shows `could not open`, `no input devices`, or
segments that are the right length but silent.

### If it does not work

The fallback is already designed for. The service keeps its lifecycle,
polling and API duties, and the capture engine runs as a separate process in
the interactive session. Set in `C:\ProgramData\PW\OngoingRec\config.json`:

```json
{ "capture_in_session_agent": true }
```

and register the capture agent as a scheduled task that starts at logon:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Program Files\PW\OngoingRec\OngoingRec.exe" -Argument "run --no-api"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -GroupId "Users" -RunLevel Highest
Register-ScheduledTask -TaskName "OngoingRecCapture" -Action $action -Trigger $trigger -Principal $principal
```

Tell me the outcome either way — if the fallback is needed, the service side
needs a small change to stop trying to capture in-process and to supervise the
agent instead.

### Also check, whichever way it goes

Windows privacy settings can silently return silence rather than an error:

* Settings → Privacy & security → Microphone → **Microphone access**: on
* **Let desktop apps access your microphone**: on

These are per-machine and per-user. Confirm them on the actual counsellor
image, not just on a developer laptop.

---

## 2. Building the installer

Needs, on the build machine:

* Python 3.11+
* [Inno Setup 6](https://jrsoftware.org/isdl.php)
* Internet access the first time, to fetch ffmpeg

```powershell
cd C:\src\PW
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

This produces `installer\Output\OngoingRec-Setup.exe`. The script downloads
ffmpeg, freezes the service with PyInstaller, smoke-tests the resulting
executable, and compiles the installer.

`ffmpeg.exe` and `ffprobe.exe` are bundled deliberately. The service runs as
LocalSystem, whose PATH is not the counsellor's, so relying on an installed
ffmpeg would produce a service that starts cleanly and records nothing.

### Code signing

The installer is unsigned, so SmartScreen will warn on first run and some
managed environments will block it entirely. Before any real rollout:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f cert.pfx installer\Output\OngoingRec-Setup.exe
```

---

## 3. Acceptance walkthrough

This covers PRD section 27 end to end. Run it on a clean machine.

### Installation asks once

1. Run `OngoingRec-Setup.exe` as administrator.
2. Enter an Email ID and Employee ID. Confirm a malformed email is rejected.
3. Enter the backend URL and enrollment key.
4. Finish. Confirm the service is running:
   ```powershell
   Get-Service OngoingRec        # Status: Running, StartType: Automatic
   ```

### It never asks again

5. Reboot. Log in. Confirm **no prompt of any kind appears** and recording has
   resumed:
   ```powershell
   Get-Service OngoingRec
   curl http://127.0.0.1:8765/health
   ```
   `recording` should be `true` and `registered` should be `true`.
6. Reboot a second time and check again. Twice matters: a value cached in
   memory from installation survives one restart and not two.

### Segments are on the grid

7. Leave it running for at least an hour, then:
   ```powershell
   dir C:\ProgramData\PW\OngoingRec\recordings\*
   ```
   Filenames are UTC (`11-00-00Z.mp3`) and land on `:00` and `:30`. The first
   segment after any start is deliberately short — it runs only to the next
   boundary, which is what puts everything after it on the grid.

### Shutdown finalizes the current segment

8. While a segment is mid-recording, `shutdown /r /t 0`.
9. After the reboot, confirm the log shows the segment was closed cleanly and
   that the file plays to its stated length:
   ```powershell
   Select-String -Path C:\ProgramData\PW\OngoingRec\logs\ongoingrec.log -Pattern "shutting down|closed"
   ```
   The preshutdown handler is what makes this work; if you see a `truncated`
   status instead, the grace period was too short and it is worth telling me.

### Sleep produces a gap, not a lie

10. Close the lid for five minutes. Reopen.
11. Request a clip spanning the sleep and confirm it reports a gap of roughly
    the right length rather than silently joining the two sides together:
    ```powershell
    .venv\Scripts\ongoingrec fetch "2026-08-12T14:30:00" --window-seconds 900 --output C:\temp\clip.mp3
    ```

### Retrieval works, both ways

12. Local, which proves recording and extraction:
    ```powershell
    curl -X POST http://127.0.0.1:8765/recordings/fetch `
      -H "Content-Type: application/json" `
      -d '{\"employee_id\":\"EMP001\",\"timestamp\":\"2026-08-12T11:22:15\"}' `
      --output C:\temp\clip.mp3
    ```
13. Through the backend, which proves the whole product. Queue a request and
    confirm the clip arrives.

### Recovery

14. Kill the service abruptly and confirm it comes back on its own:
    ```powershell
    Stop-Process -Name OngoingRec -Force
    Start-Sleep -Seconds 90
    Get-Service OngoingRec
    ```
    The installer configures Windows to restart it after 60 seconds. Confirm
    the interrupted segment was recovered — the log should show
    `recovered segment N` with a sensible duration rather than the file being
    written off.

---

## 4. Fleet rollout notes

**Do not clone a machine that has already registered.** `install_id` is
generated at configure time, so every clone would share one identity and the
backend would route all their jobs to whichever laptop polled last. Image the
machine *before* installing, or run
`ongoingrec configure --force ...` on each clone to issue a fresh identity.

**Deploy silently** with:

```powershell
OngoingRec-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES
```

Note that the silent install has no way to ask for the Email ID and Employee
ID, so it will install unconfigured. For fleet deployment, run `configure`
explicitly afterwards with values from your MDM inventory:

```powershell
& "C:\Program Files\PW\OngoingRec\OngoingRec.exe" configure `
    --email $email --employee-id $employeeId `
    --backend-url https://backend.pw.live --enrollment-key $key --register --force
Restart-Service OngoingRec
```

`--register` makes a bad URL or key fail during deployment rather than
silently at 3 a.m.

**Antivirus.** A background process continuously writing audio files and
opening the microphone is exactly what endpoint protection is built to flag.
Get `C:\Program Files\PW\OngoingRec\` and
`C:\ProgramData\PW\OngoingRec\` allow-listed before rollout, not after the
first support ticket.

**Disk.** At 32 kbps a full 8-hour day is roughly 115 MB, and the default
72-hour retention holds around 350 MB. Both are configurable in
`config.json` (`retention_hours`, `min_free_disk_mb`).
