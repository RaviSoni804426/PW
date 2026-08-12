"""Continuous recording split into wall-clock-aligned segments.

PRD sections 6 and 7. The recorder owns one invariant, and everything here
exists to protect it:

    For any segment, the audio at offset ``d`` seconds from the start of the
    file is the audio that was captured at ``segment.start + d``.

That is what makes retrieval trustworthy. It survives buffer boundaries not
lining up with segment boundaries (buffers are split sample-exactly), audio
lost to an overloaded machine (the hole is filled with silence so later audio
stays in place), and the system clock moving underneath a running segment
(elapsed time comes from sample counts, never from clock subtraction).

Segments that are simply absent -- laptop asleep, microphone unplugged,
service stopped -- are represented by nothing at all. The index has no rows
for that period and the extractor pads it, which keeps a real recording hole
distinguishable from recorded silence.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

from .audio.capture import BYTES_PER_SAMPLE, AudioSource, CaptureError, silence
from .audio.devices import NoInputDeviceError
from .audio.encoder import EncoderError, Mp3Encoder, probe_duration
from .config import Config
from .index import STATUS_COMPLETE, STATUS_MISSING, STATUS_TRUNCATED, Database
from .logging_setup import get_logger
from .timeutil import next_boundary, utcnow

log = get_logger(__name__)

# How far the captured audio may fall behind the wall clock before the
# recorder concludes audio was lost and fills the hole. Below this, the
# difference is just buffering.
DRIFT_TOLERANCE_SECONDS = 0.5

# A discrepancy this large is a clock step or a suspended machine rather than
# dropped buffers, so the recorder re-anchors instead of padding.
DRIFT_REANCHOR_SECONDS = 30.0

RESYNC_INTERVAL_SECONDS = 15.0
PROGRESS_CHECKPOINT_SECONDS = 30.0

# A segment shorter than this is a rounding artefact rather than a recording.
# Rather than emit a sliver, the remainder is folded into the following
# segment, which then runs slightly long -- harmless, because the index
# records each segment's true start and duration.
MIN_SEGMENT_SECONDS = 1.0

# Device retry backoff, in seconds, then steady state at the last value.
RETRY_BACKOFF = (1, 2, 5, 10, 30, 60)


def segment_path(recordings_dir: Path, start: datetime) -> Path:
    """Where a segment starting at *start* is stored.

    Named in UTC with an explicit ``Z``. Local naming would be friendlier to
    browse, but on a DST fall-back day it produces two different segments with
    identical names; the index is UTC regardless, so making the filenames
    match it removes a whole class of ambiguity during support.
    """
    return recordings_dir / start.strftime("%Y-%m-%d") / (start.strftime("%H-%M-%S") + "Z.mp3")


class SegmentWriter:
    """One segment: an ffmpeg process, an index row, and a sample count.

    ``capacity_samples`` is the exact number of samples between this segment's
    start and the next boundary, so a segment that begins mid-window (a
    service start at 11:07) correctly runs short to 11:30 and puts the next
    one back on the grid.
    """

    def __init__(
        self,
        *,
        config: Config,
        db: Database,
        start: datetime,
        boundary_end: datetime,
    ):
        self.config = config
        self.db = db
        self.start = start
        self.boundary_end = boundary_end
        self.path = segment_path(config.recordings_dir, start)
        self.samples_written = 0
        self.capacity_samples = max(
            1, int(round((boundary_end - start).total_seconds() * config.sample_rate))
        )
        self._last_checkpoint = 0

        self.encoder = Mp3Encoder(
            self.path,
            sample_rate=config.sample_rate,
            channels=config.channels,
            bitrate_kbps=config.mp3_bitrate_kbps,
        )
        self.encoder.start()
        self.segment_id = db.open_segment(
            install_id=config.install_id,
            email_id=config.email_id,
            employee_id=config.employee_id,
            start=start,
            sample_rate=config.sample_rate,
            channels=config.channels,
            file_path=self.path,
        )
        log.info(
            "segment %d opened: %s -> %s (%s)",
            self.segment_id,
            start.isoformat(),
            boundary_end.isoformat(),
            self.path.name,
        )

    @property
    def remaining_samples(self) -> int:
        return max(0, self.capacity_samples - self.samples_written)

    @property
    def is_full(self) -> bool:
        return self.samples_written >= self.capacity_samples

    def write(self, pcm: bytes, sample_count: int) -> None:
        self.encoder.write(pcm)
        self.samples_written += sample_count
        if self.samples_written - self._last_checkpoint >= (
            PROGRESS_CHECKPOINT_SECONDS * self.config.sample_rate
        ):
            self.db.update_progress(self.segment_id, self.samples_written)
            self._last_checkpoint = self.samples_written

    def close(self, status: str = STATUS_COMPLETE, timeout: float = 15.0) -> None:
        self.encoder.close(timeout=timeout)
        self.db.close_segment(self.segment_id, sample_count=self.samples_written, status=status)
        log.info(
            "segment %d closed: %.1fs of audio (%s)",
            self.segment_id,
            self.samples_written / self.config.sample_rate,
            status,
        )


class Recorder:
    """Drives capture into segments until asked to stop.

    Runs on its own thread. :meth:`stop` is safe to call from a signal handler
    or a Windows service control handler; it finalizes the open segment so the
    audio recorded up to shutdown remains retrievable (PRD section 19).
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        source_factory,
        *,
        resync_to_wall_clock: bool = True,
    ):
        self.config = config
        self.db = db
        self.source_factory = source_factory
        # Off for synthetic sources that generate faster than real time, where
        # comparing captured audio to the wall clock is meaningless.
        self.resync_to_wall_clock = resync_to_wall_clock

        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread: threading.Thread | None = None
        self._writer: SegmentWriter | None = None
        self._source: AudioSource | None = None

        self._stream_start: datetime | None = None
        self._next_segment_start: datetime | None = None
        self._captured_samples = 0
        self._padded_samples = 0
        self._last_resync = 0.0
        self._error: str | None = None
        self._lock = threading.Lock()

    # -- status ------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._writer is not None and self._source is not None

    @property
    def current_segment_id(self) -> int | None:
        writer = self._writer
        return writer.segment_id if writer else None

    def request_restart(self) -> None:
        """Close the current segment and reopen capture from scratch.

        Used when the machine suspends or resumes. Continuing the old segment
        across a sleep would append post-wake audio to a pre-sleep file, so
        every sample after the join would carry the wrong time -- exactly the
        error the whole design exists to prevent. Reopening instead leaves the
        sleep interval as a gap, which is what actually happened.
        """
        self._restart.set()
        source = self._source
        if source is not None:
            # Unblocks a read that is waiting on a device which may itself be
            # suspended and will never deliver another buffer.
            source.stop()

    def status(self) -> dict:
        writer = self._writer
        return {
            "recording": self.is_recording,
            "device": self._source.describe() if self._source else None,
            "current_segment_id": writer.segment_id if writer else None,
            "current_segment_start": writer.start.isoformat() if writer else None,
            "captured_seconds": round(self._captured_samples / self.config.sample_rate, 1),
            "padded_seconds": round(self._padded_samples / self.config.sample_rate, 1),
            "last_error": self._error,
        }

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("recorder already started")
        repair_open_segments(self.config, self.db)
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 20.0) -> None:
        self._stop.set()
        if self._source is not None:
            self._source.stop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.error("recorder thread did not stop within %.0fs", timeout)
        self._close_writer()
        self._thread = None

    def run_until_source_exhausted(self) -> None:
        """Run the loop inline until the source stops producing.

        The synthetic path uses this to record a simulated span in-process,
        with no threads and no waiting, which is what makes the timeline tests
        fast and deterministic.
        """
        self._run(single_attempt=True)

    # -- main loop ---------------------------------------------------------

    def _run(self, single_attempt: bool = False) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._open_source()
                attempt = 0
                self._capture_loop()
                # Reached on stop, on an exhausted synthetic source, or on a
                # requested restart. In every case the old source and segment
                # are finished with before anything is reopened.
                self._teardown()
                if single_attempt:
                    return
            except (CaptureError, NoInputDeviceError, EncoderError) as exc:
                self._error = str(exc)
                log.error("capture failed: %s", exc)
                self._teardown()
                if single_attempt or self._stop.is_set():
                    return
                delay = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                attempt += 1
                # No segment rows are written while waiting. The resulting
                # hole in the index is the honest record that audio for this
                # period does not exist.
                log.info("retrying capture in %ds", delay)
                self._stop.wait(delay)
            except Exception:  # noqa: BLE001 - the service must outlive surprises
                log.exception("unexpected recorder failure")
                self._teardown()
                if single_attempt or self._stop.is_set():
                    return
                self._stop.wait(5)
        self._teardown()

    def _open_source(self) -> None:
        source = self.source_factory()
        self._stream_start = source.start()
        self._source = source
        self._captured_samples = 0
        self._next_segment_start = None
        self._last_resync = 0.0
        self._error = None
        log.info(
            "capture stream anchored at %s on %s",
            self._stream_start.isoformat(),
            source.describe(),
        )

    def _capture_loop(self) -> None:
        assert self._source is not None
        idle_reads = 0
        while not self._stop.is_set():
            if self._restart.is_set():
                self._restart.clear()
                log.info("restarting capture on request")
                return
            pcm = self._source.read(timeout=1.0)
            if pcm is None:
                idle_reads += 1
                # A finite synthetic source signals exhaustion this way; a
                # live device that goes quiet for this long has really failed.
                if getattr(self._source, "duration_seconds", None) is not None:
                    return
                if idle_reads >= 10:
                    raise CaptureError("no audio for 10s")
                continue
            idle_reads = 0
            self._resync_if_needed()
            self._consume(pcm)

    def _resync_if_needed(self) -> None:
        """Keep captured audio aligned with real elapsed time.

        Machines stall, PortAudio drops buffers under load, and neither event
        announces how much was lost. Comparing the audio we hold against the
        wall clock detects the loss; padding it with silence keeps everything
        recorded afterwards at its correct offset, which is worth far more
        than the lost fraction of a second.
        """
        if not self.resync_to_wall_clock or self._stream_start is None:
            return
        captured_seconds = self._captured_samples / self.config.sample_rate
        if captured_seconds - self._last_resync < RESYNC_INTERVAL_SECONDS:
            return
        self._last_resync = captured_seconds

        elapsed = (utcnow() - self._stream_start).total_seconds()
        shortfall = elapsed - captured_seconds

        if shortfall > DRIFT_REANCHOR_SECONDS:
            # Too big to be dropped buffers: the machine slept, or the clock
            # stepped. Start a fresh stream anchor so the gap is recorded as
            # a gap rather than as a very long stretch of fake silence.
            log.warning(
                "audio is %.1fs behind the clock; re-anchoring the stream", shortfall
            )
            self._close_writer()
            self._stream_start = utcnow()
            self._next_segment_start = None
            self._captured_samples = 0
            self._last_resync = 0.0
            return

        if shortfall > DRIFT_TOLERANCE_SECONDS:
            missing = int(shortfall * self.config.sample_rate)
            log.warning("padding %.2fs of lost audio to hold the timeline", shortfall)
            self._padded_samples += missing
            self._consume(silence(missing, self.config.channels), count_as_capture=True)
        elif shortfall < -DRIFT_REANCHOR_SECONDS:
            log.warning(
                "captured audio runs %.1fs ahead of the clock; re-anchoring", -shortfall
            )
            self._close_writer()
            self._stream_start = utcnow()
            self._captured_samples = 0
            self._last_resync = 0.0

    def _consume(self, pcm: bytes, count_as_capture: bool = True) -> None:
        """Write PCM, splitting it exactly on segment boundaries."""
        frame_bytes = BYTES_PER_SAMPLE * self.config.channels
        total_frames = len(pcm) // frame_bytes
        offset = 0

        while offset < total_frames:
            writer = self._ensure_writer()
            take = min(total_frames - offset, writer.remaining_samples)
            if take <= 0:
                self._close_writer()
                continue
            chunk = pcm[offset * frame_bytes : (offset + take) * frame_bytes]
            writer.write(chunk, take)
            offset += take
            if count_as_capture:
                self._captured_samples += take
            if writer.is_full:
                self._close_writer()

    def _ensure_writer(self) -> SegmentWriter:
        with self._lock:
            if self._writer is not None:
                return self._writer
            assert self._stream_start is not None
            # A segment that filled hands its exact boundary to the next one.
            # Recomputing from the sample count instead would land a fraction
            # of a sample short of the boundary -- enough to spawn a
            # zero-length segment, and enough to accumulate over a long day.
            start = self._next_segment_start
            if start is None:
                start = self._stream_start + timedelta(
                    seconds=self._captured_samples / self.config.sample_rate
                )

            # Snap to the grid: a segment that begins mid-window runs only to
            # the next boundary, which puts every later segment on :00/:30.
            end = next_boundary(start, self.config.segment_seconds)
            if (end - start).total_seconds() < MIN_SEGMENT_SECONDS:
                end += timedelta(seconds=self.config.segment_seconds)

            self._next_segment_start = None
            self._writer = SegmentWriter(
                config=self.config, db=self.db, start=start, boundary_end=end
            )
            return self._writer

    def _close_writer(self, status: str = STATUS_COMPLETE) -> None:
        with self._lock:
            writer, self._writer = self._writer, None
            # Only a segment that ran to its boundary defines where the next
            # one starts. One cut short by a stop, a restart or a device
            # failure does not: the following segment begins wherever audio
            # actually resumes.
            self._next_segment_start = (
                writer.boundary_end if writer is not None and writer.is_full else None
            )
        if writer is None:
            return
        try:
            writer.close(status=status)
        except Exception:  # noqa: BLE001 - a failed close must not abort shutdown
            log.exception("error closing segment %d", writer.segment_id)

    def _teardown(self) -> None:
        self._close_writer()
        source, self._source = self._source, None
        if source is not None:
            source.stop()


def repair_open_segments(config: Config, db: Database) -> int:
    """Reconcile segments left open by a crash or power loss.

    A row still marked ``recording`` at startup belongs to a process that
    never got to close it. Its true length is recovered by probing the file,
    so the audio it does contain stays retrievable instead of being written
    off. Returns the number of rows repaired.
    """
    stale = db.stale_open_segments()
    for segment in stale:
        path = Path(segment.file_path)
        if not path.exists():
            db.mark_status(segment.id, STATUS_MISSING)
            log.warning("segment %d has no file at %s; marked missing", segment.id, path)
            continue
        duration = probe_duration(path)
        if duration is None or duration <= 0:
            db.mark_status(segment.id, STATUS_MISSING)
            log.warning("segment %d at %s is unreadable; marked missing", segment.id, path)
            continue
        sample_count = int(duration * segment.sample_rate)
        db.close_segment(segment.id, sample_count=sample_count, status=STATUS_TRUNCATED)
        log.info(
            "recovered segment %d: %.1fs of audio from an interrupted recording",
            segment.id,
            duration,
        )
    return len(stale)
