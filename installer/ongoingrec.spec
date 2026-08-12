# PyInstaller spec for OngoingRec.
#
# Build on Windows only -- PyInstaller does not cross-compile, so the .exe
# must come from a Windows machine or a windows-latest CI runner.
#
# onedir rather than onefile: a onefile build unpacks itself to a temp
# directory on every start, which for a service that starts at boot means
# slower startup, a temp directory that antivirus scans repeatedly, and an
# ffmpeg path that changes between runs. A directory install is also far
# easier to inspect when something is wrong on a counsellor's laptop.

import os
from pathlib import Path

block_cipher = None

# ffmpeg and ffprobe are placed here by installer/build.ps1 before the build.
VENDOR = Path(os.environ.get("ONGOINGREC_VENDOR", "vendor"))

binaries = []
for tool in ("ffmpeg.exe", "ffprobe.exe"):
    candidate = VENDOR / tool
    if candidate.exists():
        # Landing at the root of the bundle is what audio.encoder._locate
        # looks for first, so the service never depends on the user's PATH.
        binaries.append((str(candidate), "."))

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    binaries=binaries,
    datas=[],
    hiddenimports=[
        # Pulled in dynamically by the service wrapper and uvicorn, so
        # PyInstaller's static analysis does not see them.
        "win32timezone",
        "servicemanager",
        "win32serviceutil",
        "win32service",
        "win32event",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "_sounddevice_data",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "scipy"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OngoingRec",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A console subsystem binary is required: the Service Control Manager
    # invokes it with no arguments and pywin32's dispatcher needs stdio.
    console=True,
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="OngoingRec",
)
