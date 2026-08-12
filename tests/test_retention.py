from __future__ import annotations

from datetime import datetime, timedelta

from ongoingrec import retention
from ongoingrec.timeutil import floor_to_boundary, utcnow

from .helpers import record_synthetic


def aligned_start(**delta) -> datetime:
    """A start time on the 30-minute grid, *delta* before now.

    Retention works against the real clock, so these tests need real relative
    times -- but an arbitrary start can straddle a segment boundary and split
    a short recording in two, making segment counts depend on what time the
    suite happens to run. Anchoring to the grid keeps them deterministic.
    """
    return floor_to_boundary(utcnow() - timedelta(**delta), 1800)


class TestAgeLimit:
    def test_segments_past_the_retention_window_are_removed(self, config, db):
        config.retention_hours = 72
        old_start = aligned_start(hours=100)
        recent_start = aligned_start(hours=2)
        record_synthetic(config, db, start=old_start, seconds=120)
        record_synthetic(config, db, start=recent_start, seconds=120)

        result = retention.enforce(config, db)

        assert result["deleted_by_age"] == 1
        assert db.segment_count() == 1
        assert db.latest_segment().start == recent_start

    def test_files_are_deleted_along_with_their_rows(self, config, db):
        config.retention_hours = 1
        record_synthetic(config, db, start=aligned_start(hours=5), seconds=60)
        segment = db.latest_segment()
        assert segment.file_path.exists()

        retention.enforce(config, db)

        assert not segment.file_path.exists()
        assert db.segment_count() == 0

    def test_recent_audio_is_left_alone(self, config, db):
        config.retention_hours = 72
        record_synthetic(config, db, start=aligned_start(minutes=10), seconds=60)
        before = db.segment_count()

        result = retention.enforce(config, db)

        assert result["deleted_by_age"] == 0
        assert db.segment_count() == before

    def test_the_segment_being_recorded_is_never_deleted(self, config, db):
        """Deleting the live file would corrupt audio nobody has collected yet."""
        config.retention_hours = 1
        record_synthetic(config, db, start=aligned_start(hours=5), seconds=60)
        current = db.latest_segment()

        result = retention.enforce(config, db, protect_segment_id=current.id)

        assert result["deleted_by_age"] == 0
        assert current.file_path.exists()

    def test_empty_day_directories_are_cleaned_up(self, config, db):
        config.retention_hours = 1
        record_synthetic(config, db, start=aligned_start(hours=5), seconds=60)
        day_dirs = list(config.recordings_dir.iterdir())
        assert day_dirs

        retention.enforce(config, db)

        assert list(config.recordings_dir.iterdir()) == []


class TestFreeSpaceFloor:
    def test_oldest_audio_goes_first_when_disk_is_low(self, config, db, monkeypatch):
        """A laptop can fill up for unrelated reasons; wedging it is worse than
        losing the oldest hour."""
        config.retention_hours = 999
        base = aligned_start(hours=3)
        for offset in range(3):
            record_synthetic(config, db, start=base + timedelta(minutes=offset * 5), seconds=30)
        oldest = db.oldest_segments(limit=1)[0]

        # Report a full disk until something has been deleted.
        state = {"freed": False}
        monkeypatch.setattr(
            retention,
            "free_disk_mb",
            lambda path: 99999.0 if state["freed"] else 10.0,
        )
        real_delete = retention._delete

        def delete_and_relieve(db_, segments, reason):
            count = real_delete(db_, segments, reason)
            if count:
                state["freed"] = True
            return count

        monkeypatch.setattr(retention, "_delete", delete_and_relieve)

        result = retention.enforce(config, db)

        assert result["deleted_by_space"] >= 1
        assert db.get_segment(oldest.id) is None

    def test_gives_up_rather_than_spinning_when_nothing_is_left(self, config, db, monkeypatch):
        monkeypatch.setattr(retention, "free_disk_mb", lambda path: 1.0)
        result = retention.enforce(config, db)
        assert result["deleted_by_space"] == 0
