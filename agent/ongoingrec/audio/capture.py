"""Audio sources feeding the recorder.

A source hands the recorder raw little-endian 16-bit PCM and nothing else.
The recorder owns the timeline; a source only has to say when its first sample
was taken and keep producing bytes.

Two implementations exist. :class:`DeviceAudioSource` is the real microphone.
:class:`SyntheticAudioSource` generates a watermarked tone from a fixed start
time, which lets the whole recording and retrieval chain be tested for
timeline correctness without a microphone, in real time or far faster.
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime, timedelta
from typing import Protocol

import numpy as np

from ..logging_setup import get_logger
from ..timeutil import utcnow
from .devices import NoInputDeviceError, resolve_device
from .watermark import encode_seconds

log = get_logger(__name__)

BYTES_PER_SAMPLE = 2


class CaptureError(Exception):
    """The capture stream failed and the recorder should reopen it."""


class AudioSource(Protocol):
    """Produces PCM frames with a known start time."""

    sample_rate: int
    channels: int

    def start(self) -> datetime:
        """Open the source and return the wall-clock time of its first sample."""

    def read(self, timeout: float) -> bytes | None:
        """Next chunk of PCM, or None if none arrived within *timeout*."""

    def stop(self) -> None:
        """Release the source. Safe to call when already stopped."""

    def describe(self) -> str:
        """Human-readable identity, for logs and /health."""


class DeviceAudioSource:
    """Live microphone capture through PortAudio.

    The PortAudio callback runs on a real-time thread: it must never block or
    allocate unpredictably, so it does nothing but copy bytes into a bounded
    queue. If the recorder ever falls far enough behind to fill that queue,
    the callback drops the block and counts it rather than stalling the audio
    device, and the recorder repairs the resulting hole from its own drift
    check.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        device: str | None = None,
        block_seconds: float = 0.1,
        queue_seconds: float = 30.0,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.device_preference = device
        self.blocksize = max(1, int(sample_rate * block_seconds))
        self._queue: queue.Queue[bytes] = queue.Queue(
            maxsize=max(4, int(queue_seconds / block_seconds))
        )
        self._stream = None
        self._resolved = None
        self._first_block_at: datetime | None = None
        self._first_block_seen = threading.Event()
        self._dropped_blocks = 0
        self._overflows = 0
        self._lock = threading.Lock()

    def describe(self) -> str:
        if self._resolved is None:
            return self.device_preference or "default input"
        return f"{self._resolved.name} (index {self._resolved.index})"

    @property
    def dropped_blocks(self) -> int:
        return self._dropped_blocks

    @property
    def overflows(self) -> int:
        return self._overflows

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            # input_overflow means PortAudio discarded frames before we saw
            # them. The count is unknowable here; the recorder's drift check
            # is what restores the timeline.
            self._overflows += 1
        if self._first_block_at is None:
            # The block just handed to us was captured over the preceding
            # blocksize interval, so the first sample precedes 'now'.
            self._first_block_at = utcnow() - timedelta(seconds=frames / self.sample_rate)
            self._first_block_seen.set()
        try:
            self._queue.put_nowait(bytes(indata))
        except queue.Full:
            self._dropped_blocks += 1

    def start(self) -> datetime:
        import sounddevice as sd

        with self._lock:
            self._resolved = resolve_device(self.device_preference)
            if self._resolved.channels < self.channels:
                raise NoInputDeviceError(
                    f"{self._resolved.name} offers {self._resolved.channels} input "
                    f"channel(s), need {self.channels}"
                )
            try:
                self._stream = sd.RawInputStream(
                    samplerate=self.sample_rate,
                    blocksize=self.blocksize,
                    device=self._resolved.index,
                    channels=self.channels,
                    dtype="int16",
                    callback=self._callback,
                )
                self._stream.start()
            except Exception as exc:  # noqa: BLE001 - PortAudio raises many types
                self._stream = None
                raise CaptureError(f"could not open {self.describe()}: {exc}") from exc

        log.info("capturing from %s at %d Hz", self.describe(), self.sample_rate)

        # Wait for the first block so the returned start time reflects when
        # audio actually began, not when it was asked for. Waiting on an
        # event rather than pulling from the queue leaves the audio itself
        # untouched -- a device that opens but never delivers is a failure
        # worth reporting, not a stream to start counting time from.
        if not self._first_block_seen.wait(timeout=5.0):
            self.stop()
            raise CaptureError(
                f"{self.describe()} opened but delivered no audio within 5s"
            )
        assert self._first_block_at is not None
        return self._first_block_at

    def read(self, timeout: float) -> bytes | None:
        if self._stream is not None and not self._stream.active:
            raise CaptureError(f"capture stream for {self.describe()} stopped unexpectedly")
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception as exc:  # noqa: BLE001 - closing must never raise upward
            log.warning("error closing capture stream: %s", exc)


class SyntheticAudioSource:
    """Deterministic watermarked audio for tests and self-checks.

    Every second of output is a tone whose frequency encodes that second's
    wall-clock time (see :mod:`ongoingrec.audio.watermark`). Decoding a clip
    produced from this source therefore reveals exactly which moments it
    contains, which is how retrieval alignment is verified end to end without
    anyone listening to anything.

    With ``realtime=False`` it yields chunks as fast as they are requested, so
    a test can record a simulated 90 minutes in well under a second.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int = 1,
        start_time: datetime | None = None,
        block_seconds: float = 0.5,
        realtime: bool = False,
        duration_seconds: float | None = None,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.start_time = start_time or utcnow()
        self.block_samples = max(1, int(sample_rate * block_seconds))
        self.realtime = realtime
        self.duration_seconds = duration_seconds
        self._cursor = 0
        self._stopped = threading.Event()

    def describe(self) -> str:
        return "synthetic watermark source"

    def start(self) -> datetime:
        self._cursor = 0
        self._stopped.clear()
        return self.start_time

    def read(self, timeout: float) -> bytes | None:
        if self._stopped.is_set():
            return None
        if self.duration_seconds is not None:
            if self._cursor / self.sample_rate >= self.duration_seconds:
                return None

        if self.realtime:
            due = self.start_time + timedelta(
                seconds=(self._cursor + self.block_samples) / self.sample_rate
            )
            wait = (due - utcnow()).total_seconds()
            if wait > 0 and self._stopped.wait(min(wait, timeout)):
                return None

        pcm = encode_seconds(
            start_sample=self._cursor,
            sample_count=self.block_samples,
            sample_rate=self.sample_rate,
            epoch_offset=self.start_time.timestamp(),
        )
        self._cursor += self.block_samples
        if self.channels > 1:
            frames = np.frombuffer(pcm, dtype="<i2")
            pcm = np.repeat(frames, self.channels).astype("<i2").tobytes()
        return pcm

    def stop(self) -> None:
        self._stopped.set()


def silence(sample_count: int, channels: int = 1) -> bytes:
    """PCM silence, used to hold the timeline open across lost audio."""
    return b"\x00" * (sample_count * channels * BYTES_PER_SAMPLE)
