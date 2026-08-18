# The session-0 microphone spike (docs/windows-setup.md section 1).
#
# Answers one question and no others: can a service running as LocalSystem, in
# session 0, open the microphone and record real audio? Some Windows builds and
# privacy configurations allow it, some return silence, some fail outright, and
# the answer decides whether capture can live in the service at all.
#
#   cd agent
#   powershell -ExecutionPolicy Bypass -File installer\session0-spike.ps1 `
#       -FfmpegSource installer\vendor
#
# Must be run from an ADMINISTRATOR PowerShell. It installs a service, records
# for a couple of minutes, measures what came out, and removes the service
# again -- including after a failure, so a machine is never left with a
# half-registered service.
#
# This is a diagnostic, not part of the product: it recycles the throwaway
# SPIKE001 identity and deletes existing recordings under the OngoingRec home,
# so do not point it at an installation whose audio matters.

param(
    [Parameter(Mandatory = $true)][string]$FfmpegSource,  # dir with ffmpeg.exe/ffprobe.exe
    [int]$RecordSeconds = 100,
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Continue"

$py    = Join-Path $Root ".venv\Scripts\python.exe"
$cli   = Join-Path $Root ".venv\Scripts\ongoingrec.exe"
$state = "C:\ProgramData\PW\OngoingRec"
$log   = Join-Path $state "logs\ongoingrec.log"
$recs  = Join-Path $state "recordings"

# ffmpeg is staged where LocalSystem can certainly read it. A copy inside an
# interactive user's profile is not something a service should be relying on.
$vendor = Join-Path $state "bin"

$svcKey = "HKLM:\SYSTEM\CurrentControlSet\Services\OngoingRec"

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

# Service removal only. Staged ffmpeg is cleaned up separately, because this
# also runs *before* the install to clear out an earlier attempt -- at which
# point ffmpeg has already been put in place and must survive.
function Remove-TheService {
    Stop-Service OngoingRec -Force -ErrorAction SilentlyContinue
    & $py -m ongoingrec.service.win_service remove 2>&1 | Out-Null
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "NOT ELEVATED - rerun from an administrator PowerShell." -ForegroundColor Red
    exit 2
}

Section "Environment"
Write-Host "user   : $($identity.Name)"
Write-Host "python : $(& $py --version 2>&1)"
New-Item -ItemType Directory -Force -Path $vendor | Out-Null
foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
    $src = Join-Path $FfmpegSource $tool
    if (-not (Test-Path $src)) { Write-Host "missing $src" -ForegroundColor Red; exit 2 }
    Copy-Item $src (Join-Path $vendor $tool) -Force
}
Write-Host "ffmpeg : $vendor"

Section "Configure a throwaway install"
& $cli configure --email test@pw.live --employee-id SPIKE001 --force

Section "Microphones visible to the interactive user"
& $cli devices

Section "Install and start the service"
# A service left over from an earlier attempt would record into the same
# directory and make the measurement at the end ambiguous.
Remove-TheService
if (Test-Path $recs) { Remove-Item "$recs\*" -Recurse -Force -ErrorAction SilentlyContinue }
if (Test-Path $log) { Remove-Item $log -Force -ErrorAction SilentlyContinue }

& $py -m ongoingrec.service.win_service --startup auto install
if ($LASTEXITCODE -ne 0) { Write-Host "install failed" -ForegroundColor Red; exit 3 }

# Scaffolding, all of it a consequence of running from a virtualenv rather than
# the frozen build -- which is the point, since the spike exists to be run
# before there is an exe. These go on the service's own key because
# services.exe caches the system environment at boot and would not see a
# machine-wide variable.
#
#   PATH        pywin32 relocates pythonservice.exe into .venv\, but
#               python311.dll stays with the base interpreter. Without this the
#               host dies before Python starts and the only symptom is 1053.
#   PYTHONPATH  pythonservice.exe does not read the venv's pyvenv.cfg.
#   ONGOINGREC_FFMPEG  the shipped build finds ffmpeg beside its own exe.
$base = (& $py -c "import sys; print(sys.base_prefix)").Trim()
$site = Join-Path $Root ".venv\Lib\site-packages"
Set-ItemProperty -Path $svcKey -Name "Environment" -Type MultiString -Value @(
    "PATH=$base;$(Join-Path $base 'DLLs');$(Join-Path $Root '.venv\Scripts');$env:SystemRoot\system32;$env:SystemRoot",
    "PYTHONPATH=$Root;$site;$(Join-Path $site 'win32');$(Join-Path $site 'win32\lib');$(Join-Path $site 'Pythonwin')",
    "ONGOINGREC_FFMPEG=$(Join-Path $vendor 'ffmpeg.exe')",
    "ONGOINGREC_FFPROBE=$(Join-Path $vendor 'ffprobe.exe')"
)

