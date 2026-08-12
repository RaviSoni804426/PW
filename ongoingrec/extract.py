"""Turning a timestamp into an audio clip.

PRD sections 16 to 18. Given a moment, find the segments around it, cut the
requested window out of them, and join the pieces into one file.

The subtle part is what to do about time the recorder never captured -- the
laptop was asleep, the microphone was unplugged, the service was restarting.
Simply concatenating whatever audio exists would silently compress the
timeline: a listener would hear 11:24 run straight into 11:31 with no
indication that six minutes were missing, and anyone reasoning about *when*
something was said would be wrong.

So internal gaps are filled with silence. Offsets within a returned clip then
correspond exactly to real elapsed time, and the gaps are reported alongside
so the caller knows which stretches are absence rather than quiet.

Leading and trailing gaps are trimmed instead of padded. A request made close
to "now" would otherwise return minutes of silence for time that has not
happened yet; the clip's true start and end are reported, so alignment stays
unambiguous.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .audio.encoder import EncoderError, ffmpeg_path
from .config import Config
from .index import Database, Segment
from .logging_setup import get_logger
from .timeutil import ensure_utc, format_utc

log = get_logger(__name__)

MAX_INPUTS = 64

# A segment holds a whole number of samples, so its end can fall a fraction of
# a sample short of the grid boundary the next segment starts on. That is a
# rounding remainder, not a recording hole, and reporting it as a gap would
# put a spurious entry in the metadata of every clip that crosses a boundary.
# Anything below this is absorbed silently; a real interruption -- a sleeping
# laptop, an unplugged microphone, a service restart -- is orders of magnitude
# larger.
GAP_TOLERANCE_SECONDS = 0.05


class NoRecordingError(Exception):
    """No audio exists anywhere in the requested window (PRD section 28: 404)."""


class ExtractionError(Exception):
    """Audio exists but the clip could not be produced (PRD section 28: 500)."""


@dataclass(frozen=True)
class Gap:
    """A stretch inside the clip for which no audio was ever recorded."""

    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def to_dict(self) -> dict:
        return {
            "start": format_utc(self.start),
            "end": format_utc(self.end),
            "seconds": round(self.seconds, 3),
        }


@dataclass
class ClipResult:
    """A produced clip and the facts needed to interpret it."""

    path: Path
    requested_at: datetime
    window_seconds: int
    start: datetime
    end: datetime
    segment_ids: list[int]
    gaps: list[Gap] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def gap_seconds(self) -> float:
        return sum(gap.seconds for gap in self.gaps)

    @property
    def is_partial(self) -> bool:
        """Whether the clip is missing any of what was asked for."""
        return bool(self.gaps) or self.duration_seconds < self.window_seconds - 1.0

    def to_dict(self) -> dict:
        return {
            "requested_at": format_utc(self.requested_at),
            "window_seconds": self.window_seconds,
            "clip_start": format_utc(self.start),
            "clip_end": format_utc(self.end),
            "duration_seconds": round(self.duration_seconds, 3),
            "segment_ids": self.segment_ids,
            "gaps": [gap.to_dict() for gap in self.gaps],
            "gap_seconds": round(self.gap_seconds, 3),
            "partial": self.is_partial,
        }


@dataclass(frozen=True)
class _Piece:
    """One contiguous span of the clip, from a segment or from silence."""

    start: datetime
    end: datetime
    segment: Segment | None = None
    offset_into_segment: float = 0.0

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def plan_clip(
    db: Database, requested_at: datetime, window_seconds: int
) -> tuple[list[_Piece], list[Gap]]:
    """Work out what the clip is made of, without touching any audio.

    Separated from rendering so the window arithmetic -- which is where
    boundary and gap mistakes actually live -- can be tested directly.
    """
    requested_at = ensure_utc(requested_at)
    half = timedelta(seconds=window_seconds / 2)
    window_start = requested_at - half
    window_end = requested_at + half

    segments = db.segments_overlapping(window_start, window_end)
    segments = [s for s in segments if Path(s.file_path).exists()]
    if not segments:
        raise NoRecordingError(
            f"no recording covers {format_utc(requested_at)} "
            f"(window {format_utc(window_start)} to {format_utc(window_end)})"
        )

    pieces: list[_Piece] = []
    gaps: list[Gap] = []
    cursor: datetime | None = None

    for segment in segments:
        available_start = max(segment.start, window_start)
        available_end = min(segment.effective_end, window_end)
        if available_end <= available_start:
            continue  # an open segment whose captured audio has not reached us yet

        if cursor is not None:
            shortfall = (available_start - cursor).total_seconds()
            if shortfall > GAP_TOLERANCE_SECONDS:
                # A real hole between two segments: pad it so everything after
                # stays at its true offset.
                gap = Gap(start=cursor, end=available_start)
                gaps.append(gap)
                pieces.append(_Piece(start=gap.start, end=gap.end))

        pieces.append(
            _Piece(
                start=available_start,
                end=available_end,
                segment=segment,
                offset_into_segment=(available_start - segment.start).total_seconds(),
            )
        )
        cursor = available_end

    if not pieces:
        raise NoRecordingError(
            f"segments exist near {format_utc(requested_at)} but none has captured "
            f"audio in the requested window yet"
        )
    return pieces, gaps


def _build_command(
    pieces: list[_Piece], output: Path, *, sample_rate: int, channels: int, bitrate_kbps: int
) -> list[str]:
    """One ffmpeg invocation: every piece as an input, joined by concat.

    Doing it in a single pass avoids intermediate files and, because concat
    re-encodes, sidesteps the frame-alignment problems that stream-copying
    arbitrary offsets out of MP3 would introduce.
    """
    layout = "mono" if channels == 1 else "stereo"
    command = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin"]

    for piece in pieces:
        if piece.segment is None:
            command += [
                "-f", "lavfi",
                "-t", f"{piece.seconds:.6f}",
                "-i", f"anullsrc=r={sample_rate}:cl={layout}",
            ]
        else:
            command += [
                "-ss", f"{piece.offset_into_segment:.6f}",
                "-t", f"{piece.seconds:.6f}",
                "-i", str(piece.segment.file_path),
            ]

    streams = "".join(f"[{i}:a]" for i in range(len(pieces)))
    filtergraph = (
        f"{streams}concat=n={len(pieces)}:v=0:a=1[joined];"
        f"[joined]aformat=sample_rates={sample_rate}:channel_layouts={layout}[out]"
    )
    command += [
        "-filter_complex", filtergraph,
        "-map", "[out]",
        "-c:a", "libmp3lame",
        "-b:a", f"{bitrate_kbps}k",
        "-write_xing", "0",
        "-y",
        str(output),
    ]
    return command


def extract_clip(
    config: Config,
    db: Database,
    requested_at: datetime,
    *,
    window_seconds: int | None = None,
    output_dir: Path | None = None,
) -> ClipResult:
    """Produce the clip around *requested_at*.

    Raises:
        NoRecordingError: nothing was recorded in the window.
        ExtractionError: audio exists but ffmpeg could not render the clip.
    """
    # Explicitly None, not falsy: a caller passing 0 is making a mistake and
    # must hear about it rather than silently receive the default window.
    if window_seconds is None:
        window_seconds = config.retrieval_window_seconds
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")

    pieces, gaps = plan_clip(db, requested_at, window_seconds)
    if len(pieces) > MAX_INPUTS:
        # Heavy fragmentation means many short recording runs; joining
        # hundreds of inputs is slow and the result is barely usable.
        raise ExtractionError(
            f"window is fragmented across {len(pieces)} pieces, above the "
            f"{MAX_INPUTS} limit; the recording was repeatedly interrupted"
        )

    output_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"clip-{uuid.uuid4().hex}.mp3"

    try:
        command = _build_command(
            pieces,
            output,
            sample_rate=config.sample_rate,
            channels=config.channels,
            bitrate_kbps=config.mp3_bitrate_kbps,
        )
    except EncoderError as exc:
        raise ExtractionError(str(exc)) from exc

    log.info(
        "extracting %.0fs around %s from %d piece(s), %d gap(s)",
        window_seconds,
        format_utc(requested_at),
        len(pieces),
        len(gaps),
    )
    try:
        result = subprocess.run(command, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExtractionError(f"ffmpeg failed to run: {exc}") from exc

    if result.returncode != 0 or not output.exists():
        raise ExtractionError(
            f"ffmpeg exited {result.returncode}: {result.stderr.decode(errors='replace').strip()}"
        )

    return ClipResult(
        path=output,
        requested_at=ensure_utc(requested_at),
        window_seconds=window_seconds,
        start=pieces[0].start,
        end=pieces[-1].end,
        segment_ids=[p.segment.id for p in pieces if p.segment is not None],
        gaps=gaps,
    )
