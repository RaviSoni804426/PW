"""Time handling for OngoingRec.

Two rules hold everywhere in this codebase:

1. Every timestamp that crosses a module boundary or hits storage is an
   aware UTC ``datetime``. Naive datetimes only exist at the edges, where a
   human or an API payload supplied one, and they are resolved immediately.

2. Elapsed time inside a recording is measured from the audio itself
   (sample counts) or from ``time.monotonic()`` -- never by subtracting two
   wall-clock readings. An NTP correction or a DST shift mid-segment would
   otherwise corrupt the segment timeline, which is the one thing the whole
   product depends on being right.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Resolve *dt* to aware UTC.

    A naive datetime is interpreted as **laptop local time**, which is what a
    counsellor or a backend operator means when they write ``11:22:15``. The
    platform tz database supplies the correct offset for that specific date,
    so historical DST transitions resolve correctly.
    """
    if dt.tzinfo is None:
        return dt.astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into aware UTC.

    Accepts offsets (``2026-08-12T11:22:15+05:30``), a ``Z`` suffix, and naive
    values (resolved as local time per :func:`ensure_utc`).

    Raises:
        ValueError: if *value* is not a parseable ISO-8601 timestamp.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    return ensure_utc(parsed)


def format_utc(dt: datetime) -> str:
    """Render an aware datetime as ``2026-08-12T11:22:15Z``."""
    return ensure_utc(dt).strftime(ISO_FORMAT) + "Z"


def to_local(dt: datetime) -> datetime:
    """Convert an aware datetime to the laptop's local timezone."""
    return ensure_utc(dt).astimezone()


def local_offset_name() -> str:
    """Local UTC offset as ``+05:30``, for logging and response metadata."""
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def floor_to_boundary(dt: datetime, period_seconds: int) -> datetime:
    """Largest segment boundary <= *dt*.

    Boundaries are anchored to midnight **UTC** rather than local midnight.
    Local alignment would break on a DST fall-back day, where 01:00-01:30
    occurs twice and would collide on both the filename and the index lookup.
    UTC alignment has no such discontinuity.

    For any zone offset by a whole number of half-hours -- including IST
    (+05:30), the deployment target -- a 1800s period still lands exactly on
    the local ``:00``/``:30`` grid the PRD describes. The handful of :45 zones
    (Nepal, Chatham) get a local :15/:45 grid instead, which is cosmetic:
    lookups and extraction are unaffected.
    """
    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    dt = ensure_utc(dt)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((dt - midnight).total_seconds())
    return midnight + timedelta(seconds=(elapsed // period_seconds) * period_seconds)


def next_boundary(dt: datetime, period_seconds: int) -> datetime:
    """Smallest segment boundary strictly greater than *dt*."""
    return floor_to_boundary(dt, period_seconds) + timedelta(seconds=period_seconds)


@dataclass
class MonotonicClock:
    """Wall-clock time derived from a monotonic reading.

    Anchored once at construction, then every subsequent timestamp is the
    anchor plus monotonic elapsed time. This makes a recording's timeline
    immune to clock adjustments that happen while it is running: the segment
    stays internally consistent even if the system clock jumps.

    :meth:`drift` reports how far the anchor has diverged from the real system
    clock, so the supervisor can notice a large correction and roll the
    segment rather than silently accumulate error.
    """

    wall_anchor: datetime
    mono_anchor: float

    @classmethod
    def start(cls) -> "MonotonicClock":
        return cls(wall_anchor=utcnow(), mono_anchor=time.monotonic())

    def now(self) -> datetime:
        return self.wall_anchor + timedelta(seconds=time.monotonic() - self.mono_anchor)

    def at_elapsed(self, elapsed_seconds: float) -> datetime:
        """Wall-clock time *elapsed_seconds* after the anchor."""
        return self.wall_anchor + timedelta(seconds=elapsed_seconds)

    def drift(self) -> float:
        """Seconds by which the system clock has moved relative to this anchor.

        Positive means the system clock is ahead of where this clock thinks it
        should be (e.g. an NTP step forward).
        """
        return (utcnow() - self.now()).total_seconds()

    def rebase(self) -> "MonotonicClock":
        """Return a fresh anchor at the current system time."""
        return MonotonicClock.start()


def samples_to_seconds(sample_count: int, sample_rate: int) -> float:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return sample_count / sample_rate


def seconds_to_samples(seconds: float, sample_rate: int) -> int:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return int(round(seconds * sample_rate))
