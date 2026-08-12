"""Shared test utilities for driving the recorder without a microphone."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np

from ongoingrec.audio.capture import SyntheticAudioSource
from ongoingrec.audio.encoder import ffmpeg_path
from ongoingrec.config import Config
from ongoingrec.index import Database
from ongoingrec.segments import Recorder


def record_synthetic(
    config: Config,
    db: Database,
    *,
    start: datetime,
    seconds: float,
    block_seconds: float = 0.5,
) -> Recorder:
    """Record a simulated span instantly, with watermarked audio.

    Runs the real Recorder against a synthetic source, so segmentation,
    boundary splitting, encoding and indexing are all genuinely exercised --
    only the microphone is substituted.
    """
    recorder = Recorder(
        config,
        db,
        lambda: SyntheticAudioSource(
            sample_rate=config.sample_rate,
            channels=config.channels,
            start_time=start,
            block_seconds=block_seconds,
            duration_seconds=seconds,
        ),
        resync_to_wall_clock=False,
    )
    recorder.run_until_source_exhausted()
    return recorder


def decode_pcm(path: Path, sample_rate: int) -> np.ndarray:
    """Decode an audio file to mono int16 samples."""
    result = subprocess.run(
        [
            ffmpeg_path(),
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(path),
            "-f", "s16le",
            "-ar", str(sample_rate),
            "-ac", "1",
            "pipe:1",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"could not decode {path}: {result.stderr.decode()}")
    return np.frombuffer(result.stdout, dtype="<i2")


def decode_bytes(payload: bytes, sample_rate: int) -> np.ndarray:
    result = subprocess.run(
        [
            ffmpeg_path(),
            "-hide_banner",
            "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "s16le",
            "-ar", str(sample_rate),
            "-ac", "1",
            "pipe:1",
        ],
        input=payload,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"could not decode clip: {result.stderr.decode()}")
    return np.frombuffer(result.stdout, dtype="<i2")


def expected_watermarks(start_epoch: float, count: int) -> list[int]:
    """The watermark values a clip starting at *start_epoch* should contain."""
    from ongoingrec.audio.watermark import CYCLE_SECONDS

    return [int(start_epoch + i) % CYCLE_SECONDS for i in range(count)]
