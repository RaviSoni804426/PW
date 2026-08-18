from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from ongoingrec.index import (
    JOB_DONE,
    JOB_PENDING,
    STATUS_COMPLETE,
    STATUS_RECORDING,
    Database,
)
from ongoingrec.timeutil import parse_timestamp

SAMPLE_RATE = 16000


def make_segment(db: Database, start_iso: str, *, seconds: float | None = None, index: int = 0):
    start = parse_timestamp(start_iso)
    segment_id = db.open_segment(
        install_id="install-1",
        email_id="abc@example.com",
        employee_id="EMP001",
        start=start,
        sample_rate=SAMPLE_RATE,
        channels=1,
        file_path=Path(f"/recordings/{start_iso.replace(':', '-')}-{index}.mp3"),
    )
    if seconds is not None:
        db.close_segment(segment_id, sample_count=int(seconds * SAMPLE_RATE))
    return segment_id


class TestSegmentLifecycle:
    def test_open_then_close_derives_end_from_samples(self, db):
        segment_id = make_segment(db, "2026-08-12T11:00:00Z")
        assert db.get_segment(segment_id).status == STATUS_RECORDING
        assert db.get_segment(segment_id).end is None

        db.close_segment(segment_id, sample_count=1800 * SAMPLE_RATE)
        segment = db.get_segment(segment_id)
        assert segment.status == STATUS_COMPLETE
        assert segment.end == parse_timestamp("2026-08-12T11:30:00Z")

    def test_short_segment_end_reflects_captured_audio_not_the_grid(self, db):
        """A segment cut short by shutdown ends where the audio ends.

        Trusting the 30-minute grid here would claim audio that was never
        recorded, and extraction would hand the backend silence labelled as
        speech.
        """
        segment_id = make_segment(db, "2026-08-12T11:00:00Z", seconds=423.5)
        segment = db.get_segment(segment_id)
        assert segment.end == parse_timestamp("2026-08-12T11:00:00Z") + timedelta(seconds=423.5)
        assert segment.duration_seconds == pytest.approx(423.5)

    def test_open_segment_is_searchable_via_progress_checkpoints(self, db):
        """An in-progress segment must be retrievable up to what it has captured."""
        segment_id = make_segment(db, "2026-08-12T11:00:00Z")
        db.update_progress(segment_id, sample_count=600 * SAMPLE_RATE)

        segment = db.get_segment(segment_id)
        assert segment.end is None
        assert segment.effective_end == parse_timestamp("2026-08-12T11:10:00Z")
        assert segment.contains(parse_timestamp("2026-08-12T11:05:00Z"))
        assert not segment.contains(parse_timestamp("2026-08-12T11:15:00Z"))

    def test_closing_an_unknown_segment_raises(self, db):
        with pytest.raises(KeyError):
            db.close_segment(9999, sample_count=1)

    def test_duplicate_file_path_is_rejected(self, db):
        make_segment(db, "2026-08-12T11:00:00Z", seconds=1800)
        with pytest.raises(Exception):
            make_segment(db, "2026-08-12T11:00:00Z", seconds=1800)


