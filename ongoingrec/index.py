"""SQLite index of recorded segments and retrieval jobs.

This is the metadata layer of PRD section 9: it answers "which recording
contains 11:22:15?" without touching the filesystem, and it survives restarts
so a job accepted before a reboot is still finished afterwards.

Times are stored twice per row: an epoch float for range queries, and an ISO
string for anyone reading the database by hand during support. The epoch is
authoritative.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .timeutil import ensure_utc, format_utc, utcnow

_UTC = timezone.utc

SCHEMA_VERSION = 1

# Segment lifecycle.
STATUS_RECORDING = "recording"  # currently being written
STATUS_COMPLETE = "complete"  # closed cleanly, duration is trustworthy
STATUS_TRUNCATED = "truncated"  # process died mid-write; duration recovered by probe
STATUS_MISSING = "missing"  # indexed but the file is gone

# Job lifecycle.
JOB_PENDING = "pending"
JOB_EXTRACTING = "extracting"
JOB_UPLOADING = "uploading"
JOB_DONE = "done"
JOB_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id   TEXT NOT NULL,
    email_id     TEXT NOT NULL,
    employee_id  TEXT NOT NULL,
    start_epoch  REAL NOT NULL,
    end_epoch    REAL,
    start_utc    TEXT NOT NULL,
    end_utc      TEXT,
    sample_rate  INTEGER NOT NULL,
    channels     INTEGER NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    file_path    TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segments_start ON segments (start_epoch);
CREATE INDEX IF NOT EXISTS idx_segments_end   ON segments (end_epoch);
CREATE INDEX IF NOT EXISTS idx_segments_status ON segments (status);

CREATE TABLE IF NOT EXISTS jobs (
    job_id         TEXT PRIMARY KEY,
    requested_epoch REAL NOT NULL,
    requested_utc  TEXT NOT NULL,
    window_seconds INTEGER NOT NULL,
    status         TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    clip_path      TEXT,
    next_attempt_epoch REAL NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
"""


@dataclass
class Segment:
    """One recorded audio segment.

    ``end`` is None while the segment is still being written. ``sample_count``
    is the authority on duration once closed -- it comes from the audio
    actually captured, not from a wall-clock subtraction.
    """

    id: int
    install_id: str
    email_id: str
    employee_id: str
    start: datetime
    end: datetime | None
    sample_rate: int
    channels: int
    sample_count: int
    file_path: Path
    status: str

    @property
    def duration_seconds(self) -> float:
        if self.sample_count and self.sample_rate:
            return self.sample_count / self.sample_rate
        if self.end is not None:
            return (self.end - self.start).total_seconds()
        return 0.0

    @property
    def effective_end(self) -> datetime:
        """End of captured audio, usable while the segment is still open."""
        return self.start + timedelta(seconds=self.duration_seconds)

    def contains(self, when: datetime) -> bool:
        return self.start <= ensure_utc(when) < self.effective_end

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Segment":
        return cls(
            id=row["id"],
            install_id=row["install_id"],
            email_id=row["email_id"],
            employee_id=row["employee_id"],
            start=datetime.fromtimestamp(row["start_epoch"], tz=_UTC),
            end=(
                datetime.fromtimestamp(row["end_epoch"], tz=_UTC)
                if row["end_epoch"] is not None
                else None
            ),
            sample_rate=row["sample_rate"],
            channels=row["channels"],
            sample_count=row["sample_count"],
            file_path=Path(row["file_path"]),
            status=row["status"],
        )


@dataclass
class Job:
    """A clip request received from the backend."""

    job_id: str
    requested_at: datetime
    window_seconds: int
    status: str
    attempts: int
    last_error: str | None
    clip_path: Path | None
    next_attempt_epoch: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            job_id=row["job_id"],
            requested_at=datetime.fromtimestamp(row["requested_epoch"], tz=_UTC),
            window_seconds=row["window_seconds"],
            status=row["status"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            clip_path=Path(row["clip_path"]) if row["clip_path"] else None,
            next_attempt_epoch=row["next_attempt_epoch"],
        )