$started = $true
try { Start-Service OngoingRec -ErrorAction Stop }
catch { $started = $false; Write-Host "Start-Service failed: $($_.Exception.Message)" -ForegroundColor Red }
Start-Sleep -Seconds 3
Get-Service OngoingRec | Format-Table Name, Status, StartType -AutoSize

if (-not $started) {
    # The service log will not exist yet if the host itself failed to load, so
    # the Windows event log is the only place the reason is recorded.
    Section "Why the service did not start"
    Get-WinEvent -FilterHashtable @{LogName = 'System'; StartTime = (Get-Date).AddMinutes(-5) } -EA SilentlyContinue |
        Where-Object { $_.Message -match 'OngoingRec' } |
        Select-Object TimeCreated, Id, Message | Format-List
    Get-WinEvent -FilterHashtable @{LogName = 'Application'; StartTime = (Get-Date).AddMinutes(-5) } -EA SilentlyContinue |
        Select-Object -First 5 TimeCreated, ProviderName, Id, Message | Format-List
    if (Test-Path $log) { Section "Service log"; Get-Content $log -Tail 40 }
    Remove-TheService
    Remove-Item $vendor -Recurse -Force -ErrorAction SilentlyContinue
    Section "VERDICT"
    Write-Host "INCONCLUSIVE - the service host never ran, so session 0 capture was never exercised." -ForegroundColor Yellow
    exit 4
}

Section "Recording for $RecordSeconds seconds"
Write-Host "Talk near the microphone. Ambient room noise is enough to tell real"
Write-Host "capture from digital silence, but speech makes it unmistakable."
Start-Sleep -Seconds $RecordSeconds

Section "Service state"
& $cli status 2>&1

Section "Log"
if (Test-Path $log) { Get-Content $log -Tail 40 } else { Write-Host "no log at $log" -ForegroundColor Red }

# Stop before measuring: the current segment is still being written, and a
# finalized MP3 is what the extraction path would actually be handed.
Section "Finalizing"
Stop-Service OngoingRec -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Section "Recordings"
# Segments live in a per-day subdirectory, so this has to recurse.
$files = @(Get-ChildItem $recs -Recurse -File -Filter *.mp3 -EA SilentlyContinue | Sort-Object LastWriteTime)
if ($files.Count -eq 0) {
    Write-Host "NO FILES were produced." -ForegroundColor Red
} else {
    $files | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
}

# The decisive measurement. A file of the right size proves the encoder ran,
# not that the microphone did; digital silence sits near -91 dB.
Section "Real audio, or silence?"
$verdict = "UNKNOWN - no segment was produced to measure"
$ff = Join-Path $vendor "ffmpeg.exe"
if ($files.Count -gt 0) {
    $target = $files[-1]
    Write-Host "analysing $($target.Name)"
    $out = & $ff -hide_banner -nostdin -i $target.FullName -af volumedetect -f null NUL 2>&1 | Out-String
    $mean = [regex]::Match($out, "mean_volume:\s*(-?[\d.]+) dB")
    $max = [regex]::Match($out, "max_volume:\s*(-?[\d.]+) dB")
    if ($mean.Success) {
        $m = [double]$mean.Groups[1].Value
        Write-Host "mean_volume: $m dB   max_volume: $(if ($max.Success) { $max.Groups[1].Value } else { '?' }) dB"
        $verdict = if ($m -lt -80) {
            "SILENCE - the service opened the device but captured nothing. Use the capture_in_session_agent fallback."
        } else {
            "REAL AUDIO - session 0 capture works on this machine."
        }
    } else {
        Write-Host $out
        $verdict = "UNKNOWN - ffmpeg could not measure the segment"
    }
}

Section "Cleanup"
Remove-TheService
Remove-Item $vendor -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "service removed."

Section "VERDICT"
Write-Host $verdict -ForegroundColor Yellow