class TestOverlapQueries:
    @pytest.fixture
    def populated(self, db):
        make_segment(db, "2026-08-12T10:30:00Z", seconds=1800, index=1)
        make_segment(db, "2026-08-12T11:00:00Z", seconds=1800, index=2)
        make_segment(db, "2026-08-12T11:30:00Z", seconds=1800, index=3)
        return db

    def test_finds_the_single_containing_segment(self, populated):
        """PRD section 16: 11:22:15 resolves to the 11:00-11:30 segment."""
        found = populated.segments_overlapping(
            parse_timestamp("2026-08-12T11:22:15Z"),
            parse_timestamp("2026-08-12T11:22:16Z"),
        )
        assert len(found) == 1
        assert found[0].start == parse_timestamp("2026-08-12T11:00:00Z")

    def test_window_crossing_a_boundary_returns_both_segments(self, populated):
        """PRD section 18: 11:24-11:34 spans two 30-minute segments."""
        found = populated.segments_overlapping(
            parse_timestamp("2026-08-12T11:24:00Z"),
            parse_timestamp("2026-08-12T11:34:00Z"),
        )
        assert [s.start for s in found] == [
            parse_timestamp("2026-08-12T11:00:00Z"),
            parse_timestamp("2026-08-12T11:30:00Z"),
        ]

    def test_results_are_chronological(self, populated):
        found = populated.segments_overlapping(
            parse_timestamp("2026-08-12T10:00:00Z"),
            parse_timestamp("2026-08-12T13:00:00Z"),
        )
        assert [s.start for s in found] == sorted(s.start for s in found)

    def test_window_touching_a_boundary_exactly_excludes_the_neighbour(self, populated):
        """Half-open intervals: a window ending at 11:30 must not pull in 11:30-12:00."""
        found = populated.segments_overlapping(
            parse_timestamp("2026-08-12T11:10:00Z"),
            parse_timestamp("2026-08-12T11:30:00Z"),
        )
        assert len(found) == 1
        assert found[0].start == parse_timestamp("2026-08-12T11:00:00Z")

    def test_gap_in_coverage_returns_nothing(self, populated):
        found = populated.segments_overlapping(
            parse_timestamp("2026-08-12T14:00:00Z"),
            parse_timestamp("2026-08-12T14:10:00Z"),
        )
        assert found == []

    def test_open_segment_is_included(self, db):
        segment_id = make_segment(db, "2026-08-12T11:00:00Z")
        db.update_progress(segment_id, sample_count=300 * SAMPLE_RATE)
        found = db.segments_overlapping(
            parse_timestamp("2026-08-12T11:01:00Z"),
            parse_timestamp("2026-08-12T11:02:00Z"),
        )
        assert len(found) == 1


class TestRepairAndRetention:
    def test_stale_open_segments_are_discoverable_after_a_crash(self, db):
        crashed = make_segment(db, "2026-08-12T11:00:00Z", index=1)
        make_segment(db, "2026-08-12T11:30:00Z", seconds=1800, index=2)
        assert [s.id for s in db.stale_open_segments()] == [crashed]

    def test_current_segment_can_be_excluded_from_repair(self, db):
        current = make_segment(db, "2026-08-12T11:00:00Z")
        assert db.stale_open_segments(exclude_id=current) == []

    def test_segments_before_cutoff(self, db):
        old = make_segment(db, "2026-08-09T11:00:00Z", seconds=1800, index=1)
        make_segment(db, "2026-08-12T11:00:00Z", seconds=1800, index=2)
        expired = db.segments_before(parse_timestamp("2026-08-10T00:00:00Z"))
        assert [s.id for s in expired] == [old]

    def test_delete_removes_rows(self, db):
        first = make_segment(db, "2026-08-12T11:00:00Z", seconds=1800, index=1)
        make_segment(db, "2026-08-12T11:30:00Z", seconds=1800, index=2)
        assert db.delete_segments([first]) == 1
        assert db.segment_count() == 1

    def test_delete_of_nothing_is_a_no_op(self, db):
        assert db.delete_segments([]) == 0


class TestJobs:
    def test_duplicate_delivery_creates_one_job(self, db):
        """At-least-once delivery must not produce two uploads."""
        requested = parse_timestamp("2026-08-12T11:22:15Z")
        assert db.upsert_job("job-1", requested, 600) is True
        assert db.upsert_job("job-1", requested, 600) is False
        assert db.get_job("job-1").status == JOB_PENDING

    def test_claimable_respects_backoff(self, db):
        requested = parse_timestamp("2026-08-12T11:22:15Z")
        db.upsert_job("job-1", requested, 600)
        db.update_job("job-1", next_attempt_epoch=9_999_999_999.0, attempts=1)
        assert db.claimable_jobs(now_epoch=1_000.0) == []

    def test_finished_jobs_are_not_reclaimed(self, db):
        db.upsert_job("job-1", parse_timestamp("2026-08-12T11:22:15Z"), 600)
        db.update_job("job-1", status=JOB_DONE)
        assert db.claimable_jobs(now_epoch=9_999_999_999.0) == []

    def test_job_survives_a_reconnect(self, config):
        """A job accepted before a restart is still owed afterwards."""
        first = Database(config.db_path)
        first.upsert_job("job-1", parse_timestamp("2026-08-12T11:22:15Z"), 600)
        first.close()

        second = Database(config.db_path)
        assert [j.job_id for j in second.claimable_jobs(9_999_999_999.0)] == ["job-1"]
        second.close()
