"""Persistent state for the backend.

The mock backend keeps devices and jobs in memory, which is fine on a laptop
and wrong the moment anything depends on it: a restart forgets every enrolled
device, and an agent still holding a token the backend no longer recognises
gets 403 forever. On a hosted deployment a restart is routine -- a redeploy, a
crash, a host reboot -- so state goes to SQLite on a mounted volume instead.

One connection guarded by a lock, rather than a pool. Writes to SQLite
serialize anyway, the working set is a few hundred rows, and a single
connection removes a whole class of "which thread owns this handle" bugs.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JOB_QUEUED = "queued"
JOB_DELIVERED = "delivered"
JOB_COMPLETE = "complete"
JOB_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    install_id      TEXT PRIMARY KEY,
    email_id        TEXT NOT NULL,
    employee_id     TEXT NOT NULL,
    hostname        TEXT NOT NULL DEFAULT '',
    os_version      TEXT NOT NULL DEFAULT '',
    agent_version   TEXT NOT NULL DEFAULT '',
    device_token    TEXT NOT NULL UNIQUE,
    registered_at   TEXT NOT NULL,
    last_heartbeat_at   TEXT,
    last_heartbeat_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_devices_token    ON devices (device_token);
CREATE INDEX IF NOT EXISTS idx_devices_employee ON devices (employee_id);
CREATE INDEX IF NOT EXISTS idx_devices_email    ON devices (email_id);

CREATE TABLE IF NOT EXISTS jobs (
    job_id         TEXT PRIMARY KEY,
    install_id     TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    window_seconds INTEGER,
    status         TEXT NOT NULL,
    error_json     TEXT,
    clip_path      TEXT,
    clip_bytes     INTEGER NOT NULL DEFAULT 0,
    metadata_json  TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs (install_id, status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Device:
    install_id: str
    email_id: str
    employee_id: str
    hostname: str
    device_token: str
    registered_at: str
    last_heartbeat: dict[str, Any] | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Device":
        return cls(
            install_id=row["install_id"],
            email_id=row["email_id"],
            employee_id=row["employee_id"],
            hostname=row["hostname"],
            device_token=row["device_token"],
            registered_at=row["registered_at"],
            last_heartbeat=(
                json.loads(row["last_heartbeat_json"]) if row["last_heartbeat_json"] else None
            ),
        )

    def public(self) -> dict[str, Any]:
        return {
            "install_id": self.install_id,
            "email_id": self.email_id,
            "employee_id": self.employee_id,
            "hostname": self.hostname,
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
        }


@dataclass
class Job:
    job_id: str
    install_id: str
    timestamp: str
    window_seconds: int | None
    status: str
    error: dict[str, str] | None
    clip_path: str | None
    clip_bytes: int
    metadata: dict[str, Any] | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            job_id=row["job_id"],
            install_id=row["install_id"],
            timestamp=row["timestamp"],
            window_seconds=row["window_seconds"],
            status=row["status"],
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            clip_path=row["clip_path"],
            clip_bytes=row["clip_bytes"],
            metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "install_id": self.install_id,
            "status": self.status,
            "timestamp": self.timestamp,
            "window_seconds": self.window_seconds,
            "error": self.error,
            "clip_bytes": self.clip_bytes,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- devices ----------------------------------------------------------

    def register(
        self,
        *,
        install_id: str,
        email_id: str,
        employee_id: str,
        hostname: str = "",
        os_version: str = "",
        agent_version: str = "",
    ) -> Device:
        """Enrol a laptop, or re-enrol one that is already known.

        Idempotent on ``install_id`` per the contract: a laptop whose token was
        lost, or whose config was restored from a backup, must be recognised
        rather than duplicated. A fresh token is issued each time, which also
        makes re-enrolment the documented way to rotate one.
        """
        token = f"tok_{secrets.token_urlsafe(32)}"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO devices (
                    install_id, email_id, employee_id, hostname, os_version,
                    agent_version, device_token, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(install_id) DO UPDATE SET
                    email_id=excluded.email_id,
                    employee_id=excluded.employee_id,
                    hostname=excluded.hostname,
                    os_version=excluded.os_version,
                    agent_version=excluded.agent_version,
                    device_token=excluded.device_token,
                    registered_at=excluded.registered_at
                """,
                (
                    install_id,
                    email_id,
                    employee_id,
                    hostname,
                    os_version,
                    agent_version,
                    token,
                    _now(),
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM devices WHERE install_id=?", (install_id,)
            ).fetchone()
        return Device.from_row(row)

    def device_by_token(self, token: str) -> Device | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE device_token=?", (token,)
            ).fetchone()
        return Device.from_row(row) if row else None

    def device_by_identifier(
        self, employee_id: str | None, email_id: str | None
    ) -> Device | None:
        """Resolve an operator's identifier to the laptop that enrolled with it.

        This is the routing the whole outbound design depends on: an employee
        ID arrives from a human, and the backend already knows which machine it
        belongs to because that machine said so at enrolment. Matching is
        case- and whitespace-tolerant because these are typed by people.
        """
        with self._lock:
            if employee_id and employee_id.strip():
                row = self._conn.execute(
                    "SELECT * FROM devices WHERE lower(trim(employee_id))=? "
                    "ORDER BY registered_at DESC LIMIT 1",
                    (employee_id.strip().lower(),),
                ).fetchone()
                if row:
                    return Device.from_row(row)
            if email_id and email_id.strip():
                row = self._conn.execute(
                    "SELECT * FROM devices WHERE lower(trim(email_id))=? "
                    "ORDER BY registered_at DESC LIMIT 1",
                    (email_id.strip().lower(),),
                ).fetchone()
                if row:
                    return Device.from_row(row)
        return None

    def record_heartbeat(self, install_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET last_heartbeat_at=?, last_heartbeat_json=? "
                "WHERE install_id=?",
                (_now(), json.dumps({"received_at": _now(), **payload}), install_id),
            )

    def all_devices(self) -> list[Device]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM devices ORDER BY registered_at DESC"
            ).fetchall()
        return [Device.from_row(r) for r in rows]

    # -- jobs -------------------------------------------------------------

    def create_job(self, install_id: str, timestamp: str, window_seconds: int | None) -> Job:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    job_id, install_id, timestamp, window_seconds, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, install_id, timestamp, window_seconds, JOB_QUEUED, now, now),
            )
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return Job.from_row(row)

    def claim_queued(self, install_id: str) -> list[Job]:
        """Hand this laptop its queued jobs and mark them delivered.

        Delivery is at-least-once by design -- a job goes back on the queue if
        the laptop never reports on it, and the agent deduplicates on job_id --
        so claiming here does not have to be exactly-once to be correct.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE install_id=? AND status=? ORDER BY created_at ASC",
                (install_id, JOB_QUEUED),
            ).fetchall()
            if rows:
                self._conn.execute(
                    "UPDATE jobs SET status=?, updated_at=? WHERE install_id=? AND status=?",
                    (JOB_DELIVERED, _now(), install_id, JOB_QUEUED),
                )
        return [Job.from_row(r) for r in rows]

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def complete_job(
        self, job_id: str, *, clip_path: Path, clip_bytes: int, metadata: dict[str, Any]
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, clip_path=?, clip_bytes=?, metadata_json=?, "
                "updated_at=? WHERE job_id=?",
                (
                    JOB_COMPLETE,
                    str(clip_path),
                    clip_bytes,
                    json.dumps(metadata),
                    _now(),
                    job_id,
                ),
            )

    def fail_job(self, job_id: str, error: dict[str, str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, error_json=?, updated_at=? WHERE job_id=?",
                (JOB_FAILED, json.dumps(error), _now(), job_id),
            )

    def recent_jobs(self, limit: int = 50) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def purge_old_jobs(self, days: int) -> int:
        """Drop finished jobs and their audio once nobody could still want them.

        Clip files go with the rows. Audio the counsellor never consented to
        keeping indefinitely should not accumulate on a server forever just
        because nobody wrote the cleanup.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, clip_path FROM jobs WHERE created_at < ? AND status IN (?, ?)",
                (cutoff, JOB_COMPLETE, JOB_FAILED),
            ).fetchall()
            for row in rows:
                if row["clip_path"]:
                    try:
                        Path(row["clip_path"]).unlink(missing_ok=True)
                    except OSError:
                        pass
            cursor = self._conn.execute(
                "DELETE FROM jobs WHERE created_at < ? AND status IN (?, ?)",
                (cutoff, JOB_COMPLETE, JOB_FAILED),
            )
        return cursor.rowcount

    def counts(self) -> dict[str, int]:
        with self._lock:
            devices = self._conn.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]
            jobs = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status IN (?, ?)",
                (JOB_QUEUED, JOB_DELIVERED),
            ).fetchone()["n"]
        return {"devices": devices, "jobs": jobs, "pending_jobs": pending}
