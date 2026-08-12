"""A time watermark for verifying retrieval alignment.

The hard question about this product is not "does it record?" but "is the
audio you got back from timestamp T actually the audio from timestamp T?".
Listening cannot answer that at scale, and a test that only checks file
lengths would pass while handing the backend the wrong half hour.

So the synthetic source stamps the time into the audio itself: each whole
second is a tone whose frequency encodes that second's wall-clock value.
Decoding a returned clip recovers the exact seconds it contains, which proves
alignment across segment boundaries, gaps and clock jumps.

Design constraints on the encoding:

* Frequencies stay between 300 Hz and ~3.9 kHz, well inside what a 32 kbps
  mono MP3 preserves, so the watermark survives the real storage format.
* 4 Hz spacing against 1 Hz FFT bins (a one-second window) leaves enough
  margin to identify a second unambiguously after lossy encoding.
* The cycle is 900 seconds. Fifteen minutes of unique values is ample for
  verifying a ten-minute retrieval window, and keeps the top frequency low.
"""

from __future__ import annotations

import numpy as np

BASE_HZ = 300.0
STEP_HZ = 4.0
CYCLE_SECONDS = 900
AMPLITUDE = 0.5
FADE_SECONDS = 0.005  # kills the click at each second boundary


def frequency_for_second(epoch_second: float) -> float:
    """Tone frequency encoding the whole second containing *epoch_second*."""
    return BASE_HZ + (int(epoch_second) % CYCLE_SECONDS) * STEP_HZ


def second_for_frequency(hz: float) -> int:
    """Inverse of :func:`frequency_for_second`, to the nearest encoded second."""
    return int(round((hz - BASE_HZ) / STEP_HZ)) % CYCLE_SECONDS


def encode_seconds(
    *,
    start_sample: int,
    sample_count: int,
    sample_rate: int,
    epoch_offset: float,
) -> bytes:
    """Generate watermarked PCM for a sample range.

    Sample ``n`` represents wall-clock ``epoch_offset + n / sample_rate``.
    The result depends only on that absolute position, never on how the range
    was chunked, so a caller may request any slice and get bytes identical to
    what a contiguous generation would have produced. Segment rotation splits
    buffers at arbitrary points, and this property is what keeps the two sides
    of a split consistent.
    """
    positions = np.arange(start_sample, start_sample + sample_count, dtype=np.float64)
    times = epoch_offset + positions / sample_rate
    whole = np.floor(times)
    fraction = times - whole

    freqs = BASE_HZ + (np.mod(whole, CYCLE_SECONDS)) * STEP_HZ
    # Phase resets at each second boundary, so any slice is reproducible from
    # its absolute time alone.
    signal = np.sin(2.0 * np.pi * freqs * fraction)

    envelope = np.ones_like(signal)
    np.minimum(envelope, fraction / FADE_SECONDS, out=envelope)
    np.minimum(envelope, (1.0 - fraction) / FADE_SECONDS, out=envelope)
    np.clip(envelope, 0.0, 1.0, out=envelope)

    samples = (signal * envelope * AMPLITUDE * 32767.0).astype("<i2")
    return samples.tobytes()


def dominant_frequency(samples: np.ndarray, sample_rate: int) -> float:
    """Peak frequency of *samples*, refined by parabolic interpolation.

    Interpolation matters: a one-second window gives 1 Hz bins against 4 Hz
    tone spacing, and MP3 quantisation shifts energy between neighbouring
    bins. Taking the raw peak bin would occasionally land a full step away.
    """
    if samples.size == 0:
        return 0.0
    windowed = samples.astype(np.float64) * np.hanning(samples.size)
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / sample_rate)

    # Ignore DC and the very low end, where MP3 noise and the fade envelope
    # both put energy that is not the tone.
    floor_bin = int(np.searchsorted(freqs, BASE_HZ * 0.5))
    peak = floor_bin + int(np.argmax(spectrum[floor_bin:]))

    if 0 < peak < spectrum.size - 1:
        left, centre, right = spectrum[peak - 1], spectrum[peak], spectrum[peak + 1]
        denominator = left - 2.0 * centre + right
        if denominator != 0:
            offset = 0.5 * (left - right) / denominator
            return float(freqs[peak] + offset * (freqs[1] - freqs[0]))
    return float(freqs[peak])


def decode_second(samples: np.ndarray, sample_rate: int) -> int:
    """Recover the encoded second from one second of watermarked audio."""
    return second_for_frequency(dominant_frequency(samples, sample_rate))


def decode_timeline(
    samples: np.ndarray, sample_rate: int, *, skip_edges: bool = True
) -> list[int | None]:
    """Decode each whole second of *samples* in order.

    Returns the encoded second for each one-second window, or None where the
    window is silent -- which is how a padded gap shows up, and is exactly
    what a caller wants to assert about a clip spanning a recording hole.

    ``skip_edges`` drops a trailing partial window, which carries too little
    signal to identify reliably.
    """
    results: list[int | None] = []
    total = samples.size // sample_rate if skip_edges else -(-samples.size // sample_rate)
    for index in range(total):
        window = samples[index * sample_rate : (index + 1) * sample_rate]
        if window.size == 0:
            continue
        # Silence padding is exact zeros; real captured audio never is.
        if float(np.max(np.abs(window.astype(np.float64)))) < 1.0:
            results.append(None)
        else:
            results.append(decode_second(window, sample_rate))
    return results
