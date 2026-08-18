"""MP3 encoding and probing via a bundled ffmpeg.

Constant bitrate is a deliberate choice. The service is killed abruptly all
the time -- Windows shutdown, battery death, a forced service stop -- and a
CBR MP3 truncated mid-file is still a valid sequence of frames whose duration
can be recovered from its size. A VBR file with a missing Xing header is not.

The Xing header is suppressed for the same reason: writing it requires ffmpeg
to seek back to the start of the file on close, which never happens if the
process is killed, leaving a header that lies about the file's contents.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger(__name__)


class EncoderError(Exception):
    """ffmpeg is missing, failed to start, or exited badly."""


def _bundle_dir() -> Path | None:
    """Directory holding binaries shipped alongside a frozen executable."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return None


def _locate(name: str) -> str:
    """Find ffmpeg/ffprobe, preferring the copy we ship.

    The installed service must not depend on the counsellor's PATH, so the
    bundled binary always wins; PATH is the development fallback.
    """
    exe = f"{name}.exe" if sys.platform == "win32" else name

    override = os.environ.get(f"ONGOINGREC_{name.upper()}")
    if override and Path(override).exists():
        return override

    bundle = _bundle_dir()
    if bundle is not None:
        candidate = bundle / exe
        if candidate.exists():
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    raise EncoderError(
        f"{name} not found. It ships with the installer; for development, "
        f"install ffmpeg or set ONGOINGREC_{name.upper()} to its path."
    )


def ffmpeg_path() -> str:
    return _locate("ffmpeg")


def ffprobe_path() -> str:
    return _locate("ffprobe")


def probe_duration(path: Path) -> float | None:
    """Duration of an audio file in seconds, or None if unreadable.

    Used by the startup repair pass to recover the true length of a segment
    whose process was killed before it could be closed.
    """
    try:
        result = subprocess.run(
            [
                ffprobe_path(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired, EncoderError) as exc:
        log.warning("could not probe %s: %s", path, exc)
        return None
    if result.returncode != 0:
        log.warning("ffprobe failed on %s: %s", path, result.stderr.strip())
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


class Mp3Encoder:
    """A running ffmpeg process consuming raw PCM on stdin.

    One instance per segment. Writing is synchronous and driven by the
    recorder thread; ffmpeg's own buffering is what keeps that cheap.
    """

    def __init__(
        self,
        path: Path,
        *,
        sample_rate: int,
        channels: int,
        bitrate_kbps: int,
    ):
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.channels = channels
        self.bitrate_kbps = bitrate_kbps
        self._proc: subprocess.Popen | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._stderr_thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def command(self) -> list[str]:
        return [
            ffmpeg_path(),
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", "s16le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-i", "pipe:0",
            "-c:a", "libmp3lame",
            "-b:a", f"{self.bitrate_kbps}k",
            "-write_xing", "0",
            "-flush_packets", "1",
            "-y",
            str(self.path),
        ]

    def start(self) -> None:
        if self.running:
            raise EncoderError("encoder already started")
        self.path.parent.mkdir(parents=True, exist_ok=True)

        creationflags = 0
        if sys.platform == "win32":
            # Without this the service spawns a visible console window on the
            # counsellor's desktop every half hour.
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            self._proc = subprocess.Popen(
                self.command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise EncoderError(f"could not start ffmpeg: {exc}") from exc

        # ffmpeg blocks once its stderr pipe fills, which would stall the
        # recorder mid-segment, so it is drained continuously.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="ffmpeg-stderr", daemon=True
        )
        self._stderr_thread.start()
        log.info("encoding to %s at %d kbps", self.path.name, self.bitrate_kbps)

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in iter(proc.stderr.readline, b""):
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr_tail.append(text)
                log.warning("ffmpeg[%s]: %s", self.path.name, text)

    def write(self, pcm: bytes) -> None:
        """Feed PCM to the encoder.

        Raises:
            EncoderError: if ffmpeg has died. The recorder treats this as a
                segment failure and rolls to a new one rather than silently
                discarding audio.
        """
        if self._proc is None or self._proc.stdin is None:
            raise EncoderError("encoder not started")
        if self._proc.poll() is not None:
            raise EncoderError(
                f"ffmpeg exited with {self._proc.returncode}: {self.stderr_tail()}"
            )
        try:
            self._proc.stdin.write(pcm)
        except (BrokenPipeError, OSError) as exc:
            raise EncoderError(f"ffmpeg pipe broke: {exc}; {self.stderr_tail()}") from exc

    def close(self, timeout: float = 15.0) -> None:
        """Flush and shut down cleanly, killing ffmpeg if it will not exit.

        Called on segment rotation and on shutdown, where Windows gives a
        limited grace period -- hence the timeout rather than an open wait.
        """
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.error("ffmpeg did not exit within %.0fs; killing (%s)", timeout, self.path.name)
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        finally:
            if proc.stderr is not None:
                try:
                    proc.stderr.close()
                except OSError:
                    pass
            self._proc = None

    def stderr_tail(self) -> str:
        return " | ".join(self._stderr_tail)
