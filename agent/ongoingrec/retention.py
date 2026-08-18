"""Disk management for a recorder that never stops.

The backend collects clips daily, so segments do not need to be kept for long
(PRD section 29 leaves retention unspecified). Seventy-two hours is the
default: it costs roughly 350 MB at 32 kbps and covers a weekend, a laptop
left switched off, or a backend outage, any of which would otherwise destroy
audio before it was ever collected.

Two rules run together. Age is the normal one. Free space is the safety net,
because a counsellor's laptop can fill up for reasons that have nothing to do
with this service, and a recorder that wedges a machine at zero bytes free is
a worse failure than one that drops its oldest hour.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

from .config import Config
from .index import Database
from .logging_setup import get_logger
from .timeutil import utcnow

log = get_logger(__name__)


def free_disk_mb(path: Path) -> float:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    return shutil.disk_usage(target).free / (1024 * 1024)


def _delete(db: Database, segments, reason: str) -> int:
    """Remove files first, then their index rows.

    In that order, an interruption leaves rows pointing at missing files --
    which the extractor already handles by skipping them -- rather than
    orphaned files that nothing will ever clean up.
    """
    removed: list[int] = []
    for segment in segments:
        path = Path(segment.file_path)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not delete %s: %s", path, exc)
            continue
        removed.append(segment.id)

    deleted = db.delete_segments(removed)
    if deleted:
        log.info("retention: removed %d segment(s) (%s)", deleted, reason)
    return deleted


def _prune_empty_day_directories(recordings_dir: Path) -> None:
    if not recordings_dir.exists():
        return
    for day in recordings_dir.iterdir():
        if day.is_dir() and not any(day.iterdir()):
            try:
                day.rmdir()
            except OSError:
                pass


def enforce(config: Config, db: Database, *, protect_segment_id: int | None = None) -> dict:
    """Apply age and free-space limits once. Returns what it did.

    *protect_segment_id* is the segment currently being recorded; deleting the
    file out from under a running ffmpeg would corrupt audio that has not even
    been collected yet.
    """
    cutoff = utcnow() - timedelta(hours=config.retention_hours)
    expired = [s for s in db.segments_before(cutoff) if s.id != protect_segment_id]
    by_age = _delete(db, expired, f"older than {config.retention_hours}h")

    by_space = 0
    passes = 0
    while free_disk_mb(config.recordings_dir) < config.min_free_disk_mb:
        passes += 1
        oldest = [s for s in db.oldest_segments(limit=20) if s.id != protect_segment_id]
        if not oldest:
            log.error(
                "free space is %.0f MB, below the %d MB floor, but there are no "
                "further segments to remove",
                free_disk_mb(config.recordings_dir),
                config.min_free_disk_mb,
            )
            break
        by_space += _delete(db, oldest, "low disk space")
        if passes > 100:  # pathological disk; stop rather than spin
            log.error("retention gave up trying to reach the free-space floor")
            break

    db.purge_finished_jobs(utcnow() - timedelta(days=7))
    _prune_empty_day_directories(config.recordings_dir)

    return {
        "deleted_by_age": by_age,
        "deleted_by_space": by_space,
        "free_mb": round(free_disk_mb(config.recordings_dir), 1),
    }
