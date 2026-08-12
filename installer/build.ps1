# Build OngoingRec-Setup.exe. Run from the repository root on Windows.
#
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1
#
# PyInstaller does not cross-compile, so this must run on Windows (a local
# machine or a windows-latest CI runner). It produces:
#   dist\OngoingRec\           the frozen service and its dependencies
#   installer\Output\OngoingRec-Setup.exe

param(
    [switch]$SkipFfmpeg,
    [switch]$SkipInstaller,
    [string]$FfmpegUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> Building OngoingRec in $root" -ForegroundColor Cyan

# --- 1. Python environment -------------------------------------------------

if (-not (Test-Path ".venv")) {
    Write-Host "==> Creating virtual environment"
    python -m venv .venv
}
$py = Join-Path $root ".venv\Scripts\python.exe"

Write-Host "==> Installing dependencies"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -e ".[windows,dev]"
& $py -m pip install --quiet pyinstaller

# --- 2. ffmpeg -------------------------------------------------------------
# Bundled rather than assumed: the service runs as LocalSystem, whose PATH is
# not the counsellor's, and a missing ffmpeg means no audio is ever encoded.

$vendor = Join-Path $PSScriptRoot "vendor"
New-Item -ItemType Directory -Force -Path $vendor | Out-Null

if (-not $SkipFfmpeg) {
    if (-not (Test-Path (Join-Path $vendor "ffmpeg.exe"))) {
        Write-Host "==> Downloading ffmpeg"
        $zip = Join-Path $env:TEMP "ffmpeg-release.zip"
        $extract = Join-Path $env:TEMP "ffmpeg-extract"
        Invoke-WebRequest -Uri $FfmpegUrl -OutFile $zip -UseBasicParsing
        Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
        Expand-Archive -Path $zip -DestinationPath $extract -Force
        foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
            $found = Get-ChildItem -Path $extract -Recurse -Filter $tool | Select-Object -First 1
            if (-not $found) { throw "$tool not found in the downloaded archive" }
            Copy-Item $found.FullName (Join-Path $vendor $tool) -Force
        }
        Remove-Item $zip, $extract -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "==> ffmpeg ready in $vendor"
} else {
    Write-Host "==> Skipping ffmpeg download (--SkipFfmpeg)" -ForegroundColor Yellow
}

foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
    if (-not (Test-Path (Join-Path $vendor $tool))) {
        Write-Warning "$tool is missing from $vendor; the built service will not be able to record."
    }
}

# --- 3. Freeze -------------------------------------------------------------

Write-Host "==> Running PyInstaller"
$env:ONGOINGREC_VENDOR = $vendor
Remove-Item -Recurse -Force "$root\build", "$root\dist" -ErrorAction SilentlyContinue
& $py -m PyInstaller --noconfirm --distpath "$root\dist" --workpath "$root\build" `
    (Join-Path $PSScriptRoot "ongoingrec.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $root "dist\OngoingRec\OngoingRec.exe"
if (-not (Test-Path $exe)) { throw "expected $exe to exist" }

# Prove the frozen binary actually runs before wrapping it in an installer.
Write-Host "==> Smoke-testing the frozen executable"
& $exe --version
if ($LASTEXITCODE -ne 0) { throw "the frozen executable did not run" }

# --- 4. Installer ----------------------------------------------------------

if ($SkipInstaller) {
    Write-Host "==> Skipping installer (--SkipInstaller)" -ForegroundColor Yellow
    Write-Host "==> Done. Frozen build at dist\OngoingRec\" -ForegroundColor Green
    exit 0
}

$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    Write-Warning "Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php"
    Write-Host "==> Frozen build is at dist\OngoingRec\" -ForegroundColor Green
    exit 0
}

Write-Host "==> Compiling the installer"
& $iscc (Join-Path $PSScriptRoot "ongoingrec.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Write-Host "==> Done: installer\Output\OngoingRec-Setup.exe" -ForegroundColor Green
Write-Host ""
Write-Host "Note: the installer is unsigned. Windows SmartScreen will warn on"
Write-Host "first run until it is signed with your code-signing certificate:"
Write-Host '  signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f cert.pfx installer\Output\OngoingRec-Setup.exe'