class Database:
    """Thread-confined SQLite access.

    The recorder, the API and the poller all touch this concurrently, so each
    thread gets its own connection and WAL mode lets readers proceed while the
    recorder writes.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        with self._init_lock:
            self._migrate()

    # -- connection -------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            # Fold the WAL back into the main file so a cleanly stopped
            # service leaves a self-contained database -- which matters when
            # someone copies index.db off a laptop to diagnose a problem.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        conn.close()
        self._local.conn = None

    def _migrate(self) -> None:
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    # -- meta -------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- segments ---------------------------------------------------------

    def open_segment(
        self,
        *,
        install_id: str,
        email_id: str,
        employee_id: str,
        start: datetime,
        sample_rate: int,
        channels: int,
        file_path: Path,
    ) -> int:
        """Record a segment as it begins, before any audio is written.

        Indexing on open rather than on close means a segment interrupted by
        power loss is still discoverable; the startup repair pass reconciles
        its true duration.
        """
        start = ensure_utc(start)
        now = format_utc(utcnow())
        cursor = self.conn.execute(
            """
            INSERT INTO segments (
                install_id, email_id, employee_id, start_epoch, start_utc,
                sample_rate, channels, sample_count, file_path, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                install_id,
                email_id,
                employee_id,
                start.timestamp(),
                format_utc(start),
                sample_rate,
                channels,
                str(file_path),
                STATUS_RECORDING,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def update_progress(self, segment_id: int, sample_count: int) -> None:
        """Checkpoint how much audio has landed, so a crash loses little.

        Called periodically by the recorder rather than per buffer; the cost
        of being slightly stale is bounded by the checkpoint interval, and the
        repair pass corrects it exactly.
        """
        self.conn.execute(
            "UPDATE segments SET sample_count=?, updated_at=? WHERE id=?",
            (sample_count, format_utc(utcnow()), segment_id),
        )

    def close_segment(
        self, segment_id: int, *, sample_count: int, status: str = STATUS_COMPLETE
    ) -> None:
        """Finalize a segment, deriving its end time from captured samples."""
        row = self.conn.execute(
            "SELECT start_epoch, sample_rate FROM segments WHERE id=?", (segment_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no segment {segment_id}")
        end_epoch = row["start_epoch"] + (sample_count / row["sample_rate"])
        end = datetime.fromtimestamp(end_epoch, tz=_UTC)
        self.conn.execute(
            """
            UPDATE segments
               SET end_epoch=?, end_utc=?, sample_count=?, status=?, updated_at=?
             WHERE id=?
            """,
            (end_epoch, format_utc(end), sample_count, status, format_utc(utcnow()), segment_id),
        )

    def get_segment(self, segment_id: int) -> Segment | None:
        row = self.conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
        return Segment.from_row(row) if row else None

    def segments_overlapping(self, start: datetime, end: datetime) -> list[Segment]:
        """Segments intersecting ``[start, end)``, in chronological order.

        An open segment has a NULL ``end_epoch``; it is included whenever it
        starts before the window ends, and the caller clamps against the audio
        actually captured so far via :attr:`Segment.effective_end`.
        """
        start_epoch = ensure_utc(start).timestamp()
        end_epoch = ensure_utc(end).timestamp()
        rows = self.conn.execute(
            """
            SELECT * FROM segments
             WHERE status != ?
               AND start_epoch < ?
               AND (end_epoch IS NULL OR end_epoch > ?)
             ORDER BY start_epoch ASC
            """,
            (STATUS_MISSING, end_epoch, start_epoch),
        ).fetchall()
        return [Segment.from_row(row) for row in rows]

    def stale_open_segments(self, *, exclude_id: int | None = None) -> list[Segment]:
        """Segments left in ``recording`` state, i.e. never closed cleanly."""
        rows = self.conn.execute(
            "SELECT * FROM segments WHERE status=? ORDER BY start_epoch ASC",
            (STATUS_RECORDING,),
        ).fetchall()
        return [Segment.from_row(r) for r in rows if exclude_id is None or r["id"] != exclude_id]

    def segments_before(self, cutoff: datetime) -> list[Segment]:
        """Closed segments that ended before *cutoff*, oldest first."""
        rows = self.conn.execute(
            """
            SELECT * FROM segments
             WHERE end_epoch IS NOT NULL AND end_epoch < ?
             ORDER BY start_epoch ASC
            """,
            (ensure_utc(cutoff).timestamp(),),
        ).fetchall()
        return [Segment.from_row(row) for row in rows]

    def oldest_segments(self, limit: int = 50) -> list[Segment]:
        rows = self.conn.execute(
            "SELECT * FROM segments WHERE status IN (?, ?) ORDER BY start_epoch ASC LIMIT ?",
            (STATUS_COMPLETE, STATUS_TRUNCATED, limit),
        ).fetchall()
        return [Segment.from_row(row) for row in rows]

    def mark_status(self, segment_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE segments SET status=?, updated_at=? WHERE id=?",
            (status, format_utc(utcnow()), segment_id),
        )

    def delete_segments(self, segment_ids: Iterable[int]) -> int:
        ids = list(segment_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        cursor = self.conn.execute(f"DELETE FROM segments WHERE id IN ({placeholders})", ids)
        return cursor.rowcount

    def latest_segment(self) -> Segment | None:
        row = self.conn.execute(
            "SELECT * FROM segments ORDER BY start_epoch DESC LIMIT 1"
        ).fetchone()
        return Segment.from_row(row) if row else None

    def segment_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM segments").fetchone()["n"])

    # -- jobs -------------------------------------------------------------

    def upsert_job(self, job_id: str, requested_at: datetime, window_seconds: int) -> bool:
        """Register a job idempotently. Returns True if it is new.

        The backend delivers at-least-once, so the same ``job_id`` can arrive
        twice; the second arrival must not produce a second upload.
        """
        requested_at = ensure_utc(requested_at)
        now = format_utc(utcnow())
        cursor = self.conn.execute(
            """
            INSERT INTO jobs (
                job_id, requested_epoch, requested_utc, window_seconds,
                status, attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(job_id) DO NOTHING
            """,
            (
                job_id,
                requested_at.timestamp(),
                format_utc(requested_at),
                window_seconds,
                JOB_PENDING,
                now,
                now,
            ),
        )
        return cursor.rowcount > 0

    def claimable_jobs(self, now_epoch: float, limit: int = 10) -> list[Job]:
        """Jobs still owed to the backend and due for another attempt."""
        rows = self.conn.execute(
            """
            SELECT * FROM jobs
             WHERE status IN (?, ?, ?)
               AND next_attempt_epoch <= ?
             ORDER BY requested_epoch ASC
             LIMIT ?
            """,
            (JOB_PENDING, JOB_EXTRACTING, JOB_UPLOADING, now_epoch, limit),
        ).fetchall()
        return [Job.from_row(row) for row in rows]

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        attempts: int | None = None,
        last_error: str | None = None,
        clip_path: Path | None = None,
        next_attempt_epoch: float | None = None,
    ) -> None:
        sets: list[str] = []
        values: list[object] = []
        for column, value in (
            ("status", status),
            ("attempts", attempts),
            ("last_error", last_error),
            ("clip_path", str(clip_path) if clip_path is not None else None),
            ("next_attempt_epoch", next_attempt_epoch),
        ):
            if value is not None:
                sets.append(f"{column}=?")
                values.append(value)
        if not sets:
            return
        sets.append("updated_at=?")
        values.extend([format_utc(utcnow()), job_id])
        self.conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", values)

    def get_job(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def purge_finished_jobs(self, cutoff: datetime) -> int:
        cursor = self.conn.execute(
            "DELETE FROM jobs WHERE status IN (?, ?) AND requested_epoch < ?",
            (JOB_DONE, JOB_FAILED, ensure_utc(cutoff).timestamp()),
        )
        return cursor.rowcount
